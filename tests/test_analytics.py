"""Statistics fixtures are synthetic, isolated SQLite files; no production writes."""
import importlib
import sqlite3
import tempfile
import unittest
import uuid
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from analytics_store import AnalyticsStore, classify, day_of
from scripts.analytics_maintenance import backup


def worker_writes(args):
    path,visitor=args
    store=AnalyticsStore(path,'synthetic-key-'+('x'*32),lambda:datetime(2026,10,3,8,tzinfo=timezone.utc))
    return [store.record('book',visitor,str(uuid.uuid4()),('桌面','Chrome','Windows'),'peer') for _ in range(15)]


class AnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.now=datetime(2026,10,3,8,tzinfo=timezone.utc)
        self.store=AnalyticsStore(self.root/'analytics.sqlite3','synthetic-key-'+('x'*32),lambda:self.now)
        self.visitor=str(uuid.uuid4())

    def tearDown(self):self.temp.cleanup()

    def record(self,page='startup',visitor=None,event=None):
        return self.store.record(page,visitor or self.visitor,event or str(uuid.uuid4()),('手机','Safari','iOS'),'local-peer')

    def test_first_view_pv_uv_and_hashed_only(self):
        self.assertEqual(self.record(),'recorded')
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],1)
        self.assertEqual(self.store.summary(7)['cards']['todayUv'],1)
        with self.store.connection() as con:
            row=dict(con.execute('SELECT * FROM visits').fetchone())
            self.assertNotIn(self.visitor,str(row));self.assertEqual(len(row['visitor']),64)
            self.assertNotIn('User-Agent',str(row));self.assertNotIn('local-peer',str(row))

    def test_same_day_uv_and_spa_pages(self):
        for page in ('startup','atlas','flat','book','atlas'):self.record(page)
        result=self.store.summary(7)
        self.assertEqual((result['cards']['todayPv'],result['cards']['todayUv']),(5,1))
        self.assertEqual({r['label']:r['count'] for r in result['dimensions']['page']}, {'startup':1,'atlas':2,'flat':1,'book':1})

    def test_duplicate_event_does_not_increase_pv(self):
        event=str(uuid.uuid4());self.record(event=event)
        self.assertEqual(self.record(event=event),'duplicate')
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],1)

    def test_shanghai_midnight_and_identity_rotation(self):
        self.now=datetime(2026,10,3,15,59,59,tzinfo=timezone.utc);self.record()
        old=self.store.recent(1)['items'][0]['visitor']
        self.now+=timedelta(seconds=1);self.record()
        self.assertEqual(day_of(self.now),'2026-10-04')
        self.assertEqual(self.store.summary(7)['trend'][-2:],[{'day':'2026-10-03','pv':1,'uv':1},{'day':'2026-10-04','pv':1,'uv':1}])
        self.assertNotEqual(old,self.store.recent(1)['items'][0]['visitor'])

    def test_concurrent_writes_independent_connections(self):
        with self.store.connection():pass
        stores=[AnalyticsStore(self.store.path,'synthetic-key-'+('x'*32),lambda:self.now) for _ in range(4)]
        for store in stores:
            with store.connection():pass
        def work(i):return stores[i%4].record('book',self.visitor,str(uuid.uuid4()),('桌面','Chrome','Windows'),'peer')
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(list(pool.map(work,range(80))),['recorded']*80)
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],80)
        self.assertEqual(self.store.summary(7)['cards']['todayUv'],1)
        with self.store.connection() as con:self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_retention_raw30_summary365(self):
        today=self.now
        for offset in (365,364,30,29,0):
            self.now=today-timedelta(days=offset);self.record()
        self.now=today;self.store.cleanup()
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM visits').fetchone()[0],2)
            self.assertEqual(con.execute('SELECT count(*) FROM daily').fetchone()[0],4)
            self.assertEqual(con.execute('SELECT count(*) FROM visitors').fetchone()[0],1)
        self.assertEqual(self.store.summary(30)['cards']['last7Pv'],1)

    def test_separate_processes_like_gunicorn_workers(self):
        with self.store.connection():pass
        with multiprocessing.get_context('spawn').Pool(3) as pool:
            results=pool.map(worker_writes,[(str(self.store.path),self.visitor)]*3)
        self.assertTrue(all(item=='recorded' for batch in results for item in batch))
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],45)
        self.assertEqual(self.store.summary(7)['cards']['todayUv'],1)

    def test_rate_cap_and_daily_global_cap_survives_midday_cleanup(self):
        for _ in range(120):self.record()
        self.assertEqual(self.record(),'limited')
        with self.store.connection(write=True) as con:
            con.execute("UPDATE limits SET count=10000 WHERE kind='global'")
        self.now+=timedelta(hours=3);self.store.cleanup()
        self.assertEqual(self.record(visitor=str(uuid.uuid4())),'limited')

    def test_empty_and_paginated_recent(self):
        self.assertEqual(self.store.summary(30)['cards']['todayPv'],0)
        self.assertEqual(len(self.store.summary(30)['trend']),30)
        for _ in range(23):self.record()
        self.assertEqual(len(self.store.recent(1)['items']),20)
        self.assertEqual(len(self.store.recent(2)['items']),3)
        self.assertEqual(self.store.recent(3)['items'],[])

    def test_shared_proxy_peer_does_not_stop_at_6000(self):
        self.record()
        with self.store.connection(write=True) as con:
            con.execute("UPDATE limits SET count=6000 WHERE kind='peer'")
        self.assertEqual(self.record(visitor=str(uuid.uuid4())), 'recorded')
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],2)

    def test_dashboard_collection_cap_and_denied_counts(self):
        self.record()
        with self.store.connection(write=True) as con:
            con.execute("UPDATE limits SET count=120 WHERE kind='visitor'")
        self.assertEqual(self.record(),'limited')
        result=self.store.summary(7)['collection']
        self.assertFalse(result['reached']);self.assertEqual(result['blocked'],{'visitor':1})
        with self.store.connection(write=True) as con:
            con.execute("UPDATE limits SET count=10000 WHERE kind='global'")
        self.assertEqual(self.record(visitor=str(uuid.uuid4())),'limited')
        self.store.cleanup()
        result=self.store.summary(7)['collection']
        self.assertTrue(result['reached']);self.assertEqual(result['accepted'],10000)
        self.assertEqual(result['blockedTotal'],2)
        self.assertEqual(result['blocked'],{'visitor':1,'global':1})

    def test_backup_wal_snapshot_and_exclusive_target(self):
        self.record();target=self.root/'backups'/'snapshot.sqlite3'
        # Keep a reader open, so WAL state remains relevant during the backup.
        with self.store.connection() as con:
            self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            result=backup(self.store.path,target)
        self.assertEqual(result['integrity'],'ok');self.assertEqual(result['visits'],1)
        with self.assertRaises(FileExistsError):backup(self.store.path,target)

    def test_device_classification(self):
        self.assertEqual(classify('Mozilla iPad Safari/17'),('平板','Safari','iOS'))
        self.assertEqual(classify('Mozilla Android Chrome/120'),('平板','Chrome','Android'))
        self.assertEqual(classify('Windows Chrome/120 Edg/120'),('桌面','Edge','Windows'))
        self.assertEqual(classify(''),('未知','其他','未知'))

    def test_repeatable_init_and_unknown_schema_not_reset(self):
        self.record();second=AnalyticsStore(self.store.path,'x'*32)
        with second.connection() as con:
            self.assertEqual(con.execute('SELECT pv FROM daily').fetchone()[0],1)
            con.execute('PRAGMA user_version=99')
        with self.assertRaises(sqlite3.DatabaseError):
            with AnalyticsStore(self.store.path,'x'*32).connection():pass


