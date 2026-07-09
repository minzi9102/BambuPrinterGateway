"""Minimal public queue web app."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import open_database
from .gateway import BambuAdapter, PrinterService
from .jobs import Job, JobStateService, JobStatus, QueueService
from .phase0 import Phase0Error, PrinterConfig, validate_print_file

CHUNK_SIZE = 1024 * 1024
security = HTTPBasic()


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


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


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


def job_response(job: Job) -> dict[str, str]:
    return {
        "id": job.id,
        "display_name": job.display_name,
        "project_name": job.project_name,
        "status": job.status.value,
    }


def status_response(printer_service: object | None, queue: QueueService) -> dict[str, Any]:
    if not printer_service:
        return {"printer": {"connected": False, "state": "unknown"}}
    raw_status = getattr(printer_service, "raw_status", None) or {}
    printer = {
        "connected": bool(getattr(printer_service, "connected", False)),
        "state": getattr(printer_service, "normalized_state", "unknown"),
        "progress": raw_status.get("mc_percent"),
        "remaining_minutes": raw_status.get("mc_remaining_time"),
        "current_task": raw_status.get("subtask_name") or raw_status.get("gcode_file"),
        "layer": raw_status.get("layer_num"),
        "total_layers": raw_status.get("total_layer_num"),
        "current_job": None,
    }
    active = queue.get_active_job()
    if active and active.status == JobStatus.PRINTING:
        printer["current_job"] = job_response(active)
    return {"printer": printer}


async def wait_for_printing(printer_service: object, timeout: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if getattr(printer_service, "normalized_state", None) == "printing":
            return True
        await asyncio.sleep(0.25)
    return False


async def refresh_printer_connection(printer_service: object) -> None:
    stop = getattr(printer_service, "stop", None)
    start = getattr(printer_service, "start", None)
    if callable(stop) and callable(start):
        await asyncio.to_thread(stop)
        await asyncio.to_thread(start)


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
    printer_service: object | None = None,
    adapter: object | None = None,
    admin_username: str | None = None,
    admin_password: str | None = None,
    start_confirm_timeout: int | None = None,
    upload_timeout: int | None = None,
) -> FastAPI:
    if database_path is None and upload_dir is None:
        load_env_file()
    db_path = Path(database_path or os.environ.get("DATABASE_PATH", "data/queue.db"))
    uploads = Path(upload_dir or os.environ.get("UPLOAD_DIR", "uploads"))
    max_bytes = (max_upload_mb if max_upload_mb is not None else env_int("MAX_UPLOAD_MB", 500)) * 1024 * 1024
    start_timeout = start_confirm_timeout if start_confirm_timeout is not None else env_int("START_CONFIRM_TIMEOUT", 120)
    upload_wait = upload_timeout if upload_timeout is not None else env_int("UPLOAD_TIMEOUT", 600)
    username = admin_username or os.environ.get("ADMIN_USERNAME", "admin")
    password = admin_password or os.environ.get("ADMIN_PASSWORD", "CHANGE_ME")
    conn = open_database(db_path)
    queue = QueueService(conn)
    states = JobStateService(conn)
    hub = RealtimeHub()
    operation_lock = asyncio.Lock()
    if printer_service is None:
        names = ("PRINTER_HOST", "PRINTER_ACCESS_CODE", "PRINTER_SERIAL")
        if all(os.environ.get(name, "").strip() for name in names):
            real_adapter = BambuAdapter(PrinterConfig.from_env())
            adapter = real_adapter
            printer_service = PrinterService(real_adapter)
    elif adapter is None:
        adapter = getattr(printer_service, "adapter", None)

    def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
        valid = secrets.compare_digest(credentials.username, username) and secrets.compare_digest(
            credentials.password, password
        )
        if not valid:
            raise HTTPException(401, "Invalid admin credentials.", headers={"WWW-Authenticate": "Basic"})

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if printer_service and adapter and callable(getattr(printer_service, "start", None)):
            try:
                await asyncio.to_thread(printer_service.start)
            except Exception:
                pass
        try:
            yield
        finally:
            if printer_service and callable(getattr(printer_service, "stop", None)):
                await asyncio.to_thread(printer_service.stop)
            conn.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/api/status")
    def get_status():
        return status_response(printer_service, queue)

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

    @app.post("/api/admin/start-next")
    async def start_next_job(_: None = Depends(require_admin)):
        if operation_lock.locked():
            raise HTTPException(409, "Printer operation already in progress.")
        if not printer_service or not adapter or not getattr(printer_service, "connected", False):
            raise HTTPException(503, "Printer is not connected.")
        if getattr(printer_service, "normalized_state", None) not in {"idle", "finished"}:
            raise HTTPException(409, "Printer is not idle.")
        if queue.get_active_job():
            raise HTTPException(409, "A job is already active.")

        async with operation_lock:
            job = queue.get_next_job()
            if not job:
                raise HTTPException(404, "No queued jobs.")
            remote_path = f"cache/{job.remote_filename}"
            job = states.change_job_state(job.id, JobStatus.UPLOADING)
            await hub.broadcast({"type": "queue.changed"})
            try:
                await asyncio.to_thread(adapter.upload_file, Path(job.stored_path), remote_path, upload_wait)
                exists = await asyncio.to_thread(adapter.file_exists, remote_path, upload_wait)
                if not exists:
                    states.change_job_state(job.id, JobStatus.FAILED, f"Remote file not found: {remote_path}")
                    await hub.broadcast({"type": "job.changed"})
                    raise HTTPException(502, "Uploaded file was not found on printer.")
                job = states.change_job_state(job.id, JobStatus.STARTING)
                await refresh_printer_connection(printer_service)
                await asyncio.to_thread(adapter.start_print, job.remote_filename, remote_path)
                if not await wait_for_printing(printer_service, start_timeout):
                    states.change_job_state(job.id, JobStatus.FAILED, "Printer did not confirm print start")
                    await hub.broadcast({"type": "job.changed"})
                    raise HTTPException(504, "Printer did not confirm print start.")
                job = states.change_job_state(job.id, JobStatus.PRINTING)
            except HTTPException:
                raise
            except Exception as error:
                active = queue.get_active_job()
                if active and active.id == job.id and active.status in {JobStatus.UPLOADING, JobStatus.STARTING}:
                    states.change_job_state(job.id, JobStatus.FAILED, str(error))
                await hub.broadcast({"type": "job.changed"})
                raise HTTPException(502, str(error)) from error
            await hub.broadcast({"type": "job.changed"})
            await hub.broadcast({"type": "queue.changed"})
            return {"job": job_response(job)}

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
