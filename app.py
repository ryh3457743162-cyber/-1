from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, session, stream_with_context
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
import oss_storage
import music_store
import comment_store
import photo_lifecycle


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
UPLOAD_DIR = BASE_DIR / "uploads"
DATA_FILE = BASE_DIR / "data" / "photos.json"
BOOK_LAYOUT_FILE = BASE_DIR / "data" / "book-layout.json"
ALLOWED_YEARS = {"2026", "2027"}
BOOK_LAYOUT_SLOTS = {
    "single": 1,
    "two-horizontal": 2,
    "two-vertical": 2,
    "two-feature": 2,
    "three-hero-left": 3,
    "three-hero-top": 3,
    "four-grid": 4,
    "note-left-photo": 1,
    "photo-left-note": 1,
    "note-left-two": 2,
    "photo-bottom-note": 1,
    "note-only": 0,
}
MAX_UPLOAD_BYTES = 35 * 1024 * 1024
MAX_REQUEST_BYTES = 40 * 1024 * 1024
MAX_MUSIC_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000
ALLOWED_IMAGE_TYPES = {
    ".jpg": {"JPEG"}, ".jpeg": {"JPEG"}, ".png": {"PNG"},
    ".webp": {"WEBP"}, ".heic": {"HEIF"}, ".heif": {"HEIF"},
}
ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
    "image/heic-sequence", "image/heif-sequence", "image/x-heic",
    "application/octet-stream", "",
}
SEED_IDS = {f"seed-{index:02d}" for index in range(1, 32)}

register_heif_opener()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.environ.get("PHOTO_RING_SECRET", secrets.token_hex(32)),
    MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)

data_lock = threading.RLock()


def default_data() -> dict:
    return {"photos": [], "hiddenSeedIds": ["seed-14", "seed-16", "seed-23"], "pendingOssDeletes": [], "seedOss": {}}


def read_data() -> dict:
    with data_lock:
        if not DATA_FILE.exists():
            write_data(default_data())
        try:
            value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            app.logger.exception("Photo metadata cannot be read; refusing to overwrite it")
            raise
        value.setdefault("photos", [])
        value.setdefault("hiddenSeedIds", [])
        value.setdefault("pendingOssDeletes", [])
        value.setdefault("seedOss", {})
        value.setdefault("seedStates", {})
        return value


def write_data(value: dict) -> None:
    with data_lock:
        photo_lifecycle.atomic_write(DATA_FILE, value)


def read_book_layout() -> dict:
    with data_lock:
        if not BOOK_LAYOUT_FILE.exists():
            return {"configured": False, "years": {year: [] for year in sorted(ALLOWED_YEARS)}}
        try:
            value = json.loads(BOOK_LAYOUT_FILE.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("years"), dict):
                raise ValueError("Invalid book layout")
            return {"configured": True, "years": value["years"]}
        except (OSError, json.JSONDecodeError, ValueError):
            return {"configured": False, "years": {year: [] for year in sorted(ALLOWED_YEARS)}}


def validate_book_layout(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("years"), dict):
        raise ValueError("书本排版格式不正确")
    years = value["years"]
    if set(years) != ALLOWED_YEARS:
        raise ValueError("书本排版必须包含 2026 和 2027 年")
    clean = {}
    seen_ids = set()
    for year in sorted(ALLOWED_YEARS):
        pages = years[year]
        if not isinstance(pages, list) or len(pages) > 200:
            raise ValueError("每个年份最多保存 200 页")
        clean[year] = []
        for order, page in enumerate(pages, 1):
            if not isinstance(page, dict):
                raise ValueError("书页格式不正确")
            page_id, layout = page.get("id"), page.get("layout")
            if not isinstance(page_id, str) or not (1 <= len(page_id) <= 80) or page_id in seen_ids:
                raise ValueError("书页 ID 缺失或重复")
            if not isinstance(layout, str) or layout not in BOOK_LAYOUT_SLOTS:
                raise ValueError("未知的书页布局")
            seen_ids.add(page_id)
            photos = page.get("photos", [])
            if not isinstance(photos, list) or len(photos) > BOOK_LAYOUT_SLOTS[layout]:
                raise ValueError("书页照片数量超过布局槽位")
            slots = set()
            references = []
            for entry in photos:
                if not isinstance(entry, dict):
                    raise ValueError("照片引用格式不正确")
                photo_id, slot = entry.get("photoId"), entry.get("slot")
                if not isinstance(photo_id, str) or not (1 <= len(photo_id) <= 160):
                    raise ValueError("照片 ID 不正确")
                if type(slot) is not int or not (1 <= slot <= BOOK_LAYOUT_SLOTS[layout]) or slot in slots:
                    raise ValueError("照片槽位重复或超出范围")
                slots.add(slot)
                references.append({"photoId": photo_id, "slot": slot})
            note = page.get("note")
            clean_note = None
            if note is not None:
                if not isinstance(note, dict):
                    raise ValueError("留言格式不正确")
                if note.get("type") == "comment":
                    comment_id = note.get("commentId")
                    if not isinstance(comment_id, str) or not (1 <= len(comment_id) <= 80):
                        raise ValueError("精选评论 ID 不正确")
                    clean_note = {"type": "comment", "commentId": comment_id}
                elif note.get("type") == "manual":
                    note_text = note.get("text")
                    author = note.get("author", "")
                    if not isinstance(note_text, str) or not (1 <= len(note_text.strip()) <= 500):
                        raise ValueError("手写留言须为 1 至 500 字")
                    if not isinstance(author, str) or len(author.strip()) > 30:
                        raise ValueError("留言署名最多 30 字")
                    clean_note = {"type": "manual", "text": note_text.strip(), "author": author.strip()}
                else:
                    raise ValueError("未知的留言来源")
            clean_page = {"id": page_id, "order": order, "layout": layout,
                          "photos": sorted(references, key=lambda item: item["slot"])}
            if clean_note:
                clean_page["note"] = clean_note
            clean[year].append(clean_page)
    return {"years": clean}


