"""Add nullable deletion metadata; dry-run first, backup before --apply. No OSS access."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import photo_lifecycle


def migrate(path: Path, apply: bool = False) -> dict:
    if not path.exists():
        return {'changed': 0, 'applied': False}
    with photo_lifecycle.locked(path):
        if path.with_suffix('.purge-journal.json').exists():
            raise ValueError('Recover pending photo transaction before migration')
        raw = path.read_bytes()
        data = json.loads(raw)
        changed = 0
        for item in data.get('photos', []):
            if 'deletedAt' not in item:
                item['deletedAt'] = None
                changed += 1
        if 'seedStates' not in data:
            data['seedStates'] = {}
        for index in range(1, 32):
            state = data['seedStates'].setdefault(f'seed-{index:02d}', {})
            if 'deletedAt' not in state:
                state['deletedAt'] = None
                changed += 1
        result = {'changed': changed, 'applied': False, 'uploadedRecords': len(data.get('photos', []))}
        if apply and changed:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = path.with_name(path.name + '.pre-recycle-' + stamp + '.bak')
            with backup.open('xb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            backup.chmod(0o600)
            assert backup.read_bytes() == raw
            photo_lifecycle.atomic_write(path, data)
            result.update(applied=True, backup=str(backup))
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-file', type=Path, default=Path(__file__).resolve().parents[1] / 'data/photos.json')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.data_file.resolve(), args.apply), ensure_ascii=False))
