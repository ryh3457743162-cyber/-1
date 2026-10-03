"""Independent, bounded anonymous statistics. Never reads business JSON or IP addresses."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fixed UTC+08:00 is the current Asia/Shanghai civil timezone, with no DST.
SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
PAGES = {'startup': '启动页', 'atlas': '图集模式', 'flat': '平面模式', 'book': '书本模式'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, pv INTEGER NOT NULL, uv INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS visitors(day TEXT, visitor TEXT, PRIMARY KEY(day,visitor));
CREATE TABLE IF NOT EXISTS dimensions(day TEXT, kind TEXT, label TEXT, count INTEGER NOT NULL,
 PRIMARY KEY(day,kind,label));
CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY, day TEXT NOT NULL, at TEXT NOT NULL,
 page TEXT NOT NULL, visitor TEXT NOT NULL, device TEXT NOT NULL, browser TEXT NOT NULL,
 os TEXT NOT NULL, event TEXT NOT NULL, UNIQUE(day,event));
CREATE INDEX IF NOT EXISTS visits_day_id ON visits(day,id DESC);
CREATE TABLE IF NOT EXISTS limits(bucket TEXT, kind TEXT, key TEXT, count INTEGER NOT NULL,
 PRIMARY KEY(bucket,kind,key));
CREATE TABLE IF NOT EXISTS errors(day TEXT, status INTEGER, count INTEGER NOT NULL,
 PRIMARY KEY(day,status));
CREATE TABLE IF NOT EXISTS maintenance(key TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA user_version=1;
"""


def now_utc():
    return datetime.now(timezone.utc)


def day_of(now):
    return now.astimezone(SHANGHAI).date().isoformat()


def classify(ua):
    value = ua.lower()
    device = ('平板' if 'ipad' in value or ('android' in value and 'mobile' not in value)
              else '手机' if any(x in value for x in ('iphone', 'mobile', 'ipod'))
              else '桌面' if any(x in value for x in ('windows', 'macintosh', 'linux', 'cros')) else '未知')
    browser = next((name for markers, name in [
        (('edg/', 'edga/', 'edgios/'), 'Edge'), (('firefox/', 'fxios/'), 'Firefox'),
        (('chrome/', 'crios/'), 'Chrome'), (('safari/',), 'Safari')]
        if any(x in value for x in markers)), '其他')
    system = ('iOS' if any(x in value for x in ('iphone', 'ipad', 'ipod')) else
              'Android' if 'android' in value else 'Windows' if 'windows' in value else
              'macOS' if 'macintosh' in value else 'ChromeOS' if 'cros' in value else
              'Linux' if 'linux' in value else '未知')
    return device, browser, system


