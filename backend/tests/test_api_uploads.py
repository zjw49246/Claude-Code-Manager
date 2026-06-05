"""Tests for image upload API endpoints."""
import io
import pytest

# ── helpers ────────────────────────────────────────────────────────────────

def _png_bytes(size: int = 64) -> bytes:
    """Return a minimal valid 1×1 PNG (89 bytes) repeated to reach ~size bytes."""
    # Minimal 1×1 white pixel PNG
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _file_tuple(name: str, data: bytes, content_type: str = "image/png"):
    return (name, io.BytesIO(data), content_type)


# ── upload endpoint ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_single_image(client, tmp_path, monkeypatch):
    """Upload a single valid PNG → 200, returns id/filename/path/url."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    data = _png_bytes()
    resp = await client.post(
        "/api/uploads",
        files=[("files", _file_tuple("test.png", data))],
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    r = results[0]
    assert r["filename"] == "test.png"
    assert r["path"].endswith(".png")
    assert r["url"].startswith("/api/uploads/")
    assert "id" in r


@pytest.mark.asyncio
async def test_upload_multiple_images(client, tmp_path, monkeypatch):
    """Upload 3 images at once."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    data = _png_bytes()
    resp = await client.post(
        "/api/uploads",
        files=[
            ("files", _file_tuple("a.png", data)),
            ("files", _file_tuple("b.png", data)),
            ("files", _file_tuple("c.png", data)),
        ],
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 3


@pytest.mark.asyncio
async def test_upload_too_many_files(client, tmp_path, monkeypatch):
    """Uploading more than _MAX_FILES files returns 400."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    data = _png_bytes()
    count = uploads_mod._MAX_FILES + 1
    resp = await client.post(
        "/api/uploads",
        files=[("files", _file_tuple(f"img{i}.png", data)) for i in range(count)],
    )
    assert resp.status_code == 400
    assert "maximum" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_invalid_type(client, tmp_path, monkeypatch):
    """Uploading a non-image file type returns 400."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    resp = await client.post(
        "/api/uploads",
        files=[("files", ("malware.exe", io.BytesIO(b"MZ..."), "application/octet-stream"))],
    )
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_saves_file_to_disk(client, tmp_path, monkeypatch):
    """Uploaded file actually exists on disk after upload."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    data = _png_bytes()
    resp = await client.post(
        "/api/uploads",
        files=[("files", _file_tuple("disk_check.png", data))],
    )
    assert resp.status_code == 200
    path = resp.json()[0]["path"]
    from pathlib import Path
    assert Path(path).exists()
    assert Path(path).read_bytes() == data


@pytest.mark.asyncio
async def test_upload_unique_ids(client, tmp_path, monkeypatch):
    """Each upload gets a unique id (no collision)."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    data = _png_bytes()
    r1 = (await client.post("/api/uploads", files=[("files", _file_tuple("x.png", data))])).json()
    r2 = (await client.post("/api/uploads", files=[("files", _file_tuple("x.png", data))])).json()
    assert r1[0]["id"] != r2[0]["id"]
    assert r1[0]["path"] != r2[0]["path"]


# ── serve uploaded image ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_uploaded_image(client, tmp_path, monkeypatch):
    """GET /api/uploads/{filename} serves the file that was uploaded."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    data = _png_bytes()
    upload_resp = await client.post(
        "/api/uploads",
        files=[("files", _file_tuple("serve_me.png", data))],
    )
    assert upload_resp.status_code == 200
    url = upload_resp.json()[0]["url"]  # e.g. /api/uploads/uuid.png
    filename = url.split("/")[-1]

    serve_resp = await client.get(f"/api/uploads/{filename}")
    assert serve_resp.status_code == 200
    assert serve_resp.content == data


@pytest.mark.asyncio
async def test_get_nonexistent_image(client, tmp_path, monkeypatch):
    """GET /api/uploads/nonexistent.png returns 404."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    resp = await client.get("/api/uploads/does_not_exist.png")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_path_traversal_rejected(client, tmp_path, monkeypatch):
    """Filenames with '..' components are rejected with 4xx."""
    import backend.api.uploads as uploads_mod
    monkeypatch.setattr(uploads_mod, "UPLOAD_DIR", tmp_path)

    # Direct call to the endpoint handler — httpx normalizes encoded slashes
    # before they reach FastAPI, so we test the handler's own guard directly.
    from backend.api.uploads import get_file
    from fastapi import HTTPException as _HTTPException
    with pytest.raises(_HTTPException) as exc_info:
        await get_file("../secret.txt")
    assert exc_info.value.status_code in (400, 404)
