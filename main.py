# sabnzbd_emulator.py
import asyncio
import json
import logging
import shutil
import os
import pathlib
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Dict, List, Optional

import torboxapi

import uvicorn
import aiofiles  # New: For async file operations
import httpx

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

# --- Configuration Loading ---
CONFIG_FILE = "config.json"
try:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"ERROR: Configuration file '{CONFIG_FILE}' not found.")
    exit(1)
except json.JSONDecodeError:
    print(f"ERROR: Configuration file '{CONFIG_FILE}' contains invalid JSON.")
    exit(1)

TORBOX_ACCESS_TOKEN = config.get("torbox_access_token")
DOWNLOAD_DIR = pathlib.Path(config.get("download_dir", "/DATA/Downloads"))
FAKE_DOWNLOAD_DIR = pathlib.Path(config.get("fake_download_dir", "/downloads"))
POLL_INTERVAL = config.get("poll_interval_seconds", 15)
API_BASE_URL = config.get("api_base_url", "https://api.torbox.app")
API_VERSION = config.get("api_version", "v1")
SABNZBD_VERSION = "4.2.3" # Report a recent Sabnzbd version

if not TORBOX_ACCESS_TOKEN:
    print("ERROR: 'torbox_access_token' missing in config.json")
    exit(1)

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Data Structures ---
class JobStatus(Enum):
    QUEUED = "Queued"
    UPLOADING = "Uploading"
    CHECKING = "Checking" # Polling Torbox status
    DOWNLOADING = "Downloading" # Downloading from Torbox to local
    COMPLETED = "Completed"
    FAILED = "Failed"
    PAUSED = "Paused" # Optional, might be needed for full emulation

class DownloadJob:
    def __init__(self, internal_id: str, nzb_name: str, category: str, added_time: float):
        self.internal_id: str = internal_id
        self.nzb_name: str = nzb_name # Used for folder name (sanitized)
        self.display_name: str = nzb_name # Can be updated later if needed
        self.category: str = category
        self.status: JobStatus = JobStatus.QUEUED
        self.torbox_id: Optional[int] = None
        self.torbox_status: Optional[str] = None # e.g., 'processing', 'completed'
        self.torbox_files: List[Dict] = []
        self.total_size_bytes: int = 0
        self.downloaded_bytes: int = 0
        self.error_message: Optional[str] = None
        
        # New fields
        self.original_nzb_filename: Optional[str] = None # Store the original .nzb filename
        self._nzb_content: Optional[bytes] = None # Store NZB content for worker

        # Restore added_time
        self.added_time: float = added_time

        # Restore start_time
        self.start_time: Optional[float] = None

    def update_progress(self, downloaded_chunk_size: int):
        self.downloaded_bytes += downloaded_chunk_size
        if self.total_size_bytes > 0:
            self.downloaded_bytes = min(self.downloaded_bytes, self.total_size_bytes)

    def set_failed(self, message: str):
        self.status = JobStatus.FAILED
        self.error_message = message
        self.end_time = time.time() # Record end time on failure
        logger.error(f"Job {self.internal_id} ({self.nzb_name}) failed: {message}")

    def get_sab_status(self) -> str:
        if self.status == JobStatus.COMPLETED:
            return "Completed"
        elif self.status == JobStatus.FAILED:
            return "Failed"
        elif self.status == JobStatus.PAUSED:
            return "Paused"
        elif self.status in [JobStatus.QUEUED, JobStatus.UPLOADING, JobStatus.CHECKING]:
            return "Queued"
        elif self.status == JobStatus.DOWNLOADING:
            return "Downloading"
        return "Unknown"

    def get_sab_slot(self) -> Dict[str, Any]:
        mb = self.total_size_bytes / (1024 * 1024) if self.total_size_bytes else 0
        mbleft = max(0.0, (self.total_size_bytes - self.downloaded_bytes) / (1024 * 1024)) if self.total_size_bytes else 0
        percentage = (self.downloaded_bytes / self.total_size_bytes * 100) if self.total_size_bytes else 0

        return {
            "index": 0,
            "nzo_id": self.internal_id,
            "filename": self.display_name,
            "cat": self.category,
            "status": self.get_sab_status(),
            "mb": f"{mb:.1f}",
            "mbleft": f"{mbleft:.1f}",
            "size": f"{mb:.1f} MB",
            "sizeleft": f"{mbleft:.1f} MB",
            "percentage": f"{percentage:.0f}",
            "eta": "unknown",
            "priority": "Normal",
        }

    def get_sab_history_slot(self) -> Dict[str, Any]:
        final_status = self.get_sab_status()
        completion_time = int(self.end_time) if self.end_time else 0

        download_duration = 0
        if self.start_time and self.end_time and self.status == JobStatus.COMPLETED:
            download_duration = max(1, int(self.end_time - self.start_time))

        pp_status = "D" if final_status == "Completed" else ""

        storage_path = str(FAKE_DOWNLOAD_DIR / self.nzb_name)

        return {
            "completed": completion_time,
            "name": self.display_name,
            "nzb_name": self.original_nzb_filename or f"{self.nzb_name}.nzb",
            "category": self.category,
            "pp": pp_status,
            "status": final_status,
            "nzo_id": self.internal_id,
            "storage": storage_path,
            "path": storage_path,
            "download_time": download_duration,
            "downloaded": self.total_size_bytes,
            "fail_message": self.error_message or "",
            "bytes": self.total_size_bytes,
            "size": f"{self.total_size_bytes / (1024*1024):.1f} MB",
        }

