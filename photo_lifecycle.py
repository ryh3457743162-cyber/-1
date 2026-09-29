"""Serialize photo/comment/book changes and recover JSON commits, without SQL."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

_mutex = threading.RLock()


def atomic_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if sys.platform != 'win32':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(photos_file: Path):
    """One cross-process lock for all related HTTP reads/writes."""
    with _mutex:
        path = photos_file.with_suffix('.lifecycle.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as handle:
            if sys.platform == 'win32':
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b'\0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == 'win32':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def recover(photos_file: Path, book_file: Path, comments_file: Path) -> None:
    """Roll forward an already prepared purge; callers must hold locked()."""
    journal = photos_file.with_suffix('.purge-journal.json')
    if not journal.exists():
        return
    value = json.loads(journal.read_text(encoding='utf-8'))
    if set(value) != {'photos', 'book', 'comments'}:
        raise ValueError('Invalid purge journal')
    # Fixed destinations: never trust file paths embedded in a journal.
    atomic_write(comments_file, value['comments'])
    if value['book'] is not None:
        atomic_write(book_file, value['book'])
    atomic_write(photos_file, value['photos'])
    journal.unlink()


def commit(photos_file: Path, book_file: Path, comments_file: Path,
           photos: dict, book: dict | None, comments: list) -> None:
    """Durable intent before touching any of the three metadata files."""
    atomic_write(photos_file.with_suffix('.purge-journal.json'),
                 {'photos': photos, 'book': book, 'comments': comments})
    recover(photos_file, book_file, comments_file)
