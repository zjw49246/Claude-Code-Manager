import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/files", tags=["files"])

MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB (for reading)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB (for uploading)
MAX_UPLOAD_FILES = 10


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


@router.post("/upload")
async def upload_to_directory(
    target_dir: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Upload files to a specific directory on the server."""
    target = Path(target_dir).expanduser().resolve()
    if not target.exists():
        raise HTTPException(400, f"Directory not found: {target_dir}")
    if not target.is_dir():
        raise HTTPException(400, f"Not a directory: {target_dir}")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"Maximum {MAX_UPLOAD_FILES} files per request")

    results = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_UPLOAD_SIZE:
            raise HTTPException(400, f"File '{f.filename}' exceeds 50 MB limit")
        save_path = target / (f.filename or "upload")
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            counter = 1
            while save_path.exists():
                save_path = target / f"{stem}_{counter}{suffix}"
                counter += 1
        save_path.write_bytes(data)
        results.append({
            "name": save_path.name,
            "path": str(save_path),
            "size": len(data),
        })
    return results


MAX_DIFF_SIZE = 2 * 1024 * 1024  # 2 MB max diff output


async def _run_git(
    repo_path: str, *args: str,
    max_output: int = MAX_DIFF_SIZE,
    allow_nonzero: bool = False,
) -> str:
    """Run a git command in repo_path and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0 and not allow_nonzero:
        err_msg = stderr.decode(errors="replace").strip()
        raise HTTPException(400, f"git error: {err_msg}")
    output = stdout[:max_output].decode(errors="replace")
    return output


@router.get("/git/status")
async def git_status(path: str = Query(..., description="Git repository root path")):
    """Get git status (changed files) for a repository."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise HTTPException(404, f"Path not found: {path}")
    if not (target / ".git").exists() and not (target / ".git").is_file():
        raise HTTPException(400, f"Not a git repository: {path}")

    raw = await _run_git(str(target), "status", "--porcelain=v1", "-uall")
    files = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        x, y = line[0], line[1]
        filepath = line[3:]
        if " -> " in filepath:
            filepath = filepath.split(" -> ", 1)[1]
        if x == "?" and y == "?":
            status = "untracked"
        elif x in ("A", " ") and y == " ":
            status = "added" if x == "A" else "clean"
        elif x == "D" or y == "D":
            status = "deleted"
        elif x == "M" or y == "M":
            status = "modified"
        elif x == "R":
            status = "renamed"
        else:
            status = "modified"
        if status != "clean":
            files.append({"path": filepath, "status": status, "x": x, "y": y})

    branch = (await _run_git(str(target), "branch", "--show-current")).strip()
    return {"path": str(target), "branch": branch, "files": files}


async def _untracked_diff(repo_path: str, files: list[str]) -> str:
    """Generate diff-like output for untracked files using git diff --no-index."""
    parts = []
    for f in files:
        out = await _run_git(
            repo_path, "diff", "--no-index", "--no-color", "/dev/null", f,
            allow_nonzero=True,
        )
        if out.strip():
            parts.append(out)
    return "\n".join(parts)


async def _get_untracked_files(repo_path: str) -> list[str]:
    """List untracked files in the repo."""
    raw = await _run_git(repo_path, "ls-files", "--others", "--exclude-standard")
    return [f for f in raw.splitlines() if f.strip()]


@router.get("/git/diff")
async def git_diff(
    path: str = Query(..., description="Git repository root path"),
    file: Optional[str] = Query(None, description="Specific file to diff"),
    staged: bool = Query(False, description="Show staged changes"),
):
    """Get git diff output for a repository or specific file."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise HTTPException(404, f"Path not found: {path}")

    repo = str(target)

    if file:
        untracked = await _get_untracked_files(repo)
        if file in untracked:
            diff_output = await _untracked_diff(repo, [file])
        else:
            args = ["diff"]
            if staged:
                args.append("--cached")
            args.extend(["--no-color", "--", file])
            diff_output = await _run_git(repo, *args)
    else:
        args = ["diff"]
        if staged:
            args.append("--cached")
        args.append("--no-color")
        diff_output = await _run_git(repo, *args)

        if not staged:
            untracked = await _get_untracked_files(repo)
            if untracked:
                ut_diff = await _untracked_diff(repo, untracked[:50])
                if ut_diff:
                    diff_output = diff_output + ("\n" if diff_output else "") + ut_diff

    return {"path": repo, "diff": diff_output, "file": file, "staged": staged}


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
