"""Minimal public queue web app."""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import sys
import uuid
import zipfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .database import open_database
from .gateway import BambuAdapter, MQTT_RECONNECT_SECONDS, PrinterService
from .jobs import Job, JobStateService, JobStatus, QueueError, QueueService, now
from .phase0 import Phase0Error, PrinterConfig, validate_print_file

CHUNK_SIZE = 1024 * 1024
PREVIEW_PATH = "Metadata/plate_1.png"
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
START_READY_STATES = {"idle", "finished", "failed"}
COMPLETED_PRINTER_STATES = {"idle", "finished"}
STARTUP_FAILURE_MESSAGE = "Server restarted during job startup"
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


def history_response(queue: QueueService, *, include_error: bool = False) -> dict[str, list[dict[str, Any]]]:
    jobs = []
    for job in queue.get_history():
        item = {
            "id": job.id,
            "display_name": job.display_name,
            "project_name": job.project_name,
            "status": job.status.value,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
        if include_error:
            item["error_message"] = job.error_message
        jobs.append(item)
    return {"jobs": jobs}


def ams_tray_response(raw_status: dict[str, Any]) -> list[dict[str, Any]]:
    ams_units = ((raw_status.get("ams") or {}).get("ams") or [])
    trays = (ams_units[0].get("tray") or []) if ams_units else []
    response = []
    for index, tray in enumerate(trays[:4]):
        try:
            slot = int(tray.get("id", index))
        except (TypeError, ValueError):
            slot = index
        sub_brand = str(tray.get("tray_sub_brands") or "").strip()
        material = sub_brand or str(tray.get("tray_type") or "").strip()
        tray_id = str(tray.get("tray_id_name") or "").strip()
        remain = tray.get("remain")
        parts = [f"AMS Slot {slot + 1}", material, tray_id]
        if isinstance(remain, int) and remain >= 0:
            parts.append(f"{remain}%")
        response.append(
            {
                "slot": slot,
                "label": " - ".join(part for part in parts if part),
                "type": tray.get("tray_type"),
                "sub_brand": tray.get("tray_sub_brands"),
                "color": tray.get("tray_color"),
                "remain": remain,
                "tray_id_name": tray.get("tray_id_name"),
            }
        )
    return response


def ams_slot_from_body(body: dict[str, Any] | None) -> int:
    slot = (body or {}).get("ams_slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= 3:
        raise HTTPException(400, "ams_slot must be an integer from 0 to 3.")
    return slot


def move_direction_from_body(body: dict[str, Any] | None) -> str:
    direction = (body or {}).get("direction")
    if direction not in {"up", "down"}:
        raise HTTPException(400, 'direction must be "up" or "down".')
    return direction


def current_task(raw_status: dict[str, Any]) -> Any:
    return raw_status.get("subtask_name") or raw_status.get("gcode_file")


def telemetry_response(raw_status: dict[str, Any]) -> dict[str, Any]:
    return {
        "temperatures": {
            "nozzle": {
                "current": raw_status.get("nozzle_temper"),
                "target": raw_status.get("nozzle_target_temper"),
            },
            "bed": {
                "current": raw_status.get("bed_temper"),
                "target": raw_status.get("bed_target_temper"),
            },
            "chamber": raw_status.get("chamber_temper"),
        },
        "fans": {
            "cooling": raw_status.get("cooling_fan_speed"),
            "heatbreak": raw_status.get("heatbreak_fan_speed"),
            "auxiliary_1": raw_status.get("big_fan1_speed"),
            "auxiliary_2": raw_status.get("big_fan2_speed"),
        },
        "wifi_signal": raw_status.get("wifi_signal"),
    }


def fail_interrupted_startup_jobs(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = COALESCE(finished_at, ?), error_message = ?
            WHERE status IN (?, ?)
            """,
            (
                JobStatus.FAILED.value,
                now(),
                STARTUP_FAILURE_MESSAGE,
                JobStatus.UPLOADING.value,
                JobStatus.STARTING.value,
            ),
        )


def status_response(printer_service: object | None, queue: QueueService) -> dict[str, Any]:
    if not printer_service:
        return {
            "printer": {
                "connected": False,
                "state": "unknown",
                "raw_state": None,
                "ams_trays": [],
                "telemetry": telemetry_response({}),
            }
        }
    raw_status = getattr(printer_service, "raw_status", None) or {}
    printer = {
        "connected": bool(getattr(printer_service, "connected", False)),
        "state": getattr(printer_service, "normalized_state", "unknown"),
        "raw_state": raw_status.get("gcode_state"),
        "progress": raw_status.get("mc_percent"),
        "remaining_minutes": raw_status.get("mc_remaining_time"),
        "current_task": current_task(raw_status),
        "layer": raw_status.get("layer_num"),
        "total_layers": raw_status.get("total_layer_num"),
        "current_job": None,
        "ams_trays": ams_tray_response(raw_status),
        "telemetry": telemetry_response(raw_status),
    }
    active = queue.get_active_job()
    if active:
        printer["current_job"] = job_response(active)
        if active.status == JobStatus.STARTING and not printer["connected"]:
            printer["state"] = "reconnecting"
    return {"printer": printer}


def debug_response(printer_service: object | None, queue: QueueService) -> dict[str, Any]:
    raw_status = getattr(printer_service, "raw_status", None) or {}
    ams_units = ((raw_status.get("ams") or {}).get("ams") or [])
    trays = (ams_units[0].get("tray") or []) if ams_units else []
    return {
        "runtime": {
            "python": sys.executable,
            "cwd": str(Path.cwd()),
            "web_module_file": __file__,
            "gateway_module_file": sys.modules[BambuAdapter.__module__].__file__,
        },
        "printer": {
            "connected": bool(getattr(printer_service, "connected", False)) if printer_service else False,
            "normalized_state": getattr(printer_service, "normalized_state", "unknown") if printer_service else "unknown",
            "last_seen_at": getattr(printer_service, "last_seen_at", None) if printer_service else None,
            "raw_gcode_state": raw_status.get("gcode_state"),
            "print_error": raw_status.get("print_error"),
            "hms": raw_status.get("hms"),
            "current_task": current_task(raw_status),
            "progress": raw_status.get("mc_percent"),
        },
        "ams": {
            "ams_status": raw_status.get("ams_status"),
            "ams_rfid_status": raw_status.get("ams_rfid_status"),
            "ams_present": bool(ams_units),
            "ams_tray_count": len(trays),
            "ams_trays": ams_tray_response(raw_status),
        },
        "queue": queue_response(queue),
    }


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


async def cleanup_printer_connection(printer_service: object) -> None:
    stop = getattr(printer_service, "stop", None)
    if callable(stop):
        with suppress(Exception):
            await asyncio.to_thread(stop)


async def retry_printer_connection(printer_service: object) -> None:
    while not getattr(printer_service, "connected", False):
        await asyncio.sleep(MQTT_RECONNECT_SECONDS)
        if getattr(printer_service, "connected", False):
            return
        try:
            await asyncio.to_thread(printer_service.start)
        except Exception:
            await cleanup_printer_connection(printer_service)
            print(f"Printer unavailable; retrying in {MQTT_RECONNECT_SECONDS} seconds.")
        else:
            if getattr(printer_service, "connected", False):
                return
            await cleanup_printer_connection(printer_service)


async def reconcile_active_job(
    printer_state: str | None,
    queue: QueueService,
    states: JobStateService,
    hub: RealtimeHub,
) -> None:
    active = queue.get_active_job()
    if not active or active.status != JobStatus.PRINTING:
        return
    if printer_state in COMPLETED_PRINTER_STATES:
        states.change_job_state(active.id, JobStatus.COMPLETED)
    elif printer_state == "failed":
        states.change_job_state(active.id, JobStatus.FAILED, "Printer reported FAILED")
    else:
        return
    await hub.broadcast({"type": "job.changed"})
    await hub.broadcast({"type": "queue.changed"})


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
    fail_interrupted_startup_jobs(conn)
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
        retry_task: asyncio.Task[None] | None = None
        if printer_service and adapter and callable(getattr(printer_service, "start", None)):
            try:
                await asyncio.to_thread(printer_service.start)
            except Exception:
                await cleanup_printer_connection(printer_service)
                print(f"Printer unavailable; retrying in {MQTT_RECONNECT_SECONDS} seconds.")
                retry_task = asyncio.create_task(retry_printer_connection(printer_service))
        try:
            yield
        finally:
            if retry_task:
                retry_task.cancel()
                with suppress(asyncio.CancelledError):
                    await retry_task
            if printer_service and callable(getattr(printer_service, "stop", None)):
                await asyncio.to_thread(printer_service.stop)
            conn.close()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(sqlite3.Error)
    async def database_error_handler(_: Any, __: sqlite3.Error):
        return JSONResponse({"detail": "Queue database error."}, status_code=500)

    @app.get("/api/status")
    async def get_status():
        if printer_service:
            await reconcile_active_job(getattr(printer_service, "normalized_state", None), queue, states, hub)
        return status_response(printer_service, queue)

    @app.get("/api/admin/debug")
    def get_admin_debug(_: None = Depends(require_admin)):
        return debug_response(printer_service, queue)

    @app.get("/api/admin/history")
    def get_admin_history(_: None = Depends(require_admin)):
        return history_response(queue, include_error=True)

    @app.get("/api/queue")
    def get_queue():
        return queue_response(queue)

    @app.get("/api/history")
    def get_history():
        return history_response(queue)

    @app.get("/api/jobs/{job_id}/preview")
    def get_job_preview(job_id: str):
        job = queue.get_active_job()
        if not job or job.id != job_id:
            raise HTTPException(404, "Preview not available.")
        try:
            with zipfile.ZipFile(job.stored_path) as archive:
                info = archive.getinfo(PREVIEW_PATH)
                if info.file_size > PREVIEW_MAX_BYTES:
                    raise HTTPException(404, "Preview not available.")
                preview = archive.read(info)
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise HTTPException(404, "Preview not available.") from error
        if not preview.startswith(PNG_SIGNATURE):
            raise HTTPException(404, "Preview not available.")
        return Response(
            preview,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/jobs")
    async def create_job(
        display_name: str = Form(...),
        file: UploadFile = File(...),
    ):
        display = display_name.strip()
        if not display:
            raise HTTPException(400, "display_name is required.")

        stored_filename, stored_path = await save_upload(file, uploads, max_bytes)
        original_filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1] or stored_filename
        try:
            job = queue.create_job(
                display,
                original_filename,
                original_filename,
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
    async def start_next_job(body: dict[str, Any] | None = Body(default=None), _: None = Depends(require_admin)):
        ams_slot = ams_slot_from_body(body)
        if operation_lock.locked():
            raise HTTPException(409, "Printer operation already in progress.")
        if not printer_service or not adapter or not getattr(printer_service, "connected", False):
            raise HTTPException(503, "Printer is not connected.")
        printer_state = getattr(printer_service, "normalized_state", None)
        if printer_state not in START_READY_STATES:
            raise HTTPException(409, "Printer is not idle.")

        async with operation_lock:
            await reconcile_active_job(printer_state, queue, states, hub)
            if queue.get_active_job():
                raise HTTPException(409, "A job is already active.")
            job = queue.get_next_job()
            if not job:
                raise HTTPException(404, "No queued jobs.")
            remote_path = f"cache/{job.remote_filename}"
            job = states.change_job_state(job.id, JobStatus.UPLOADING)
            await hub.broadcast({"type": "queue.changed"})
            await hub.broadcast({"type": "job.changed"})
            try:
                await asyncio.to_thread(adapter.upload_file, Path(job.stored_path), remote_path, upload_wait)
                exists = await asyncio.to_thread(adapter.file_exists, remote_path, upload_wait)
                if not exists:
                    states.change_job_state(job.id, JobStatus.FAILED, f"Remote file not found: {remote_path}")
                    await hub.broadcast({"type": "job.changed"})
                    raise HTTPException(502, "Uploaded file was not found on printer.")
                job = states.change_job_state(job.id, JobStatus.STARTING)
                await hub.broadcast({"type": "job.changed"})
                await refresh_printer_connection(printer_service)
                await asyncio.to_thread(adapter.start_print, job.remote_filename, remote_path, ams_slot=ams_slot)
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

    @app.post("/api/admin/jobs/{job_id}/cancel")
    async def cancel_queued_job(job_id: str, _: None = Depends(require_admin)):
        try:
            job = queue.cancel_job(job_id)
        except QueueError as error:
            raise HTTPException(409, str(error)) from error
        await hub.broadcast({"type": "queue.changed"})
        await hub.broadcast({"type": "job.changed"})
        return {"job": job_response(job)}

    @app.post("/api/admin/jobs/{job_id}/move")
    async def move_queued_job(
        job_id: str,
        body: dict[str, Any] | None = Body(default=None),
        _: None = Depends(require_admin),
    ):
        try:
            job = queue.move_job(job_id, move_direction_from_body(body))
        except QueueError as error:
            raise HTTPException(409, str(error)) from error
        position = next(index for index, item in enumerate(queue.get_queue(), start=1) if item.id == job.id)
        await hub.broadcast({"type": "queue.changed"})
        return {"job": job_response(job), "position": position}

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
    uvicorn.run(
        create_app(),
        host=os.environ.get("BAMBU_QUEUE_HOST", "127.0.0.1"),
        port=env_int("BAMBU_QUEUE_PORT", 8000),
    )
