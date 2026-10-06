"""Media upload planning and the three-request upload (HAR c50): stoken -> stream PUT -> attach POST."""
from __future__ import annotations
import hashlib
from pathlib import Path

NAMESPACE = "1093"  # as captured
MAX_BYTES = 25 * 1024 * 1024
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def _file(path):
    p = Path(path or "")
    if not str(path or "").strip() or not p.is_absolute() or ".." in p.parts:
        raise ValueError("file must be an absolute path")
    p = p.resolve()
    if p.suffix.lower() not in MIME or not p.is_file() or p.is_symlink() or not 0 < p.stat().st_size <= MAX_BYTES:
        raise ValueError("unsupported or unreadable image")
    return p


def media_details(path):
    """-> (bytes, sha256, mime, width, height, extension) for a local image."""
    p = _file(path)
    data = p.read_bytes()
    from PIL import Image
    import io
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    return data, hashlib.sha256(data).hexdigest(), MIME[p.suffix.lower()], width, height, p.suffix.lower().lstrip(".")


def attach_body(media_id, title, mime, width, height, size, ext):
    # Values copied from the UI capture: fileExtension was "jpg" even for a png, fileType "p", attachAsPrimary "".
    return [{"id": media_id, "name": title, "mimeType": mime, "attachAsPrimary": "",
             "additionalFileDetails": {"fileExtension": "jpg", "fileHeight": height, "fileHWidth": width,
                                       "fileSize": size, "fileSubtype": ext, "fileType": "p"}}]


def media_upload_plan(tree_id, person_id, file, title):
    """Dry-run plan: validates the file and returns the final attach request (no bytes sent)."""
    from .ops import WriteRequestError
    try:
        data, sha, mime, w, h, ext = media_details(file)
    except (ValueError, OSError, ImportError) as exc:
        raise WriteRequestError("invalid-media-file", [{"field": "file", "issue": "invalid", "expected": "an absolute path to a png, jpg, gif or webp file up to 25 MB"}]) from exc
    if not title or len(title) > 200:
        raise WriteRequestError("invalid-write-request")
    return dict(method="POST", path=f"/api/media/upload/mapi/tree/{tree_id}/person/{person_id}/media",
                body=attach_body("<mediaId>", title, mime, w, h, len(data), ext),
                upload={"sha256": sha, "bytes": len(data), "file": Path(file).name})
