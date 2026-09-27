"""Shared photo comments, stored separately from photos and book layouts."""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path


DATA_FILE = Path(__file__).resolve().parent / "data" / "comments.json"
LOCK_FILE = DATA_FILE.with_suffix(".lock")


def read() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("Invalid comment metadata")
    for item in value:
        if isinstance(item, dict) and item.get("status") in (None, "visible"):
            item["status"] = "approved"
    return value


@contextmanager
def _locked():
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


def update(change):
    """Apply a change to the latest data with a cross-process lock."""
    with _locked():
        value = read()
        result = change(value)
        temporary = DATA_FILE.with_name(f".{DATA_FILE.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, DATA_FILE)
        finally:
            temporary.unlink(missing_ok=True)
        return result
