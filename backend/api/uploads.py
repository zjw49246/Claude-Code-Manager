import asyncio
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# Project root / uploads
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = _PROJECT_ROOT / "uploads"

_CLEANUP_MAX_AGE_DAYS = 15
_CLEANUP_INTERVAL_HOURS = 24
_BLOCKED_EXTENSIONS = {".exe"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
_MAX_FILES = 10


def _get_upload_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


@router.post("")
async def upload_files(files: list[UploadFile] = File(...)):
    """Upload up to 5 files. Returns list of {id, filename, path, url, is_image}."""
    if len(files) > _MAX_FILES:
        raise HTTPException(400, f"Maximum {_MAX_FILES} files allowed per request")

    results = []
    for file in files:
        ext = Path(file.filename or "file").suffix.lower()
        if ext in _BLOCKED_EXTENSIONS:
            raise HTTPException(400, f"File type '{ext}' is not allowed")

        data = await file.read()
        if len(data) > _MAX_SIZE_BYTES:
            raise HTTPException(400, f"File '{file.filename}' exceeds 50 MB limit")

        file_id = str(uuid.uuid4())
        saved_name = f"{file_id}{ext}" if ext else file_id

        save_path = _get_upload_dir() / saved_name
        save_path.write_bytes(data)

        results.append(
            {
                "id": file_id,
                "filename": file.filename,
                "path": str(save_path.resolve()),
                "url": f"/api/uploads/{saved_name}",
                "is_image": ext in _IMAGE_EXTENSIONS,
            }
        )

    return results


@router.get("/{filename}")
async def get_file(filename: str):
    """Serve an uploaded file."""
    upload_dir = _get_upload_dir()
    file_path = upload_dir / filename

    # Prevent path traversal
    if not str(file_path.resolve()).startswith(str(upload_dir.resolve())):
        raise HTTPException(400, "Invalid filename")
    if not file_path.exists():
        raise HTTPException(404, "File not found")

    return FileResponse(str(file_path))


def cleanup_expired_uploads() -> int:
    """Delete files in UPLOAD_DIR older than _CLEANUP_MAX_AGE_DAYS. Returns count deleted."""
    if not UPLOAD_DIR.is_dir():
        return 0
    cutoff = time.time() - _CLEANUP_MAX_AGE_DAYS * 86400
    deleted = 0
    for f in UPLOAD_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            deleted += 1
    return deleted


async def start_upload_cleanup_loop() -> asyncio.Task:
    """Start a background loop that cleans expired uploads every 24 hours."""

    async def _loop():
        while True:
            try:
                deleted = await asyncio.to_thread(cleanup_expired_uploads)
                if deleted:
                    logger.info("Upload cleanup: deleted %d expired file(s)", deleted)
            except Exception:
                logger.exception("Upload cleanup error")
            await asyncio.sleep(_CLEANUP_INTERVAL_HOURS * 3600)

    return asyncio.create_task(_loop())