def is_admin() -> bool:
    return bool(session.get("admin"))


def admin_required(function):
    def wrapped(*args, **kwargs):
        if not is_admin():
            return jsonify({"error": "请先登录"}), 401
        return function(*args, **kwargs)

    wrapped.__name__ = function.__name__
    return wrapped


@app.before_request
def lock_photo_lifecycle():
    if request.path.startswith(("/api/photos", "/api/images/", "/api/thumbnails/", "/photos/",
                                "/api/book-layout", "/api/manage/photos", "/api/manage/trash",
                                "/api/manage/upload", "/api/manage/book-layout", "/api/manage/comments",
                                "/api/manage/oss/retry-deletes")):
        guard = photo_lifecycle.locked(DATA_FILE)
        try:
            guard.__enter__()
            g.photo_guard = guard
            photo_lifecycle.recover(DATA_FILE, BOOK_LAYOUT_FILE, comment_store.DATA_FILE)
        except Exception:
            app.logger.error("Photo metadata temporarily unavailable")
            return jsonify({"error": "照片数据暂时不可用，请稍后重试"}), 503


@app.teardown_request
def unlock_photo_lifecycle(_error):
    guard = g.pop("photo_guard", None)
    if guard is not None:
        guard.__exit__(None, None, None)


def all_photo_records(data: dict) -> list[dict]:
    seeds = []
    for index in range(1, 32):
        photo_id = f"seed-{index:02d}"
        state = data["seedStates"].get(photo_id, {})
        storage = data["seedOss"].get(photo_id, {})
        if photo_id in data["hiddenSeedIds"] and not state.get("deletedAt"):
            continue  # Legacy hidden photos are not silently restored or reclassified.
        if not (PUBLIC_DIR / "photos" / f"photo-{index:02d}.jpg").is_file() and not storage.get("objectKey") and not state.get("deletedAt"):
            continue
        seeds.append({"id": photo_id, "title": f"和宝宝的记忆 · {index:02d}", "year": "2026",
                      "src": f"/photos/photo-{index:02d}.jpg", "builtin": True,
                      **storage, **state,
                      **({"thumbnailSrc": f"/api/thumbnails/{photo_id}"} if storage.get("thumbnailKey") else {})})
    return seeds + data["photos"]


def public_photo_ids(data: dict) -> set[str]:
    return {item["id"] for item in all_photo_records(data) if not item.get("deletedAt")}


def photo_record(data: dict, photo_id: str) -> dict | None:
    return next((item for item in all_photo_records(data) if item["id"] == photo_id), None)


def image_available(photo_id: str) -> bool:
    item = photo_record(read_data(), photo_id)
    return bool(item and (not item.get("deletedAt") or is_admin()))


@app.get("/admin/trash")
def manage_trash_page():
    return send_from_directory(PUBLIC_DIR, "trash-admin.html")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/api/music/stream/"):
        response.headers["Cache-Control"] = "private, max-age=3600"
    elif request.path.startswith(("/api/images/", "/api/thumbnails/", "/photos/")):
        response.headers["Cache-Control"] = "private, no-cache" if response.status_code < 400 else "no-store"
    else:
        response.headers["Cache-Control"] = (
            "no-store" if request.path.startswith("/api/") else "public, max-age=300"
        )
    return response


@app.get("/")
def home():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/manage")
def manage():
    return send_from_directory(PUBLIC_DIR, "manage.html")


@app.get("/admin/book")
def manage_book():
    return send_from_directory(PUBLIC_DIR, "book-admin.html")


@app.get("/admin/music")
def manage_music_page():
    return send_from_directory(PUBLIC_DIR, "music-admin.html")


@app.get("/admin/comments")
def manage_comments_page():
    return send_from_directory(PUBLIC_DIR, "comments-admin.html")


@app.get("/music-player.js")
def music_player_script():
    return send_from_directory(PUBLIC_DIR, "music-player.js")


@app.get("/book-layouts.js")
def book_layout_script():
    return send_from_directory(PUBLIC_DIR, "book-layouts.js")


@app.get("/photos/<path:name>")
def seed_image(name: str):
    match = re.fullmatch(r"photo-(\d{2})\.jpg", name)
    if not match or not image_available(f"seed-{match.group(1)}"):
        return jsonify({"error": "照片不存在"}), 404
    if match and os.environ.get("PHOTO_RING_LEGACY_FIRST") != "1":
        photo_id = f"seed-{match.group(1)}"
        entry = read_data()["seedOss"].get(photo_id, {})
        if entry.get("objectKey"):
            try:
                return send_file(BytesIO(oss_storage.get(entry["objectKey"])), mimetype="image/webp")
            except Exception:
                app.logger.exception("OSS seed read failed for photo %s; using local copy", photo_id)
    return send_from_directory(PUBLIC_DIR / "photos", name)