# --- Global State ---
download_queue: Dict[str, DownloadJob] = {}
download_queue_order: List[str] = []  # Maintain order of jobs
queue_condition = asyncio.Condition()
worker_task: Optional[asyncio.Task] = None

sdk = torboxapi.Torbox(TORBOX_ACCESS_TOKEN)

# --- Helper Functions ---
def generate_nzo_id() -> str:
    return f"SABnzbd_nzo_{uuid.uuid4().hex[:10]}"

async def queue_worker():
    while True:
        async with queue_condition:
            while not download_queue_order:
                await queue_condition.wait()
            job_id = download_queue_order[0]
            job = download_queue.get(job_id)
        if job is None:
            async with queue_condition:
                download_queue_order.pop(0)
            continue
        if job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PAUSED]:
            async with queue_condition:
                download_queue_order.pop(0)
            continue
        try:
            await process_download(job, job._nzb_content)
        except Exception as e:
            job.set_failed(f"Worker error: {e}")
        async with queue_condition:
            download_queue_order.pop(0)

async def process_download(job: DownloadJob, nzb_content: bytes):
    logger.info(f"Starting processing for job {job.internal_id} ({job.nzb_name})")

    job.status = JobStatus.UPLOADING
    # Write the NZB content to a temporary file to avoid printing its contents in logs
    temp_nzb_path = DOWNLOAD_DIR / f"{job.nzb_name}.nzb"
    async with aiofiles.open(temp_nzb_path, "wb") as f:
        await f.write(nzb_content)

    logger.info(f"Uploading {job.nzb_name} to Torbox...")
    # Now pass the file path string instead of raw content
    job.torbox_id = sdk.create_usenet_download(nzb_content, job.nzb_name)

    job.status = JobStatus.CHECKING
    logger.info(f"Waiting for Torbox to download to their servers...")
    while True:
        await asyncio.sleep(2)
        if sdk.is_usenet_download_ready(job.torbox_id):
            break

    logger.info(f"Getting file list...")
    job.torbox_files = sdk.get_usenet_download_file_list(job.torbox_id)

    if not job.torbox_files:
        job.set_failed("Torbox completed but provided no file list.")
        return

    job.status = JobStatus.DOWNLOADING
    job.start_time = time.time()  # Set start_time when download begins
    download_path = DOWNLOAD_DIR / job.nzb_name
    download_path.mkdir(parents=True, exist_ok=True)

    logger.info(f"Starting local download for job {job.internal_id} to {download_path}")
    total_files = len(job.torbox_files)
    job.downloaded_bytes = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), follow_redirects=True) as download_client:
        for i, file_info in enumerate(job.torbox_files):
            file_id = file_info.get("id")
            short_name = file_info.get("short_name")
            file_size = file_info.get("size", 0)

            safe_short_name = os.path.basename(short_name)

            local_file_path = download_path / safe_short_name
            logger.info(f"Downloading file {i+1}/{total_files}: '{safe_short_name}' ({file_size} bytes) to {local_file_path} with id {file_id}")

            retries = 3
            while retries > 0:
                try:
                    download_url = sdk.get_usenet_file_download_link(job.torbox_id, file_id)
                    if not isinstance(download_url, str) or not download_url.startswith("http"):
                        raise RuntimeError(f"Invalid download URL received for file {file_id}: {download_url}")

                    logger.debug(f"Got download URL for {safe_short_name}: {download_url[:60]}...")

                    async with download_client.stream("GET", download_url) as response:
                        response.raise_for_status()
                        async with aiofiles.open(local_file_path, "wb") as f:
                            bytes_downloaded_this_file = 0
                            async for chunk in response.aiter_bytes(chunk_size=8192*4):
                                await f.write(chunk)
                                bytes_downloaded_this_file += len(chunk)
                                job.update_progress(len(chunk))

                    logger.info(f"Successfully downloaded {safe_short_name}")
                    await asyncio.sleep(0.01)
                    break

                except Exception as e:
                    retries -= 1
                    logger.error(f"Error downloading file '{safe_short_name}' (Job {job.internal_id}): {e}. Retries left: {retries}")
                    if await asyncio.to_thread(local_file_path.exists):
                        try:
                            await asyncio.to_thread(local_file_path.unlink)
                        except OSError as unlink_err:
                            logger.warning(f"Could not remove partial file {local_file_path}: {unlink_err}")
                    if retries == 0:
                        job.set_failed(f"Failed to download file '{safe_short_name}' after multiple attempts: {e}")
                        await download_client.aclose()
                        return
                    await asyncio.sleep(5)

    job.status = JobStatus.COMPLETED
    job.end_time = time.time()
    if job.total_size_bytes > 0:
        job.downloaded_bytes = job.total_size_bytes
    logger.info(f"All files downloaded successfully for job {job.internal_id} ({job.nzb_name}) at {job.end_time}")

