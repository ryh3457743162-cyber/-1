"""Copy selected legacy photos to OSS, retaining local files and old URLs.

Dry-run is the default. Apply requires explicit photo IDs and writes a report.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageOps


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
SEED_COUNT = 31


def inspect_file(path: Path) -> dict:
    if not path.is_file():
        return {"status": "missing", "error": "local file not found"}
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
            orientation = image.getexif().get(274, 1)
    except (OSError, ValueError, SyntaxError) as error:
        return {"status": "unreadable", "error": type(error).__name__}
    return {
        "status": "ready",
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "format": image_format,
        "width": width,
        "height": height,
        "exifOrientation": orientation,
    }


def object_keys(photo_id: str, year: str, digest: str) -> tuple[str, str]:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(photo_id).stem).strip("-")
    token = f"{safe_id}-{digest[:32]}"
    return (f"photos/originals/{year}/{token}.webp",
            f"photos/thumbnails/{year}/{token}.webp")


def inventory(root: Path) -> dict:
    metadata_file = root / "data" / "photos.json"
    data = json.loads(metadata_file.read_text(encoding="utf-8"))
    hidden = set(data.get("hiddenSeedIds", []))
    seed_oss = data.get("seedOss", {})
    if not isinstance(seed_oss, dict):
        raise ValueError("seedOss must be an object")
    items = []

    def add(photo_id: str, year: str, title: str, old_url: str,
            path: Path, source: str, sort_order: int | None,
            existing: dict) -> None:
        details = inspect_file(path)
        item = {
            "photoId": photo_id,
            "source": source,
            "year": year,
            "title": title,
            "oldUrl": old_url,
            "localPath": str(path),
            "sortOrder": sort_order,
            **details,
        }
        if existing.get("objectKey"):
            item["status"] = "already-migrated"
            item["objectKey"] = existing["objectKey"]
            item["thumbnailKey"] = existing.get("thumbnailKey")
        elif item["status"] == "ready":
            item["objectKey"], item["thumbnailKey"] = object_keys(
                photo_id, year, details["sha256"]
            )
        items.append(item)

    for index in range(1, SEED_COUNT + 1):
        photo_id = f"seed-{index:02d}"
        if photo_id in hidden:
            continue
        name = f"photo-{index:02d}.jpg"
        add(photo_id, "2026", f"和宝宝的记忆 · {index:02d}",
            f"/photos/{name}", root / "public" / "photos" / name,
            "static-seed", index, seed_oss.get(photo_id) or {})

    for index, record in enumerate(data.get("photos", []), 1):
        photo_id = record["id"]
        if Path(photo_id).name != photo_id:
            raise ValueError(f"unsafe photo ID: {photo_id}")
        add(photo_id, str(record.get("year", "")),
            str(record.get("title", "")), str(record.get("src", "")),
            root / "uploads" / photo_id, "uploaded-record",
            record.get("sortOrder"), record)

    statuses = {name: sum(item["status"] == name for item in items)
                for name in ("ready", "already-migrated", "missing", "unreadable")}
    return {
        "mode": "dry-run",
        "metadataFile": str(metadata_file),
        "visibleCount": len(items),
        "staticSeedCount": sum(item["source"] == "static-seed" for item in items),
        "uploadedRecordCount": sum(item["source"] == "uploaded-record" for item in items),
        "hiddenSeedIds": sorted(hidden),
        "statuses": statuses,
        "items": items,
    }


def load_oss_environment(path: Path) -> None:
    # The root-only systemd env file never appears in reports or output.
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("OSS_") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        os.environ[key] = value


def web_versions(path: Path) -> tuple[bytes, bytes]:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    image.thumbnail((3200, 3200), Image.Resampling.LANCZOS)
    original = io.BytesIO()
    image.save(original, "WEBP", quality=90, method=4)
    thumbnail = image.copy()
    thumbnail.thumbnail((600, 600), Image.Resampling.LANCZOS)
    preview = io.BytesIO()
    thumbnail.save(preview, "WEBP", quality=82, method=4)
    return original.getvalue(), preview.getvalue()


def copy_and_verify(storage, key: str, content: bytes) -> None:
    if not storage.exists(key):
        storage.put(key, content, "image/webp")
    stored = storage.get(key)
    if hashlib.sha256(stored).digest() != hashlib.sha256(content).digest():
        raise ValueError("OSS checksum mismatch")
    with Image.open(io.BytesIO(stored)) as image:
        image.verify()


def save_metadata(root: Path, item: dict) -> None:
    metadata = root / "data" / "photos.json"
    current_bytes = metadata.read_bytes()
    data = json.loads(current_bytes)
    photo_id = item["photoId"]
    if item["source"] == "static-seed":
        if photo_id in data.get("hiddenSeedIds", []):
            raise ValueError("photo is now hidden")
        mapping = data.setdefault("seedOss", {})
        if mapping.get(photo_id, {}).get("objectKey"):
            raise ValueError("photo migrated concurrently")
        mapping[photo_id] = {
            "objectKey": item["objectKey"],
            "thumbnailKey": item["thumbnailKey"],
        }
    else:
        match = [p for p in data.get("photos", []) if p.get("id") == photo_id]
        if len(match) != 1 or match[0].get("src") != item["oldUrl"]:
            raise ValueError("photo metadata changed during migration")
        if match[0].get("objectKey"):
            raise ValueError("photo migrated concurrently")
        match[0]["objectKey"] = item["objectKey"]
        match[0]["thumbnailKey"] = item["thumbnailKey"]
        match[0]["thumbnailSrc"] = f"/api/thumbnails/{photo_id}"
        match[0]["mimeType"] = "image/webp"
    if metadata.read_bytes() != current_bytes:
        raise ValueError("metadata changed during migration")
    temporary = metadata.with_suffix(".migration-tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, metadata.stat().st_mode & 0o777)
        os.chown(temporary, metadata.stat().st_uid, metadata.stat().st_gid)
        temporary.replace(metadata)
    finally:
        temporary.unlink(missing_ok=True)


def write_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def migrate(root: Path, selected_ids: list[str], env_file: Path, report_path: Path) -> dict:
    load_oss_environment(env_file)
    sys.path.insert(0, str(root))
    import oss_storage
    if not oss_storage.configured():
        raise ValueError("OSS is not configured")
    current = inventory(root)
    by_id = {item["photoId"]: item for item in current["items"]}
    if len(selected_ids) != len(set(selected_ids)) or any(photo_id not in by_id for photo_id in selected_ids):
        raise ValueError("unknown or repeated photo ID")
    report = {"mode": "apply", "selectedCount": len(selected_ids), "results": []}
    for photo_id in selected_ids:
        item = by_id[photo_id]
        result = {key: item.get(key) for key in (
            "photoId", "oldUrl", "localPath", "objectKey", "thumbnailKey")}
        if item["status"] == "already-migrated":
            result["status"] = "skipped-already-migrated"
        elif item["status"] != "ready":
            result["status"] = "failed"
            result["error"] = item["status"]
        else:
            try:
                source = Path(item["localPath"])
                if inspect_file(source).get("sha256") != item["sha256"]:
                    raise ValueError("source file changed since inventory")
                original, thumbnail = web_versions(source)
                copy_and_verify(oss_storage, item["objectKey"], original)
                copy_and_verify(oss_storage, item["thumbnailKey"], thumbnail)
                save_metadata(root, item)
                result["status"] = "success"
            except Exception as error:
                # Never print remote SDK errors: they can contain request details.
                result["status"] = "failed"
                result["error"] = type(error).__name__
        report["results"].append(result)
        write_report(report_path, report)
        print(f'{photo_id}: {result["status"]}')
    report["successCount"] = sum(x["status"] == "success" for x in report["results"])
    report["failedCount"] = sum(x["status"] == "failed" for x in report["results"])
    write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--env-file", type=Path, default=Path("/etc/photo-ring.env"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.dry_run:
        if args.ids:
            parser.error("--ids is only valid with --apply")
        print(json.dumps(inventory(root), ensure_ascii=False, indent=2))
        return 0
    if not args.ids or not args.report:
        parser.error("--apply requires --ids and --report")
    result = migrate(root, args.ids, args.env_file, args.report)
    print(f'{result["successCount"]} succeeded, {result["failedCount"]} failed')
    if result["failedCount"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