@app.get("/api/images/<path:name>")
def uploaded_image(name: str):
    if not image_available(name):
        return jsonify({"error": "照片不存在"}), 404
    photo = next((item for item in read_data()["photos"] if item.get("id") == name), None)
    if photo and photo.get("objectKey") and os.environ.get("PHOTO_RING_LEGACY_FIRST") != "1":
        try:
            return send_file(BytesIO(oss_storage.get(photo["objectKey"])), mimetype=photo.get("mimeType", "image/webp"))
        except Exception:
            app.logger.exception("OSS original read failed for photo %s; trying local copy", name)
            if not (UPLOAD_DIR / name).is_file():
                return jsonify({"error": "照片暂时无法加载"}), 502
    return send_from_directory(UPLOAD_DIR, name)


@app.get("/api/thumbnails/<path:name>")
def uploaded_thumbnail(name: str):
    if not image_available(name):
        return jsonify({"error": "照片不存在"}), 404
    data = read_data()
    photo = next((item for item in data["photos"] if item.get("id") == name), None)
    seed = data["seedOss"].get(name, {}) if name in SEED_IDS else {}
    key = (photo or seed).get("thumbnailKey")
    local = (PUBLIC_DIR / "photos" / f"photo-{name[5:]}.jpg") if name in SEED_IDS else (UPLOAD_DIR / name)
    if os.environ.get("PHOTO_RING_LEGACY_FIRST") == "1" and local.is_file():
        return send_file(local)
    if not key:
        return jsonify({"error": "缩略图不存在"}), 404
    try:
        return send_file(BytesIO(oss_storage.get(key)), mimetype="image/webp")
    except Exception:
        app.logger.exception("OSS thumbnail read failed for photo %s; trying local copy", name)
        if local.is_file():
            return send_file(local)
        return jsonify({"error": "缩略图暂时无法加载"}), 502


@app.get("/api/photos")
def photos():
    data = read_data()
    active_ids = public_photo_ids(data)
    counts = {}
    for comment in readable_public_comments():
        if comment.get("status") == "approved" and comment.get("photoId") in active_ids:
            photo_id = comment.get("photoId")
            counts[photo_id] = counts.get(photo_id, 0) + 1
    public_fields = {"id", "title", "year", "src", "thumbnailSrc", "uploaded", "width", "height"}
    return jsonify({
        "photos": [{**{key: value for key, value in item.items() if key in public_fields},
                    "commentCount": counts.get(item["id"], 0)} for item in data["photos"] if item["id"] in active_ids],
        "hiddenSeedIds": sorted(SEED_IDS - active_ids),
        "commentCounts": counts,
    })


@app.get("/api/book-layout")
def book_layout():
    layout = read_book_layout()
    active_ids = public_photo_ids(read_data())
    ids = {page.get("note", {}).get("commentId") for pages in layout["years"].values()
           for page in pages if isinstance(page, dict) and isinstance(page.get("note"), dict)}
    layout["featuredComments"] = {item["id"]: public_comment(item) for item in readable_public_comments()
                                  if item.get("id") in ids and item.get("status") == "approved"
                                  and item.get("photoId") in active_ids}
    return jsonify(layout)


def readable_public_comments() -> list[dict]:
    try:
        return comment_store.read()
    except (OSError, ValueError, json.JSONDecodeError):
        app.logger.exception("Comments cannot be read; showing photos without comment data")
        return []


def public_comment(item: dict, own: bool = False) -> dict:
    result = {key: item[key] for key in ("id", "nickname", "content", "createdAt")}
    result["status"] = item["status"]
    if own and item["status"] in ("pending", "rejected"):
        result.update(status=item["status"], isOwnPending=item["status"] == "pending")
    return result


def visitor_hash(create: bool = False) -> str | None:
    token = request.headers.get("X-Visitor-Token", "")
    if token and (not 32 <= len(token) <= 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", token)):
        return None
    if not token:
        token = session.get("comment_visitor_token", "")
        if not token and create:
            token = secrets.token_urlsafe(32)
            session["comment_visitor_token"] = token
    return hashlib.sha256(token.encode()).hexdigest() if token else None


def photo_is_public(photo_id: str) -> bool:
    return photo_id in public_photo_ids(read_data())


@app.get("/api/photos/<photo_id>/comments")
def photo_comments(photo_id: str):
    if not photo_is_public(photo_id):
        return jsonify({"error": "照片不存在"}), 404
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 0
    if not 1 <= page <= 10000:
        return jsonify({"error": "页码不正确"}), 400
    owner = visitor_hash()
    photo_items = [item for item in comment_store.read() if item.get("photoId") == photo_id]
    items = [public_comment(item, item.get("visitorTokenHash") == owner and owner is not None)
             for item in photo_items if (item.get("status") == "approved" or
                  (owner and item.get("visitorTokenHash") == owner and item.get("status") in ("pending", "rejected")))]
    items.sort(key=lambda item: (item["createdAt"], item["id"]))
    return jsonify({"comments": items[(page - 1) * 20:page * 20], "total": len(items),
                    "page": page, "hasMore": page * 20 < len(items),
                    "commentCount": sum(item.get("status") == "approved" for item in photo_items)})


@app.post("/api/photos/<photo_id>/comments")
def add_comment(photo_id: str):
    if not photo_is_public(photo_id):
        return jsonify({"error": "照片不存在"}), 404
    if request.content_length and request.content_length > 4096:
        return jsonify({"error": "留言内容过长"}), 413
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "留言格式不正确"}), 400
    nickname, content = payload.get("nickname", ""), payload.get("content")
    if not isinstance(nickname, str) or not isinstance(content, str):
        return jsonify({"error": "留言格式不正确"}), 400
    nickname, content = nickname.strip() or "访客", content.strip()
    if not 1 <= len(nickname) <= 20 or not 1 <= len(content) <= 300:
        return jsonify({"error": "昵称最多 20 字，留言须为 1 至 300 字"}), 400
    if request.headers.get("X-Visitor-Token") and visitor_hash() is None:
        return jsonify({"error": "访客标识格式不正确"}), 400
    owner = visitor_hash(create=True)
    remote = request.headers.get("X-Real-IP") if request.remote_addr in ("127.0.0.1", "::1") else request.remote_addr
    remote = remote or "unknown"
    fingerprint = hmac.new(app.secret_key.encode(), remote.encode(), hashlib.sha256).hexdigest()
    now = datetime.now(timezone.utc)
    item = {"id": uuid.uuid4().hex, "photoId": photo_id, "nickname": nickname,
            "content": content, "createdAt": now.isoformat(), "updatedAt": now.isoformat(),
            "status": "pending", "visitorTokenHash": owner, "senderHash": fingerprint,
            "approvedAt": None, "rejectedAt": None}

    def change(items):
        recent = (entry for entry in reversed(items) if entry.get("senderHash") == fingerprint)
        previous = next(recent, None)
        if previous:
            try:
                if (now - datetime.fromisoformat(previous["createdAt"])).total_seconds() < 8:
                    return False
            except (ValueError, KeyError):
                pass
        items.append(item)
        return True

    if not comment_store.update(change):
        return jsonify({"error": "发送太频繁，请稍后再试"}), 429
    return jsonify({"comment": public_comment(item, own=True), "commentCount": sum(
        entry.get("photoId") == photo_id and entry.get("status") == "approved"
        for entry in comment_store.read())}), 201