# --- FastAPI Application ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup...")
    try:
        await asyncio.to_thread(DOWNLOAD_DIR.mkdir, parents=True, exist_ok=True)
        logger.info(f"Download directory configured: {DOWNLOAD_DIR}")
    except OSError as e:
        logger.error(f"FATAL: Could not create download directory {DOWNLOAD_DIR}: {e}")

    global worker_task
    worker_task = asyncio.create_task(queue_worker())

    yield

    logger.info("Shutting down...")
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutdown complete.")

app = FastAPI(lifespan=lifespan)

@app.api_route("/api", methods=["GET", "POST"])
async def api_handler(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")

    logger.debug(f"Received request: mode={mode}, params={params}")

    if mode == "version":
        return {"version": SABNZBD_VERSION}

    elif mode == "get_config":
        try:
            with open('configsab.json') as f:
                data = json.load(f)
            return data
        except Exception as e:
            logger.error(f"Could not load configsab.json: {e}")

    elif mode == "addfile":
        try:
            form = await request.form()
            nzb_upload: Optional[UploadFile] = form.get("name")
            nzb_name = params.get("nzbname", None)
            category = params.get("cat", "*")

            if not nzb_upload or not nzb_upload.filename:
                logger.error("addfile request missing NZB file.")
                return JSONResponse(status_code=400, content={"status": False, "error": "NZB file required"})

            original_filename = nzb_upload.filename

            if not nzb_name:
                nzb_name = pathlib.Path(original_filename).stem
                logger.warning(f"No nzbname parameter provided, using filename stem: {nzb_name}")

            invalid_chars = '<>:"/\\|?*'
            final_nzb_name = "".join(c if c.isalnum() or c in (' ', '.', '_', '-') and c not in invalid_chars else '_' for c in nzb_name)
            final_nzb_name = "_".join(final_nzb_name.split())
            if not final_nzb_name:
                final_nzb_name = f"download_{uuid.uuid4().hex[:6]}"

            nzb_content = await nzb_upload.read()
            await nzb_upload.close()

            internal_id = generate_nzo_id()
            job = DownloadJob(
                internal_id=internal_id,
                nzb_name=final_nzb_name,
                category=category,
                added_time=time.time()
            )
            job.display_name = final_nzb_name
            job.original_nzb_filename = original_filename
            job._nzb_content = nzb_content  # Store content for worker

            download_queue[internal_id] = job
            async with queue_condition:
                download_queue_order.append(internal_id)
                queue_condition.notify()
            logger.info(f"Queued job {internal_id} for NZB '{final_nzb_name}', category '{category}'")

            return {"status": True, "nzo_ids": [internal_id]}

        except Exception as e:
            logger.exception("Error processing addfile request")
            return JSONResponse(status_code=500, content={"status": False, "error": f"Internal Server Error: {e}"})

    elif mode == "queue":
        slots = []
        paused = False
        queue_speed = "0.0 K"
        total_mb = 0
        remaining_mb = 0

        current_jobs = list(download_queue.values())
        sorted_jobs = sorted(current_jobs, key=lambda j: j.added_time)
        active_jobs = [j for j in sorted_jobs if j.status not in [JobStatus.COMPLETED, JobStatus.FAILED]]
        
        # If asking to delete files
        if params.get('name') == 'delete':
            downloadid = params.get('value')
            if downloadid in download_queue:
                # Delete the folder
                folder_path = DOWNLOAD_DIR / download_queue[downloadid].nzb_name
                shutil.rmtree(folder_path, ignore_errors=True)
                download_queue.pop(downloadid)
                async with queue_condition:
                    if downloadid in download_queue_order:
                        download_queue_order.remove(downloadid)
                logger.info(f"Deleted folder {folder_path} for job {downloadid}")
                
                # Remove the job from the active tasks
                return {"status": True, "nzo_ids": [downloadid]}
            else:
                logger.warning(f"Job {downloadid} not found in queue for deletion.")
                return {"status": False, "nzo_ids": []}

        for index, job in enumerate(active_jobs):
            slot = job.get_sab_slot()
            slot["index"] = index
            slots.append(slot)
            try:
                mb_val = float(slot.get('mb', 0))
                mbleft_val = float(slot.get('mbleft', 0))
                total_mb += mb_val
                remaining_mb += mbleft_val
            except (ValueError, TypeError):
                logger.warning(f"Could not parse mb/mbleft for job {job.internal_id}")
                pass

        queue_status = "Idle"
        if any(j.status == JobStatus.DOWNLOADING for j in active_jobs):
            queue_status = "Downloading"
        elif any(j.status == JobStatus.CHECKING for j in active_jobs):
            queue_status = "Checking"
        elif any(j.status == JobStatus.UPLOADING for j in active_jobs):
            queue_status = "Uploading"
        elif active_jobs:
            queue_status = "Queued"

        if any(j.status == JobStatus.PAUSED for j in active_jobs):
            paused = True
            queue_status = "Paused"

        return {
            "queue": {
                "slots": slots,
                "paused": paused,
                "paused_all": paused,
                "noofslots": len(slots),
                "mb": f"{total_mb:.1f}",
                "mbleft": f"{remaining_mb:.1f}",
                "timeleft": "0:00:00",
                "speed": queue_speed,
                "limit": 0.0,
                "limit_abs": "",
                "start": 0,
                "status": queue_status,
                "finish": 0,
                "version": SABNZBD_VERSION,
                "categories": ["movies", "tv", "music", "books", "*"],
                "isverbose": False,
                "total_size": f"{total_mb:.1f} MB",
                "month_size": "0 B",
                "week_size": "0 B",
                "day_size": "0 B",
            }
        }

    elif mode == "history":
        slots = []
        total_size_gb = 0
        noofslots = 0

        current_jobs = list(download_queue.values())
        sorted_jobs = sorted(current_jobs, key=lambda j: j.added_time, reverse=True)
        completed_failed_jobs = [j for j in sorted_jobs if j.status in [JobStatus.COMPLETED, JobStatus.FAILED]]

        for job in completed_failed_jobs:
            slot = job.get_sab_history_slot()
            slots.append(slot)
            noofslots += 1
            try:
                total_size_gb += job.total_size_bytes / (1024 * 1024 * 1024)
            except ValueError:
                pass

        return {
            "history": {
                "total_size": f"{total_size_gb:.1f} G",
                "month_size": "0 G",
                "week_size": "0 G",
                "day_size": "0 M",
                "slots": slots,
                "noofslots": noofslots,
                "last_history_update": int(time.time()),
                "version": SABNZBD_VERSION,
            }
        }

    elif mode == "pause.queue":
        count = 0
        for job in download_queue.values():
            if job.status not in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PAUSED]:
                job.status = JobStatus.PAUSED
                count += 1
        logger.warning(f"Pause queue requested - marked {count} active jobs as Paused.")
        return {"status": True}

    elif mode == "resume.queue":
        count = 0
        for job in download_queue.values():
            if job.status == JobStatus.PAUSED:
                job.status = JobStatus.CHECKING if job.torbox_id else JobStatus.QUEUED
                count += 1
        logger.warning(f"Resume queue requested - marked {count} paused jobs to resume.")
        return {"status": True}

    elif mode == "auth":
        client_host = request.client.host if request.client else "Unknown"
        logger.info(f"Auth check requested by {client_host}")
        return JSONResponse(content="ok")

    else:
        logger.warning(f"Unhandled API mode requested: {mode}")
        return JSONResponse(status_code=404, content={"status": False, "error": f"Mode '{mode}' not implemented"})

    return JSONResponse(status_code=500, content={"status": False, "error": "Internal server error: Unhandled request path"})


if __name__ == "__main__":
    logger.info(f"Starting SABnzbd Emulator for Torbox on port 8000")
    logger.info(f"Download directory: {DOWNLOAD_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=8000, lifespan="on")