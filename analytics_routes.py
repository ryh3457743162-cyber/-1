"""Privacy-preserving analytics routes, kept separate from the photo lifecycle lock."""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from flask import jsonify, request, send_from_directory
from analytics_store import AnalyticsStore, PAGES, classify, day_of


def register_analytics(app, base_dir, public_dir, admin_required, is_admin):
    app.config.setdefault('ANALYTICS_ENABLED', os.getenv('ANALYTICS_ENABLED','0') == '1')
    app.config.setdefault('ANALYTICS_HMAC_KEY', os.getenv('ANALYTICS_HMAC_KEY',''))
    app.config.setdefault('ANALYTICS_DATABASE', os.getenv('ANALYTICS_DATABASE',str(Path(base_dir)/'data'/'analytics.sqlite3')))
    stores = {}; store_lock = threading.Lock(); last_warning = [0.0]

    def store():
        key = app.config['ANALYTICS_HMAC_KEY']
        if not app.config['ANALYTICS_ENABLED'] or not isinstance(key,str) or len(key) < 32:
            return None
        path = str(app.config['ANALYTICS_DATABASE'])
        if Path(path).resolve().is_relative_to(Path(public_dir()).resolve()):
            return None
        with store_lock:
            if (path,key) not in stores:
                stores[(path,key)] = AnalyticsStore(path,key)
            return stores[(path,key)]

    app.extensions['analytics_store'] = store

    def warn():
        # No UA, IP, URL, identifier, key or DB contents in logs.
        with store_lock:
            if time.monotonic()-last_warning[0] > 60:
                last_warning[0] = time.monotonic()
                app.logger.warning('Analytics temporarily unavailable; public features continue normally')

    @app.get('/admin/analytics')
    def analytics_page():
        return send_from_directory(public_dir(), 'analytics-admin.html')

    @app.get('/analytics.js')
    def analytics_script():
        return send_from_directory(public_dir(), 'analytics.js')

    @app.get('/analytics-admin.js')
    def analytics_admin_script():
        return send_from_directory(public_dir(), 'analytics-admin.js')

    @app.post('/api/analytics/pageview')
    def pageview():
        if request.content_length is not None and request.content_length > 1024:
            return jsonify(error='请求过大'),413
        if request.mimetype != 'application/json':
            return jsonify(error='仅支持 JSON'),415
        origin = request.headers.get('Origin')
        # nginx terminates TLS; do not assume Flask's internal HTTP scheme is public HTTPS.
        # Match authority without trusting caller-supplied X-Forwarded-Proto or XFF.
        try:
            parsed = urlsplit(origin) if origin else None
            wrong_origin = bool(parsed and (parsed.scheme not in ('http','https') or
                parsed.netloc.lower()!=request.host.lower() or parsed.path or parsed.query or parsed.fragment))
        except ValueError:
            wrong_origin = True
        if wrong_origin or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify(error='来源不允许'),403
        body = request.stream.read(1025)
        if len(body)>1024:
            return jsonify(error='请求过大'),413
        try:
            value = json.loads(body)
            if not isinstance(value,dict) or set(value) != {'page','visitorId','eventId','date'}:
                raise ValueError()
            if not isinstance(value['page'],str) or value['page'] not in PAGES:
                raise ValueError()
            if not isinstance(value['date'],str) or date.fromisoformat(value['date']).isoformat()!=value['date']:
                raise ValueError()
            for field in ('visitorId','eventId'):
                token = value[field]
                if not isinstance(token,str) or len(token)!=36 or str(uuid.UUID(token))!=token or uuid.UUID(token).version!=4:
                    raise ValueError()
        except (ValueError,TypeError,UnicodeDecodeError):
            return jsonify(error='统计参数不正确'),400
        database = store()
        if not database:
            return jsonify(accepted=False),202
        if value['date'] != day_of(database.clock()):
            return jsonify(error='统计日期已变化',code='date_changed'),400
        ua = request.headers.get('User-Agent','')[:2048]
        if is_admin() or re.search(r'bot\b|spider|crawler|slurp|bingpreview|headlesschrome',ua,re.I):
            return jsonify(accepted=False),202
        try:
            result = database.record(value['page'],value['visitorId'],value['eventId'],classify(ua),request.remote_addr or 'unknown')
            if result=='limited':
                return jsonify(accepted=False),429,{'Retry-After':'3600'}
            return jsonify(accepted=result=='recorded'),202
        except (sqlite3.Error,OSError):
            warn()
            return jsonify(accepted=False),202

    def unavailable():
        return jsonify(error='访问统计暂不可用，请检查统计开关、密钥与数据目录权限。'),503

    @app.get('/api/manage/analytics/summary')
    @admin_required
    def summary():
        if set(request.args)-{'days'} or request.args.get('days','7') not in ('7','30'):
            return jsonify(error='请选择 7 天或 30 天'),400
        database = store()
        if not database:
            return unavailable()
        try:
            return jsonify(database.summary(int(request.args.get('days','7'))))
        except (sqlite3.Error,OSError):
            warn(); return unavailable()

    @app.get('/api/manage/analytics/visits')
    @admin_required
    def visits():
        page = request.args.get('page','1')
        if set(request.args)-{'page'} or not re.fullmatch(r'[1-9][0-9]{0,5}',page):
            return jsonify(error='页码不正确'),400
        database = store()
        if not database:
            return unavailable()
        try:
            return jsonify(database.recent(int(page)))
        except (sqlite3.Error,OSError):
            warn(); return unavailable()

    @app.after_request
    def application_errors(response):
        if (response.status_code==404 or response.status_code>=500) and not request.path.startswith(('/api/analytics/','/api/manage/analytics/')):
            database = store()
            if database:
                try:
                    database.error(response.status_code)
                except (sqlite3.Error,OSError):
                    warn()
        return response
