"""Incremental comment migration. Dry-run by default; --apply backs up first."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import comment_store


def migrate(path: Path, apply: bool = False) -> dict:
    if not path.exists():
        return {"records": 0, "changed": 0, "applied": False, "reason": "No comment file exists"}
    original_file, original_lock = comment_store.DATA_FILE, comment_store.LOCK_FILE
    comment_store.DATA_FILE, comment_store.LOCK_FILE = path, path.with_suffix(".lock")
    try:
        with comment_store._locked():
            raw = path.read_bytes()
            records = json.loads(raw.decode("utf-8"))
            if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                raise ValueError("Invalid comment metadata")
            changed = 0
            for item in records:
                before = dict(item)
                if item.get("status") in (None, "visible"):
                    item["status"] = "approved"
                item.setdefault("visitorTokenHash", None)
                item.setdefault("approvedAt", item.get("createdAt") if item["status"] == "approved" else None)
                item.setdefault("rejectedAt", None)
                changed += item != before
            result = {"records": len(records), "changed": changed, "applied": False}
            if apply and changed:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = path.with_name(path.name + ".pre-moderation-" + stamp + ".bak")
                backup.write_bytes(raw)
                temporary = path.with_name(path.name + ".moderation.tmp")
                try:
                    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                result.update(applied=True, backup=str(backup))
            return result
    finally:
        comment_store.DATA_FILE, comment_store.LOCK_FILE = original_file, original_lock


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-file", type=Path, default=comment_store.DATA_FILE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.data_file.resolve(), args.apply), ensure_ascii=False))