class AnalyticsStore:
    def __init__(self, path, secret, clock=now_utc):
        self.path, self.secret, self.clock = Path(path), secret.encode(), clock
        self._initialized = False
        self._init_lock = threading.Lock()

    @contextmanager
    def connection(self, write=False):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=.15, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA busy_timeout=150')
            con.execute('PRAGMA synchronous=NORMAL')
            if not self._initialized:
                with self._init_lock:
                    if not self._initialized:
                        version = con.execute('PRAGMA user_version').fetchone()[0]
                        if version not in (0, 1):
                            raise sqlite3.DatabaseError('Unsupported analytics schema')
                        con.execute('PRAGMA journal_mode=WAL')
                        con.execute('PRAGMA synchronous=NORMAL')
                        con.executescript(SCHEMA)
                        self.path.chmod(0o600)
                        self._initialized = True
            if write:
                con.execute('BEGIN IMMEDIATE')
            yield con
            if write:
                con.commit()
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def digest(self, purpose, day, value):
        return hmac.new(self.secret, f'{purpose}\0{day}\0{value}'.encode(), hashlib.sha256).hexdigest()

    def _cleanup(self, con, now):
        day = day_of(now)
        previous = con.execute("SELECT value FROM maintenance WHERE key='cleanup'").fetchone()
        if previous and previous[0] == day:
            return
        civil = now.astimezone(SHANGHAI).date()
        raw_cutoff, summary_cutoff = (civil-timedelta(days=29)).isoformat(), (civil-timedelta(days=364)).isoformat()
        con.execute('DELETE FROM visits WHERE day < ?', (raw_cutoff,))
        con.execute('DELETE FROM visitors WHERE day < ?', ((civil-timedelta(days=1)).isoformat(),))
        for table in ('daily', 'dimensions', 'errors'):
            con.execute(f'DELETE FROM {table} WHERE day < ?', (summary_cutoff,))
        con.execute("DELETE FROM limits WHERE (kind!='global' AND bucket < ?) OR (kind='global' AND bucket < ?)",
                    ((now-timedelta(hours=2)).strftime('%Y-%m-%dT%H'),day+'T00'))
        con.execute("INSERT INTO maintenance VALUES('cleanup',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (day,))

    def record(self, page, visitor_id, event_id, categories, peer):
        now = self.clock(); day = day_of(now)
        visitor, event = self.digest('visitor', day, visitor_id), self.digest('event', day, event_id)
        hour = now.strftime('%Y-%m-%dT%H')
        with self.connection(write=True) as con:
            self._cleanup(con, now)
            if con.execute('SELECT 1 FROM visits WHERE day=? AND event=?', (day,event)).fetchone():
                return 'duplicate'
            # One daily global cap bounds storage to <=300,000 raw rows / 30 days.
            buckets = [(hour,'visitor',visitor,120), (hour,'peer',self.digest('peer',day,peer),6000),
                       (day+'T00','global','all',10000)]
            for bucket, kind, key, cap in buckets:
                row = con.execute('SELECT count FROM limits WHERE bucket=? AND kind=? AND key=?', (bucket,kind,key)).fetchone()
                if row and row[0] >= cap:
                    return 'limited'
            for bucket, kind, key, _ in buckets:
                con.execute('INSERT INTO limits VALUES(?,?,?,1) ON CONFLICT(bucket,kind,key) DO UPDATE SET count=count+1', (bucket,kind,key))
            fresh = con.execute('INSERT OR IGNORE INTO visitors VALUES(?,?)', (day,visitor)).rowcount
            con.execute('INSERT INTO daily VALUES(?,1,?) ON CONFLICT(day) DO UPDATE SET pv=pv+1,uv=uv+excluded.uv', (day,fresh))
            for kind,label in zip(('page','device','browser','os'), (page,*categories)):
                con.execute('INSERT INTO dimensions VALUES(?,?,?,1) ON CONFLICT(day,kind,label) DO UPDATE SET count=count+1', (day,kind,label))
            con.execute('INSERT INTO visits(day,at,page,visitor,device,browser,os,event) VALUES(?,?,?,?,?,?,?,?)',
                        (day,now.isoformat(),page,visitor,*categories,event))
        return 'recorded'

    def error(self, status):
        now = self.clock()
        with self.connection(write=True) as con:
            self._cleanup(con, now)
            con.execute('INSERT INTO errors VALUES(?,?,1) ON CONFLICT(day,status) DO UPDATE SET count=count+1', (day_of(now),status))

    def summary(self, days):
        today = self.clock().astimezone(SHANGHAI).date()
        start, seven = (today-timedelta(days=days-1)).isoformat(), (today-timedelta(days=6)).isoformat()
        with self.connection() as con:
            # One consistent WAL snapshot for all metrics in this response.
            con.execute('BEGIN')
            rows = {r['day']:dict(r) for r in con.execute('SELECT * FROM daily WHERE day BETWEEN ? AND ?', (start,today.isoformat()))}
            current = rows.get(today.isoformat(), {'pv':0,'uv':0})
            recent7 = con.execute('SELECT COALESCE(SUM(pv),0) FROM daily WHERE day BETWEEN ? AND ?', (seven,today.isoformat())).fetchone()[0]
            errors_today = con.execute('SELECT COALESCE(SUM(count),0) FROM errors WHERE day=?', (today.isoformat(),)).fetchone()[0]
            dimensions = {}
            for kind in ('page','device','browser','os'):
                dimensions[kind] = [{'label':r['label'],'count':r['count']} for r in con.execute(
                    'SELECT label,SUM(count) AS count FROM dimensions WHERE kind=? AND day BETWEEN ? AND ? GROUP BY label ORDER BY count DESC,label', (kind,start,today.isoformat()))]
            errors = [dict(r) for r in con.execute('SELECT day,status,count FROM errors WHERE day BETWEEN ? AND ? ORDER BY day DESC,status', (start,today.isoformat()))]
            trend = []
            for i in range(days):
                day = (today-timedelta(days=days-1-i)).isoformat()
                trend.append({'day':day,'pv':rows.get(day,{}).get('pv',0),'uv':rows.get(day,{}).get('uv',0)})
            return {'today':today.isoformat(),'timezone':'Asia/Shanghai','days':days,
                    'cards':{'todayPv':current['pv'],'todayUv':current['uv'],'last7Pv':recent7,'todayErrors':errors_today},
                    'trend':trend,'dimensions':dimensions,'errors':errors,'pages':PAGES}

    def recent(self, page):
        cutoff = (self.clock().astimezone(SHANGHAI).date()-timedelta(days=29)).isoformat()
        with self.connection() as con:
            con.execute('BEGIN')
            total = con.execute('SELECT count(*) FROM visits WHERE day>=?',(cutoff,)).fetchone()[0]
            rows = con.execute('SELECT day,at,page,visitor,device,browser,os FROM visits WHERE day>=? ORDER BY id DESC LIMIT 20 OFFSET ?', (cutoff,(page-1)*20)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value['visitor'] = value['visitor'][:12]
                result.append(value)
            return {'items':result,'total':total,'page':page,'pageSize':20}

    def cleanup(self):
        with self.connection(write=True) as con:
            con.execute("DELETE FROM maintenance WHERE key='cleanup'")
            self._cleanup(con,self.clock())
