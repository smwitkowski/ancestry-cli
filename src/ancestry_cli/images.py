"""Save a record image (read-only), optionally cropped: `ancestry read image --collection C --image I --out FILE`."""
from __future__ import annotations

import io

from . import runtime as rt
from .runtime import failure


def _crop_box(spec, width, height):
    """`x,y,w,h` in pixels of the saved image, or all fractions between 0 and 1 of its width and height."""
    x, y, w, h = (float(v) for v in spec.split(","))
    if max(x, y, w, h) <= 1:
        x, y, w, h = x * width, y * height, w * width, h * height
    box = (round(x), round(y), round(x + w), round(y + h))
    if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
        raise ValueError("crop")
    return box


def save(*, collection, image_id, out, record_id=None, crop=None, scale=1, bridge=None):
    if not (collection and (image_id or record_id) and out):
        return failure("missing-arguments")
    if not image_id:            # the record's own image id, from its hover card
        from . import reads
        card = reads.read(action="record", collection=collection, record_id=record_id, full=True, bridge=bridge)
        body = card.get("body") if card.get("ok") else None
        image_id = (body or {}).get("imageId") or next(iter((body or {}).get("imageIds") or []), None)
        if not image_id:
            return failure("image-unavailable")
    from pathlib import Path
    dest = Path(out)
    if dest.exists() or not dest.parent.is_dir():
        return failure("configuration-error", problems=[{"field": "out", "issue": "invalid",
                                                           "expected": "a new file in an existing folder"}])
    try:
        box_check = crop and [float(v) for v in crop.split(",")]
        if crop and len(box_check) != 4:
            raise ValueError
    except ValueError:
        return failure("configuration-error", problems=[{"field": "crop", "issue": "invalid", "expected": "x,y,w,h"}])
    bridge = bridge or rt.load_bridge()
    try:
        with rt.quiet(), rt.lock("ancestry"):
            lease = rt.open_lane(bridge)
            lease.check()
            status, ctype, data = bridge.fetch_record_image(lease.base, lease.target_id, collection, image_id, record_id, scale)
    except rt.LaneError as exc:
        return failure(exc.code, **exc.detail)
    except Exception as exc:
        return failure(rt.classify_exception(exc) or "image-unavailable")
    if status != 200 or not ctype.startswith("image/") or not data:
        return failure("image-unavailable", status=status)
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    size = img.size
    if crop:
        try:
            img = img.crop(_crop_box(crop, *size))
        except ValueError:
            return failure("configuration-error", problems=[{"field": "crop", "issue": "invalid",
                                                               "expected": f"a box inside the {size[0]}x{size[1]} image"}])
        img.save(dest, format="PNG" if dest.suffix.lower() == ".png" else "JPEG", quality=95)
    else:
        dest.write_bytes(data)
    dest.chmod(0o600)
    return {"ok": True, "classification": "image", "dispatch_attempted": True, "state": "unchanged", "file": str(dest),
            "image_size": list(size), "saved_size": list(img.size), "bytes": dest.stat().st_size}