@app.get("/api/manage/comments")
@admin_required
def manage_comments():
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 0
    if not 1 <= page <= 10000:
        return jsonify({"error": "页码不正确"}), 400
    all_items = comment_store.read()
    items = all_items
    photo_id = request.args.get("photoId", "")
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip().casefold()[:100]
    if photo_id:
        items = [item for item in items if item.get("photoId") == photo_id]
    if status in ("pending", "approved", "rejected", "hidden"):
        items = [item for item in items if item.get("status") == status]
    status_counts = {name: 0 for name in ("pending", "approved", "rejected", "hidden")}
    for item in all_items:
        if item.get("status") in status_counts:
            status_counts[item["status"]] += 1
    if search:
        items = [item for item in items if search in (item.get("nickname", "") + " " + item.get("content", "")).casefold()]
    items.sort(key=lambda item: (item.get("createdAt", ""), item.get("id", "")), reverse=True)
    fields = ("id", "photoId", "nickname", "content", "createdAt", "updatedAt", "status", "approvedAt", "rejectedAt")
    return jsonify({"comments": [{key: item.get(key) for key in fields} for item in items[(page - 1) * 20:page * 20]],
                    "total": len(items), "page": page, "hasMore": page * 20 < len(items),
                    "statusCounts": status_counts})


