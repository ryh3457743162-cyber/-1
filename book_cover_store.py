"""One independent album cover; never changes photo or body-page metadata."""

import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "book-cover.json"
LOCK_FILE = DATA_FILE.with_suffix(".lock")
_thread_lock = threading.RLock()
DEFAULT = {
    "title": "凡凡 land 涵涵", "subtitle": "我们的故事，从这里开始",
    "dateText": "2026", "footerText": "把喜欢的瞬间，慢慢装订成一本书。",
    "photoId": None, "layout": "classic", "titleAlign": "center",
    "photoPosition": "center", "showPhoto": False,
}


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("封面配置不正确")
    result = {}
    for field, maximum in (("title", 120), ("subtitle", 200), ("dateText", 80), ("footerText", 300)):
        text = value.get(field, DEFAULT[field])
        if not isinstance(text, str) or len(text.strip()) > maximum:
            raise ValueError(f"封面文字过长或格式不正确（{maximum} 字以内）")
        result[field] = text.strip()
    for field, choices in (("layout", ("classic", "photo", "minimal", "polaroid")),
                           ("titleAlign", ("left", "center", "right")),
                           ("photoPosition", ("top", "center", "bottom"))):
        choice = value.get(field, DEFAULT[field])
        if not isinstance(choice, str) or choice not in choices:
            raise ValueError("封面排版选项不正确")
        result[field] = choice
    photo_id = value.get("photoId")
    if photo_id is not None and (not isinstance(photo_id, str) or not 1 <= len(photo_id) <= 160):
        raise ValueError("封面照片不正确")
    result["photoId"] = photo_id
    show = value.get("showPhoto", bool(photo_id))
    if not isinstance(show, bool):
        raise ValueError("照片显示选项不正确")
    result["showPhoto"] = show
    return result


def read():
    if not DATA_FILE.exists():
        return dict(DEFAULT)
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        result = validate(value)
        if isinstance(value.get("updatedAt"), str):
            result["updatedAt"] = value["updatedAt"]
        return result
    except (OSError, ValueError, TypeError):
        # A cover is optional; damaged configuration must not block the album.
        return dict(DEFAULT)


@contextmanager
def _locked():
    with _thread_lock:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_FILE.open("a+b") as handle:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save(value):
    result = validate(value)
    result["updatedAt"] = datetime.now(timezone.utc).isoformat()
    with _locked():
        temporary = DATA_FILE.with_name(f".{DATA_FILE.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, DATA_FILE)
        finally:
            temporary.unlink(missing_ok=True)
    return result
