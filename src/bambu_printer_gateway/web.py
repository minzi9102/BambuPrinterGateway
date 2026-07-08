"""Minimal public queue web app."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import open_database
from .jobs import QueueService
from .phase0 import Phase0Error, validate_print_file

CHUNK_SIZE = 1024 * 1024


class RealtimeHub:
    def __init__(self):
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        for websocket in list(self.connections):
            try:
                await websocket.send_json(message)
            except RuntimeError:
                self.disconnect(websocket)


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return default if not value else int(value)


def queue_response(queue: QueueService) -> dict[str, list[dict[str, Any]]]:
    return {
        "jobs": [
            {
                "position": index,
                "id": job.id,
                "display_name": job.display_name,
                "project_name": job.project_name,
                "status": job.status.value,
            }
            for index, job in enumerate(queue.get_queue(), start=1)
        ]
    }


async def save_upload(file: UploadFile, upload_dir: Path, max_bytes: int) -> tuple[str, Path]:
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid.uuid4().hex}.gcode.3mf"
    stored_path = upload_dir / stored_filename
    size = 0
    keep_file = False
    try:
        with stored_path.open("wb") as stream:
            while chunk := await file.read(CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(400, "Uploaded file is too large.")
                stream.write(chunk)
        validate_print_file(stored_path)
        keep_file = True
        return stored_filename, stored_path
    except Phase0Error as error:
        raise HTTPException(400, str(error)) from error
    except HTTPException:
        raise
    finally:
        await file.close()
        if stored_path.exists() and not keep_file:
            stored_path.unlink()


def create_app(
    *,
    database_path: str | Path | None = None,
    upload_dir: str | Path | None = None,
    max_upload_mb: int | None = None,
) -> FastAPI:
    db_path = Path(database_path or os.environ.get("DATABASE_PATH", "data/queue.db"))
    uploads = Path(upload_dir or os.environ.get("UPLOAD_DIR", "uploads"))
    max_bytes = (max_upload_mb if max_upload_mb is not None else env_int("MAX_UPLOAD_MB", 500)) * 1024 * 1024
    conn = open_database(db_path)
    queue = QueueService(conn)
    hub = RealtimeHub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/api/status")
    def get_status():
        return {"printer": {"connected": False, "state": "unknown"}}

    @app.get("/api/queue")
    def get_queue():
        return queue_response(queue)

    @app.post("/api/jobs")
    async def create_job(
        display_name: str = Form(...),
        project_name: str = Form(...),
        file: UploadFile = File(...),
    ):
        display = display_name.strip()
        project = project_name.strip()
        if not display or not project:
            raise HTTPException(400, "display_name and project_name are required.")

        stored_filename, stored_path = await save_upload(file, uploads, max_bytes)
        try:
            job = queue.create_job(
                display,
                project,
                file.filename or stored_filename,
                stored_filename,
                str(stored_path),
            )
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise

        jobs = queue.get_queue()
        position = next(index for index, item in enumerate(jobs, start=1) if item.id == job.id)
        await hub.broadcast({"type": "queue.changed"})
        return JSONResponse({"id": job.id, "status": job.status.value, "position": position})

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await hub.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            hub.disconnect(websocket)

    static_dir = Path(__file__).with_name("static")
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def main() -> None:
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