@app.patch("/api/manage/comments/<comment_id>")
@admin_required
def update_comment(comment_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("status") not in ("approved", "rejected", "hidden"):
        return jsonify({"error": "评论状态不正确"}), 400
    def change(items):
        item = next((entry for entry in items if entry.get("id") == comment_id), None)
        if item:
            old, new = item["status"], payload["status"]
            allowed = (old == new or (new == "approved" and old in ("pending", "rejected", "hidden"))
                       or (new == "rejected" and old == "pending")
                       or (new == "hidden" and old == "approved"))
            if not allowed:
                return False
            if old != new:
                now = datetime.now(timezone.utc).isoformat()
                item["status"] = new
                item["updatedAt"] = now
                if new == "approved":
                    item["approvedAt"] = now
                    item["rejectedAt"] = None
                elif new == "rejected":
                    item["rejectedAt"] = now
        return item
    item = comment_store.update(change)
    if item is False:
        return jsonify({"error": "不能这样变更评论状态"}), 409
    if item is None:
        return jsonify({"error": "评论不存在"}), 404
    return jsonify({"ok": True})


@app.get("/api/manage/comments/<comment_id>")
@admin_required
def manage_comment_detail(comment_id: str):
    item = next((entry for entry in comment_store.read() if entry.get("id") == comment_id), None)
    if not item:
        return jsonify({"error": "评论不存在"}), 404
    fields = ("id", "photoId", "nickname", "content", "createdAt", "updatedAt", "status", "approvedAt", "rejectedAt")
    return jsonify({"comment": {key: item.get(key) for key in fields}})


@app.delete("/api/manage/comments/<comment_id>")
@admin_required
def delete_comment(comment_id: str):
    def change(items):
        count = len(items)
        items[:] = [item for item in items if item.get("id") != comment_id]
        return len(items) < count
    if not comment_store.update(change):
        return jsonify({"error": "评论不存在"}), 404
    return jsonify({"ok": True})


def public_music(track: dict) -> dict:
    return {
        "id": track["id"], "title": track["title"], "artist": track.get("artist", ""),
        "duration": track.get("duration", 0), "url": f"/api/music/stream/{track['id']}",
    }


@app.get("/api/music/current")
def current_music():
    try:
        data = music_store.read()
        settings = data["settings"]
        track = next((item for item in data["music"] if item["id"] == settings["activeMusicId"]), None)
        enabled = bool(settings["musicEnabled"] and track)
        return jsonify({
            "enabled": enabled,
            "volume": settings["musicVolume"],
            "loop": settings["musicLoop"],
            "fadeInDuration": settings["musicFadeInDuration"],
            "music": public_music(track) if enabled else None,
        })
    except Exception:
        app.logger.exception("Music settings cannot be read")
        return jsonify({"enabled": False, "volume": 0.3, "loop": True,
                        "fadeInDuration": 3000, "music": None})


@app.get("/api/manage/music")
@admin_required
def manage_music():
    return jsonify(music_store.read())


@app.post("/api/manage/music")
@admin_required
def upload_music():
    file = request.files.get("music")
    title = request.form.get("title", "").strip()
    artist = request.form.get("artist", "").strip()
    if not file or not title or len(title) > 120 or len(artist) > 120:
        return jsonify({"error": "请选择 MP3，并填写 1 至 120 字的歌曲标题"}), 400
    original_name = Path((file.filename or "").replace("\\", "/")).name
    if Path(original_name).suffix.lower() != ".mp3" or file.mimetype not in (
        "audio/mpeg", "audio/mp3", "application/octet-stream", ""
    ):
        return jsonify({"error": "目前只支持 MP3 文件"}), 400
    raw = file.read(MAX_MUSIC_BYTES + 1)
    if not raw or len(raw) > MAX_MUSIC_BYTES:
        return jsonify({"error": "单首音乐不能超过 30 MB"}), 400
    try:
        from mutagen.mp3 import MP3
        duration = round(MP3(BytesIO(raw)).info.length, 2)
        if duration <= 0 or duration > 24 * 3600:
            raise ValueError("invalid duration")
    except Exception:
        return jsonify({"error": "无法读取这首 MP3，请检查文件是否完整"}), 400
    if not oss_storage.configured():
        return jsonify({"error": "OSS 暂时不可用，音乐未上传"}), 503
    track_id = uuid.uuid4().hex
    key = f"music/{time.gmtime().tm_year}/{track_id}.mp3"
    track = {
        "id": track_id, "title": title, "artist": artist, "objectKey": key,
        "duration": duration, "fileSize": len(raw), "mimeType": "audio/mpeg",
        "originalName": original_name[:255],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        oss_storage.put(key, raw, "audio/mpeg")
        music_store.update(lambda data: data["music"].append(track))
    except Exception:
        app.logger.exception("Music upload failed")
        try:
            oss_storage.delete(key)
        except Exception:
            app.logger.exception("Music upload rollback failed for %s", key)
        return jsonify({"error": "音乐上传失败，请稍后重试"}), 502
    return jsonify({"ok": True, "music": track}), 201


@app.put("/api/manage/music/settings")
@admin_required
def update_music_settings():
    changes = request.get_json(silent=True)
    if not isinstance(changes, dict) or not changes or set(changes) - {
        "musicEnabled", "activeMusicId", "musicVolume", "musicLoop"
    }:
        return jsonify({"error": "音乐设置格式不正确"}), 400
    if "musicEnabled" in changes and type(changes["musicEnabled"]) is not bool:
        return jsonify({"error": "开启状态不正确"}), 400
    if "musicLoop" in changes and type(changes["musicLoop"]) is not bool:
        return jsonify({"error": "循环状态不正确"}), 400
    if "musicVolume" in changes and (type(changes["musicVolume"]) not in (int, float)
                                     or not 0 <= changes["musicVolume"] <= 1):
        return jsonify({"error": "音量必须在 0 到 100% 之间"}), 400
    if "activeMusicId" in changes and changes["activeMusicId"] is not None and not isinstance(changes["activeMusicId"], str):
        return jsonify({"error": "歌曲 ID 不正确"}), 400

    def change(data):
        next_settings = {**data["settings"], **changes}
        if next_settings["activeMusicId"] is not None and not any(
            item["id"] == next_settings["activeMusicId"] for item in data["music"]
        ):
            raise ValueError("没有找到这首音乐")
        if next_settings["activeMusicId"] is None:
            next_settings["musicEnabled"] = False
        data["settings"] = next_settings
        return next_settings

    try:
        settings = music_store.update(change)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"ok": True, "settings": settings})


@app.delete("/api/manage/music/<track_id>")
@admin_required
def delete_music(track_id: str):
    def change(data):
        track = next((item for item in data["music"] if item["id"] == track_id), None)
        if track is None:
            raise KeyError(track_id)
        data["music"] = [item for item in data["music"] if item["id"] != track_id]
        if data["settings"]["activeMusicId"] == track_id:
            data["settings"]["activeMusicId"] = None
            data["settings"]["musicEnabled"] = False
        data["pendingOssDeletes"].append(track["objectKey"])
        return track["objectKey"]

    try:
        key = music_store.update(change)
    except KeyError:
        return jsonify({"error": "没有找到这首音乐"}), 404
    try:
        oss_storage.delete(key)
        music_store.update(lambda data: data["pendingOssDeletes"].remove(key)
                           if key in data["pendingOssDeletes"] else None)
        return jsonify({"ok": True, "cleanupPending": False})
    except Exception:
        app.logger.exception("Music OSS cleanup pending for %s", key)
        return jsonify({"ok": True, "cleanupPending": True})


@app.post("/api/manage/music/retry-deletes")
@admin_required
def retry_music_deletes():
    keys = list(music_store.read()["pendingOssDeletes"])
    cleared = []
    for key in keys:
        try:
            oss_storage.delete(key)
            cleared.append(key)
        except Exception:
            app.logger.exception("Music OSS cleanup retry failed for %s", key)
    if cleared:
        def remove_cleared(data):
            data["pendingOssDeletes"] = [key for key in data["pendingOssDeletes"] if key not in cleared]
        music_store.update(remove_cleared)
    return jsonify({"ok": True, "deleted": len(cleared), "pending": len(keys) - len(cleared)})


