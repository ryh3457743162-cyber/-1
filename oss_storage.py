"""Private OSS storage for newly published photos.

This module deliberately does not read or modify legacy local uploads.
The SDK is imported only when OSS is used, so an unconfigured site still starts.
"""

import os
from functools import lru_cache


class OssNotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return all(os.environ.get(name) for name in (
        "OSS_REGION", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"
    ))


@lru_cache(maxsize=1)
def _bucket():
    if not configured():
        raise OssNotConfigured("OSS is not configured")
    import oss2

    region = os.environ["OSS_REGION"]
    endpoint = os.environ.get("OSS_ENDPOINT") or f"https://oss-{region}-internal.aliyuncs.com"
    if not endpoint.startswith("https://"):
        raise OssNotConfigured("OSS_ENDPOINT must use HTTPS")
    auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, endpoint, os.environ["OSS_BUCKET"], connect_timeout=10)


def put(key: str, content: bytes, content_type: str) -> None:
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "public, max-age=3600",
        "x-oss-forbid-overwrite": "true",
    }
    result = _bucket().put_object(key, content, headers=headers)
    if result.status != 200:
        raise RuntimeError("OSS upload failed")


def get(key: str) -> bytes:
    return _bucket().get_object(key).read()


def size(key: str) -> int:
    """Return the object size without downloading its content."""
    return int(_bucket().head_object(key).content_length)


def open_range(key: str, start: int | None = None, end: int | None = None):
    """Open a streaming OSS response, optionally for an inclusive byte range."""
    if start is None:
        return _bucket().get_object(key)
    return _bucket().get_object(key, byte_range=(start, end))


def exists(key: str) -> bool:
    return _bucket().object_exists(key)


def delete(key: str) -> None:
    result = _bucket().delete_object(key)
    if result.status not in (200, 204):
        raise RuntimeError("OSS object deletion failed")