class AnalyticsApiTest(unittest.TestCase):
    def setUp(self):
        self.site=importlib.import_module('app');self.app=self.site.app
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config={k:self.app.config[k] for k in ('ANALYTICS_ENABLED','ANALYTICS_HMAC_KEY','ANALYTICS_DATABASE')}
        self.app.config.update(ANALYTICS_ENABLED=True,ANALYTICS_HMAC_KEY='test-only-'+('x'*32),ANALYTICS_DATABASE=str(self.root/'stats.sqlite3'))
        self.store=self.app.extensions['analytics_store']();self.now=datetime(2026,10,3,8,tzinfo=timezone.utc);self.store.clock=lambda:self.now
        self.client=self.app.test_client();self.admin=self.app.test_client()
        with self.admin.session_transaction() as session:session['admin']=True
        self.body={'page':'startup','visitorId':str(uuid.uuid4()),'eventId':str(uuid.uuid4()),'date':'2026-10-03'}

    def tearDown(self):self.app.config.update(self.config);self.temp.cleanup()

    def post(self,client=None,**changes):return (client or self.client).post('/api/analytics/pageview',json={**self.body,**changes},headers={'User-Agent':'Mozilla Windows Chrome/120'})

    def test_auth_and_admin_visits_excluded(self):
        for path in ('summary','visits'):self.assertEqual(self.client.get('/api/manage/analytics/'+path).status_code,401)
        self.assertFalse(self.post(self.admin).json['accepted'])
        self.assertEqual(self.admin.get('/api/manage/analytics/summary').json['cards']['todayPv'],0)

    def test_invalid_page_date_fields_and_body_size(self):
        for page in ('admin','/','https://example.com','api','static',None,[]):self.assertEqual(self.post(page=page).status_code,400)
        for date in ('2026-02-30','2026-10-02','2026-10-04',None):self.assertEqual(self.post(date=date).status_code,400)
        self.assertEqual(self.post(url='secret-query').status_code,400)
        self.assertEqual(self.post(visitorId='abc').status_code,400)
        self.assertEqual(self.client.post('/api/analytics/pageview',data='x'*1025,content_type='application/json').status_code,413)
        self.assertEqual(self.client.post('/api/analytics/pageview',data='{}',content_type='text/plain').status_code,415)

    def test_bot_and_cross_site_rejected(self):
        response=self.client.post('/api/analytics/pageview',json=self.body,headers={'User-Agent':'Googlebot/2.1'})
        self.assertFalse(response.json['accepted'])
        response=self.client.post('/api/analytics/pageview',json=self.body,headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],0)

    def test_https_origin_behind_nginx_internal_http(self):
        response=self.client.post('/api/analytics/pageview',json=self.body,
            base_url='http://zhoumengfan.vip',headers={'Origin':'https://zhoumengfan.vip','X-Forwarded-For':'untrusted','X-Forwarded-Proto':'https'})
        self.assertEqual(response.status_code,202);self.assertTrue(response.json['accepted'])
        response=self.client.post('/api/analytics/pageview',json=self.body,
            base_url='http://zhoumengfan.vip',headers={'Origin':'https://www.zhoumengfan.vip'})
        self.assertEqual(response.status_code,403)

    def test_event_idempotency_and_no_xff_trust(self):
        self.assertTrue(self.post().json['accepted']);self.assertFalse(self.post().json['accepted'])
        self.client.post('/api/analytics/pageview',json={**self.body,'eventId':str(uuid.uuid4())},headers={'X-Forwarded-For':'198.51.100.7'})
        with self.store.connection() as con:
            keys=[r[0] for r in con.execute("SELECT key FROM limits WHERE kind='peer'")]
        self.assertEqual(keys,[self.store.digest('peer','2026-10-03','127.0.0.1')])

    def test_application_errors_scope_not_pv(self):
        self.client.get('/nonexistent-test');self.client.get('/another-missing')
        self.assertEqual(self.store.summary(7)['cards']['todayErrors'],2)
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],0)
        # after_request handles 500 responses without depending on nginx or exception text.
        with self.app.test_request_context('/api/example'):
            response=self.app.process_response(self.app.response_class(status=500))
            self.assertEqual(response.status_code,500)
        self.assertEqual(self.store.summary(7)['cards']['todayErrors'],3)
        self.admin.get('/api/manage/analytics/summary?days=9')
        self.assertEqual(self.store.summary(7)['cards']['todayErrors'],3)

    def test_static_admin_api_gets_not_pv(self):
        for path in ('/','/analytics.js','/analytics-admin.js','/manage','/admin/book','/admin/analytics','/api/manage/analytics/summary'):
            self.admin.get(path)
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],0)

    def test_database_unavailable_never_breaks_public_page(self):
        with patch.object(self.store,'record',side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(self.post().status_code,202)
        with patch.object(self.store,'error',side_effect=OSError('offline')):
            self.assertEqual(self.client.get('/').status_code,200)
            self.assertEqual(self.client.get('/nonexistent').status_code,404)
        with patch.object(self.store,'summary',side_effect=sqlite3.DatabaseError('offline')):
            self.assertEqual(self.admin.get('/api/manage/analytics/summary').status_code,503)

    def test_real_sqlite_writer_lock_degrades_without_partial_count(self):
        self.store.summary(7)
        con=sqlite3.connect(self.store.path,isolation_level=None)
        try:
            con.execute('BEGIN IMMEDIATE')
            started=time.monotonic();response=self.post()
            self.assertEqual(response.status_code,202);self.assertFalse(response.json['accepted'])
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual(self.client.get('/').status_code,200)
        finally:
            con.rollback();con.close()
        self.assertEqual(self.store.summary(7)['cards']['todayPv'],0)

    def test_disabled_missing_secret_no_creation(self):
        self.app.config['ANALYTICS_HMAC_KEY']=''
        self.assertEqual(self.post().status_code,202)
        self.assertEqual(self.admin.get('/api/manage/analytics/summary').status_code,503)
        self.assertFalse((self.root/'stats.sqlite3').exists())

    def test_database_in_public_directory_rejected(self):
        path=self.site.PUBLIC_DIR/'analytics.sqlite3'
        self.app.config['ANALYTICS_DATABASE']=str(path)
        self.assertEqual(self.post().status_code,202)
        self.assertEqual(self.admin.get('/api/manage/analytics/summary').status_code,503)
        self.assertFalse(path.exists())

    def test_visitor_rate_limit_api(self):
        for _ in range(120):self.post(eventId=str(uuid.uuid4()))
        self.assertEqual(self.post(eventId=str(uuid.uuid4())).status_code,429)

    def test_admin_strict_pagination(self):
        for page in ('0','-1','abc','1.5','1000000'):
            self.assertEqual(self.admin.get('/api/manage/analytics/visits?page='+page).status_code,400)
        self.assertEqual(self.admin.get('/api/manage/analytics/summary?days=30').status_code,200)