@app.route("/api/music/stream/<track_id>", methods=["GET", "HEAD"])
def stream_music(track_id: str):
    try:
        data = music_store.read()
        track = next((item for item in data["music"] if item["id"] == track_id), None)
        if not track or not (is_admin() or (data["settings"]["musicEnabled"]
                                             and data["settings"]["activeMusicId"] == track_id)):
            return jsonify({"error": "音乐不存在"}), 404
        total = oss_storage.size(track["objectKey"])
        if total <= 0:
            raise ValueError("Empty music object")
        requested = request.headers.get("Range")
        start, end, partial = 0, total - 1, False
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
            if not match or not (match[1] or match[2]):
                return Response(status=416, headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"})
            if match[1]:
                start = int(match[1])
                end = int(match[2]) if match[2] else total - 1
            else:
                length = int(match[2])
                start = max(0, total - length)
            if start >= total or end < start:
                return Response(status=416, headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"})
            end = min(end, total - 1, start + 1024 * 1024 - 1)
            partial = True
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
                   "Content-Type": "audio/mpeg"}
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        if request.method == "HEAD":
            return Response(status=206 if partial else 200, headers=headers)
        source = (oss_storage.open_range(track["objectKey"], start, end) if partial
                  else oss_storage.open_range(track["objectKey"]))

        def chunks():
            try:
                while True:
                    piece = source.read(64 * 1024)
                    if not piece:
                        break
                    yield piece
            finally:
                response = getattr(getattr(source, "resp", None), "response", None)
                if response is not None:
                    response.close()

        return Response(stream_with_context(chunks()), status=206 if partial else 200, headers=headers)
    except Exception:
        app.logger.exception("Music stream failed for %s", track_id)
        return jsonify({"error": "音乐暂时无法播放"}), 502


@app.put("/api/manage/book-layout")
@admin_required
def save_book_layout():
    try:
        value = validate_book_layout(request.get_json(silent=True))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    old_pages = {page.get("id"): page for pages in read_book_layout()["years"].values()
                 for page in pages if isinstance(page, dict)}
    active_ids = public_photo_ids(read_data())
    approved_ids = {item["id"] for item in comment_store.read() if item.get("status") == "approved"
                    and item.get("photoId") in active_ids}
    for pages in value["years"].values():
        for page in pages:
            old_refs = {(ref["photoId"], ref["slot"]) for ref in old_pages.get(page["id"], {}).get("photos", [])}
            if any(ref["photoId"] not in active_ids and (ref["photoId"], ref["slot"]) not in old_refs
                   for ref in page["photos"]):
                return jsonify({"error": "照片已不可用，请刷新排版后重试"}), 409
            note = page.get("note", {})
            if note.get("type") != "comment":
                continue
            old_note = old_pages.get(page["id"], {}).get("note", {})
            if note.get("commentId") != old_note.get("commentId") and note["commentId"] not in approved_ids:
                return jsonify({"error": "只有审核通过的评论才能设为书本留言"}), 400
    with data_lock:
        temporary = BOOK_LAYOUT_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(BOOK_LAYOUT_FILE)
    return jsonify({"ok": True})


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/login")
def login():
    password_hash = os.environ.get("PHOTO_RING_PASSWORD_HASH", "")
    supplied = (request.get_json(silent=True) or {}).get("password", "")
    if not password_hash or not check_password_hash(password_hash, supplied):
        time.sleep(0.5)
        return jsonify({"error": "密码不正确"}), 401
    session.clear()
    session["admin"] = True
    return jsonify({"ok": True})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/manage/photos")
@admin_required
def manage_photos():
    data = read_data()
    items = all_photo_records(data)
    include_trash = request.args.get("includeTrash") == "1"
    fields = {"id", "title", "year", "src", "thumbnailSrc", "builtin", "uploaded", "deletedAt", "purgeStartedAt"}
    return jsonify({"photos": [{key: value for key, value in item.items() if key in fields}
                               for item in items if include_trash or not item.get("deletedAt")]})


def save_image(file_storage) -> tuple[str, str]:
    raw = file_storage.read()
    if not raw:
        raise ValueError("照片内容为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("单张照片不能超过 35MB")

    try:
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, "JPEG", quality=90, optimize=True)
    except Exception as error:
        raise ValueError("无法读取这张照片，请换成 JPG、PNG、WebP 或 HEIC") from error

    stem = secure_filename(Path(file_storage.filename or "photo").stem) or "photo"
    photo_id = f"{int(time.time() * 1000)}-{secrets.token_hex(8)}.jpg"
    (UPLOAD_DIR / photo_id).write_bytes(output.getvalue())
    return photo_id, stem


def save_oss_image(file_storage, year: str, uploaded_keys: list[str]) -> dict:
    """Convert one file and upload two web variants; never write a second local copy."""
    extension = Path(file_storage.filename or "").suffix.lower()
    if extension not in ALLOWED_IMAGE_TYPES or (file_storage.mimetype or "").lower() not in ALLOWED_MIME_TYPES:
        raise ValueError("只支持 JPG、PNG、WebP 或 HEIC 照片")
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("单张照片必须小于 35MB")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in ALLOWED_IMAGE_TYPES[extension] or source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("照片格式或尺寸不符合要求")
            image = ImageOps.exif_transpose(source)
            image.load()
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.thumbnail((3200, 3200), Image.Resampling.LANCZOS)
        width, height = image.size
        original = BytesIO()
        image.save(original, "WEBP", quality=90, method=4)
        thumbnail = image.copy()
        thumbnail.thumbnail((600, 600), Image.Resampling.LANCZOS)
        preview = BytesIO()
        thumbnail.save(preview, "WEBP", quality=82, method=4)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("无法读取这张照片，请换成 JPG、PNG、WebP 或 HEIC") from error

    token = uuid.uuid4().hex
    photo_id = token + ".webp"
    original_key = f"photos/originals/{year}/{token}.webp"
    thumbnail_key = f"photos/thumbnails/{year}/{token}.webp"
    uploaded_keys.append(original_key)
    oss_storage.put(original_key, original.getvalue(), "image/webp")
    uploaded_keys.append(thumbnail_key)
    oss_storage.put(thumbnail_key, preview.getvalue(), "image/webp")
    return {
        "id": photo_id,
        "title": secure_filename(Path(file_storage.filename or "photo").stem) or "photo",
        "year": year,
        "src": f"/api/images/{photo_id}",
        "thumbnailSrc": f"/api/thumbnails/{photo_id}",
        "objectKey": original_key,
        "thumbnailKey": thumbnail_key,
        "mimeType": "image/webp",
        "width": width,
        "height": height,
        "fileSize": len(original.getvalue()),
        "uploaded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def delete_oss_keys(keys: list[str]) -> list[str]:
    failed = []
    for key in keys:
        try:
            oss_storage.delete(key)
        except Exception:
            app.logger.exception("OSS cleanup failed for object %s", key)
            failed.append(key)
    return failed


@app.get("/api/manage/oss/status")
@admin_required
def oss_status():
    return jsonify({
        "configured": oss_storage.configured(),
        "uploadMode": os.environ.get("OSS_UPLOAD_MODE", "local"),
        "testUploadEnabled": os.environ.get("OSS_TEST_UPLOAD_ENABLED") == "1",
        "region": os.environ.get("OSS_REGION", ""),
        "bucket": os.environ.get("OSS_BUCKET", ""),
    })


@app.post("/api/manage/upload")
@admin_required
def upload():
    year = request.form.get("year", "2026")
    if year not in ALLOWED_YEARS:
        return jsonify({"error": "年份只能选择 2026 或 2027"}), 400
    incoming = request.files.getlist("photos")
    if not incoming:
        return jsonify({"error": "请选择照片"}), 400

    storage_request = request.form.get("storage", "")
    if storage_request and storage_request != "oss-test":
        return jsonify({"error": "上传方式不正确"}), 400
    if storage_request == "oss-test" and os.environ.get("OSS_TEST_UPLOAD_ENABLED") != "1":
        return jsonify({"error": "OSS 测试上传尚未启用"}), 403
    use_oss = storage_request == "oss-test" or os.environ.get("OSS_UPLOAD_MODE", "local") == "oss"
    if use_oss and not oss_storage.configured():
        return jsonify({"error": "照片上传暂时不可用，请稍后重试"}), 503

    saved = []
    uploaded_keys = []
    try:
        for file_storage in incoming:
            if use_oss:
                saved.append(save_oss_image(file_storage, year, uploaded_keys))
            else:
                photo_id, fallback_title = save_image(file_storage)
                saved.append({
                    "id": photo_id, "title": fallback_title, "year": year,
                    "src": f"/api/images/{photo_id}",
                    "uploaded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
    except ValueError as error:
        for item in saved:
            if not item.get("objectKey"):
                (UPLOAD_DIR / item["id"]).unlink(missing_ok=True)
        delete_oss_keys(uploaded_keys)
        return jsonify({"error": str(error)}), 400
    except Exception:
        app.logger.exception("Photo storage failed")
        for item in saved:
            if not item.get("objectKey"):
                (UPLOAD_DIR / item["id"]).unlink(missing_ok=True)
        delete_oss_keys(uploaded_keys)
        return jsonify({"error": "照片上传失败，请重试"}), 502

    try:
        with data_lock:
            data = read_data()
            data["photos"].extend(saved)
            for item in saved:
                item.setdefault("deletedAt", None)
            write_data(data)
    except Exception:
        app.logger.exception("Photo metadata write failed")
        for item in saved:
            if not item.get("objectKey"):
                (UPLOAD_DIR / item["id"]).unlink(missing_ok=True)
        delete_oss_keys(uploaded_keys)
        return jsonify({"error": "照片上传失败，请重试"}), 500
    return jsonify({"ok": True, "count": len(saved), "photos": saved})


@app.delete("/api/manage/photos/<photo_id>")
@admin_required
def delete_photo(photo_id: str):
    data = read_data()
    target = photo_record(data, photo_id)
    if not target:
        return jsonify({"error": "没有找到这张照片"}), 404
    state = data["seedStates"].setdefault(photo_id, {}) if photo_id in SEED_IDS else target
    if not state.get("deletedAt"):
        state["deletedAt"] = datetime.now(timezone.utc).isoformat()
        write_data(data)
    return jsonify({"ok": True})


@app.get("/api/manage/trash/photos")
@admin_required
def trash_photos():
    items = [item for item in all_photo_records(read_data()) if item.get("deletedAt")]
    items.sort(key=lambda item: (item["deletedAt"], item["id"]), reverse=True)
    comments = comment_store.read()
    counts = {}
    for item in comments:
        counts[item["photoId"]] = counts.get(item["photoId"], 0) + 1
    refs = {ref["photoId"] for pages in read_book_layout()["years"].values()
            for page in pages for ref in page.get("photos", [])}
    fields = {"id", "title", "year", "src", "thumbnailSrc", "builtin", "deletedAt", "purgeStartedAt"}
    return jsonify({"photos": [{**{key: value for key, value in item.items() if key in fields},
                                "commentTotal": counts.get(item["id"], 0), "bookReferenced": item["id"] in refs}
                               for item in items], "total": len(items)})


@app.post("/api/manage/photos/<photo_id>/restore")
@admin_required
def restore_photo(photo_id: str):
    data = read_data()
    target = photo_record(data, photo_id)
    if not target:
        return jsonify({"error": "没有找到这张照片"}), 404
    if target.get("purgeStartedAt"):
        return jsonify({"error": "永久清理已开始，部分资源可能已删除，请重试永久删除"}), 409
    state = data["seedStates"].get(photo_id, {}) if photo_id in SEED_IDS else target
    state["deletedAt"] = None
    write_data(data)
    return jsonify({"ok": True})


@app.delete("/api/manage/trash/photos/<photo_id>")
@admin_required
def purge_photo(photo_id: str):
    data = read_data()
    target = photo_record(data, photo_id)
    if not target:
        return jsonify({"error": "没有找到这张照片"}), 404
    if not target.get("deletedAt"):
        return jsonify({"error": "请先将照片移到回收站"}), 409
    keys = list(dict.fromkeys(key for key in (target.get("objectKey"), target.get("thumbnailKey")) if key))
    # Check all mappings, including legacy hidden seeds, before deleting a key.
    other_records = [item for item in data["photos"] if item["id"] != photo_id]
    other_records += [item for key, item in data["seedOss"].items() if key != photo_id]
    shared = {item.get(field) for item in other_records for field in ("objectKey", "thumbnailKey")}
    if any(not isinstance(key, str) or not re.fullmatch(r'photos/(originals|thumbnails)/(2026|2027)/[A-Za-z0-9_.-]+', key)
           or key in shared or key in data["pendingOssDeletes"] for key in keys):
        app.logger.error("Photo purge blocked: unsafe or shared resource photoId=%s", photo_id)
        return jsonify({"error": "照片资源关联需要人工核查，未执行永久删除"}), 409
    base = PUBLIC_DIR / "photos" if photo_id in SEED_IDS else UPLOAD_DIR
    local = base / (f"photo-{photo_id[5:]}.jpg" if photo_id in SEED_IDS else photo_id)
    if local.parent.resolve() != base.resolve() or local.is_symlink():
        return jsonify({"error": "照片文件路径需要人工核查"}), 409
    # Prepare/validate every JSON relationship before irreversible resource removal.
    with comment_store._locked():
        try:
            comments = comment_store.read()
            removed_ids = {item["id"] for item in comments if item.get("photoId") == photo_id}
            remaining = [item for item in comments if item.get("photoId") != photo_id]
            book = None
            if BOOK_LAYOUT_FILE.exists():
                book = json.loads(BOOK_LAYOUT_FILE.read_text(encoding="utf-8"))
                for pages in book["years"].values():
                    for page in pages:
                        # Empty slots are represented by absence from photos[], preserving page/slot numbering.
                        page["photos"] = [ref for ref in page.get("photos", []) if ref.get("photoId") != photo_id]
                        if page.get("note", {}).get("type") == "comment" and page["note"].get("commentId") in removed_ids:
                            page.pop("note")
        except (ValueError, KeyError, TypeError, AttributeError):
            app.logger.error("Photo purge blocked: invalid relationship metadata photoId=%s", photo_id)
            return jsonify({"error": "照片关联数据需要人工核查，未执行永久删除"}), 409
        state = data["seedStates"].setdefault(photo_id, {}) if photo_id in SEED_IDS else target
        try:
            if not state.get("purgeStartedAt"):
                state["purgeStartedAt"] = datetime.now(timezone.utc).isoformat()
                state["purgeCompletedKeys"] = []
                write_data(data)  # Durable retry identity before deleting the first object.
            for key in keys:
                if key in state["purgeCompletedKeys"]:
                    continue
                try:
                    oss_storage.delete(key)
                except Exception as error:
                    if getattr(error, "code", None) != "NoSuchKey":
                        raise
                state["purgeCompletedKeys"].append(key)
                write_data(data)
            local.unlink(missing_ok=True)
            if photo_id in SEED_IDS:
                data["seedStates"].pop(photo_id, None)
                data["seedOss"].pop(photo_id, None)
                data["hiddenSeedIds"] = sorted(set(data["hiddenSeedIds"]) | {photo_id})
            else:
                data["photos"] = [item for item in data["photos"] if item["id"] != photo_id]
            photo_lifecycle.commit(DATA_FILE, BOOK_LAYOUT_FILE, comment_store.DATA_FILE, data, book, remaining)
        except Exception as error:
            app.logger.error("Photo purge incomplete photoId=%s result=retry-required errorType=%s", photo_id, type(error).__name__)
            return jsonify({"error": "永久删除未完成，记录和清理进度已保留，请重试。此照片暂不能恢复。"}), 502
    app.logger.info("Photo purge completed photoId=%s result=success", photo_id)
    return jsonify({"ok": True})


@app.post("/api/manage/oss/retry-deletes")
@admin_required
def retry_oss_deletes():
    snapshot = read_data()
    referenced = {item.get(field) for item in snapshot["photos"] + list(snapshot["seedOss"].values())
                  for field in ("objectKey", "thumbnailKey")}
    keys = [key for key in snapshot["pendingOssDeletes"] if key not in referenced]
    failed = delete_oss_keys(keys)
    if len(failed) != len(keys):
        with data_lock:
            data = read_data()
            for key in keys:
                if key not in failed and key in data["pendingOssDeletes"]:
                    data["pendingOssDeletes"].remove(key)
            write_data(data)
    return jsonify({"ok": True, "deleted": len(keys) - len(failed), "pending": len(read_data()["pendingOssDeletes"])})


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "一次上传的文件太大，请分批上传"}), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
