import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# Project root / uploads
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = _PROJECT_ROOT / "uploads"

_BLOCKED_EXTENSIONS = {".exe", ".zip"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_FILES = 5


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
            raise HTTPException(400, f"File '{file.filename}' exceeds 10 MB limit")

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
