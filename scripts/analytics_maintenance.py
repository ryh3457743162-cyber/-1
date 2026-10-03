"""Local/future deployment tool: idempotent init, retention cleanup and WAL-safe backup."""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analytics_store import AnalyticsStore


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('统计数据库不存在')
    if any(part.lower()=='public' for part in destination.parts) or source == destination:
        raise ValueError('备份必须位于非公开目录，不能覆盖源数据库')
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Exclusive creation avoids overwriting another backup, even during concurrent runs.
    descriptor=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(descriptor)
    deadline=time.monotonic()+30
    def progress(_status,_remaining,_total):
        if time.monotonic()>deadline:
            raise TimeoutError('统计备份超时，请检查数据库负载')
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=.15)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst,pages=64,progress=progress,sleep=.05)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('统计备份完整性验证失败')
            count=dst.execute('SELECT count(*) FROM visits').fetchone()[0]
        result={'path':str(destination),'bytes':destination.stat().st_size,
                'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'integrity':'ok','visits':count}
        Path(str(destination)+'.checksums.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        Path(str(destination)+'.checksums.json').chmod(0o600)
        return result
    except BaseException:
        # Do not present an incomplete backup as usable; retain it for diagnosis.
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['init','cleanup','backup'])
    parser.add_argument('--database',required=True,type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if any(part.lower()=='public' for part in args.database.resolve().parts):
        parser.error('统计数据库必须位于非公开持久化目录')
    if args.action=='backup':
        if not args.output:parser.error('备份需要 --output')
        print(json.dumps(backup(args.database,args.output),ensure_ascii=False))
    else:
        if args.action=='cleanup' and not args.database.is_file():parser.error('统计数据库不存在')
        store=AnalyticsStore(args.database,'maintenance-only-no-identities')
        if args.action=='init':
            with store.connection() as connection:
                assert connection.execute('PRAGMA user_version').fetchone()[0]==1
        else:store.cleanup()
        print(args.action+' completed')


if __name__=='__main__':
    main()
