import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/files", tags=["files"])

MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

class SSHCreds(BaseModel):
    host: str
    port: int = 22
    username: str
    password: Optional[str] = None
    key_path: Optional[str] = None  # path to private key on the backend machine


class SSHListRequest(SSHCreds):
    path: str


class SSHReadRequest(SSHCreds):
    path: str


def _make_ssh_client(creds: SSHCreds):
    try:
        import paramiko
    except ImportError:
        raise HTTPException(status_code=500, detail="paramiko is not installed")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs: dict = {
            "hostname": creds.host,
            "port": creds.port,
            "username": creds.username,
            "timeout": 10,
        }
        key_path = os.path.expanduser(creds.key_path) if creds.key_path else None
        if key_path and os.path.isfile(key_path):
            connect_kwargs["key_filename"] = key_path
        elif creds.password:
            connect_kwargs["password"] = creds.password
        client.connect(**connect_kwargs)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"SSH connection failed: {e}")
    return client


def _safe_path(path: str) -> Path:
    """Resolve path and guard against empty input."""
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="path is required")
    resolved = Path(path).expanduser().resolve()
    return resolved


@router.get("/list")
async def list_directory(path: str = Query(..., description="Absolute directory path")):
    """List contents of a directory."""
    target = _safe_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    entries = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "size": stat.st_size if entry.is_file() else None,
                })
            except OSError:
                pass  # skip unreadable entries
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {"path": str(target), "entries": entries}


@router.get("/read")
async def read_file(path: str = Query(..., description="Absolute file path")):
    """Read a file's content (max 1 MB)."""
    target = _safe_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    size = target.stat().st_size
    if size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size // 1024} KB). Max is {MAX_FILE_SIZE // 1024} KB.",
        )

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {"path": str(target), "content": content, "size": size}


MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


@router.get("/download")
async def download_file(path: str = Query(..., description="Absolute file path")):
    """Download a file (max 100 MB)."""
    target = _safe_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    size = target.stat().st_size
    if size > MAX_DOWNLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size // 1024 // 1024} MB). Max is {MAX_DOWNLOAD_SIZE // 1024 // 1024} MB.",
        )

    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# SSH endpoints
# ---------------------------------------------------------------------------

@router.post("/ssh/list")
async def ssh_list_directory(req: SSHListRequest):
    """List contents of a directory on a remote SSH server."""
    client = _make_ssh_client(req)
    try:
        sftp = client.open_sftp()
        try:
            attrs = sftp.listdir_attr(req.path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")

        import stat as stat_mod
        entries = []
        for a in sorted(attrs, key=lambda e: (not stat_mod.S_ISDIR(e.st_mode or 0), (e.filename or '').lower())):
            is_dir = stat_mod.S_ISDIR(a.st_mode or 0)
            entries.append({
                "name": a.filename,
                "path": req.path.rstrip("/") + "/" + a.filename,
                "is_dir": is_dir,
                "size": a.st_size if not is_dir else None,
            })
        sftp.close()
    finally:
        client.close()

    return {"path": req.path, "entries": entries}


@router.post("/ssh/read")
async def ssh_read_file(req: SSHReadRequest):
    """Read a file from a remote SSH server (max 1 MB)."""
    client = _make_ssh_client(req)
    try:
        sftp = client.open_sftp()
        try:
            file_attr = sftp.stat(req.path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

        size = file_attr.st_size or 0
        if size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({size // 1024} KB). Max is {MAX_FILE_SIZE // 1024} KB.",
            )

        try:
            with sftp.open(req.path, "r") as f:
                raw = f.read()
            content = raw.decode("utf-8", errors="replace")
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        finally:
            sftp.close()
    finally:
        client.close()

    return {"path": req.path, "content": content, "size": size}


@router.post("/ssh/download")
async def ssh_download_file(req: SSHReadRequest):
    """Download a file from a remote SSH server (max 100 MB)."""
    client = _make_ssh_client(req)
    try:
        sftp = client.open_sftp()
        try:
            file_attr = sftp.stat(req.path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

        size = file_attr.st_size or 0
        if size > MAX_DOWNLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({size // 1024 // 1024} MB). Max is {MAX_DOWNLOAD_SIZE // 1024 // 1024} MB.",
            )

        filename = req.path.rstrip("/").rsplit("/", 1)[-1] or "download"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}")
        try:
            sftp.getfo(req.path, tmp)
            tmp.close()
        except PermissionError:
            os.unlink(tmp.name)
            raise HTTPException(status_code=403, detail="Permission denied")
        finally:
            sftp.close()
    finally:
        client.close()

    return FileResponse(
        path=tmp.name,
        filename=filename,
        media_type="application/octet-stream",
        background=None,
    )
