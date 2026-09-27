"""Ownership isolation, review transitions, legacy data and book reference gates."""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash


class ModerationTest(unittest.TestCase):
    def setUp(self):
        self.site = importlib.import_module("app")
        self.store = importlib.import_module("comment_store")
        self.original = self.site.DATA_FILE, self.site.BOOK_LAYOUT_FILE, self.store.DATA_FILE, self.store.LOCK_FILE
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.site.DATA_FILE, self.site.BOOK_LAYOUT_FILE = root / "photos.json", root / "book.json"
        self.store.DATA_FILE, self.store.LOCK_FILE = root / "comments.json", root / "comments.lock"
        self.password_hash = os.environ.get("PHOTO_RING_PASSWORD_HASH")
        os.environ["PHOTO_RING_PASSWORD_HASH"] = generate_password_hash("test-password")
        self.a, self.b, self.admin = [self.site.app.test_client() for _ in range(3)]
        self.admin.post("/api/login", json={"password": "test-password"})
        self.headers = {"X-Visitor-Token": "a" * 64}
        self.other_headers = {"X-Visitor-Token": "b" * 64}
        self.url = "/api/photos/seed-01/comments"

    def tearDown(self):
        self.site.DATA_FILE, self.site.BOOK_LAYOUT_FILE, self.store.DATA_FILE, self.store.LOCK_FILE = self.original
        if self.password_hash is None:
            os.environ.pop("PHOTO_RING_PASSWORD_HASH", None)
        else:
            os.environ["PHOTO_RING_PASSWORD_HASH"] = self.password_hash
        self.directory.cleanup()

    def create(self, headers=None):
        response = self.a.post(self.url, headers=headers or self.headers,
                               json={"nickname": "访客 A", "content": "<script>alert(1)</script>那天真好"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["commentCount"], 0)
        self.assertTrue(response.json["comment"]["isOwnPending"])
        return response.json["comment"]["id"]

    def change(self, comment_id, status):
        return self.admin.patch("/api/manage/comments/" + comment_id, json={"status": status})

    def count(self):
        return self.b.get("/api/photos").json["commentCounts"].get("seed-01", 0)

    def book(self, comment_id, page_id="page1"):
        return {"years": {"2026": [{"id": page_id, "layout": "note-only", "photos": [],
                                    "note": {"type": "comment", "commentId": comment_id}}], "2027": []}}

    def test_owner_isolation_persistence_and_review_lifecycle(self):
        cid = self.create()
        saved = self.store.read()[0]
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(len(saved["visitorTokenHash"]), 64)
        self.assertNotEqual(saved["visitorTokenHash"], self.headers["X-Visitor-Token"])
        self.assertNotIn("visitorToken", saved)
        self.assertEqual(self.a.get(self.url, headers=self.headers).json["comments"][0]["status"], "pending")
        # A new browser session with the same retained token still owns it, even after an IP change.
        fresh = self.site.app.test_client()
        self.assertEqual(fresh.get(self.url, headers=self.headers, environ_overrides={"REMOTE_ADDR": "10.2.3.4"}).json["total"], 1)
        for headers in ({}, self.other_headers, {"X-Visitor-Token": "invalid"}):
            self.assertEqual(self.b.get(self.url, headers=headers).json["total"], 0)
        self.assertEqual(self.count(), 0)
        listing = self.admin.get("/api/manage/comments?status=pending").json
        self.assertEqual(listing["statusCounts"]["pending"], 1)
        self.assertEqual(listing["total"], 1)
        for payload in (listing, self.admin.get("/api/manage/comments/" + cid).json,
                        self.a.get(self.url, headers=self.headers).json):
            self.assertNotIn("visitorToken", json.dumps(payload))
            self.assertNotIn("senderHash", json.dumps(payload))
        self.assertEqual(self.admin.put("/api/manage/book-layout", json=self.book(cid)).status_code, 400)
        self.assertEqual(self.change(cid, "hidden").status_code, 409)
        self.assertEqual(self.change(cid, "rejected").status_code, 200)
        self.assertEqual(self.a.get(self.url, headers=self.headers).json["comments"][0]["status"], "rejected")
        self.assertEqual(self.b.get(self.url).json["total"], 0)
        self.assertEqual(self.count(), 0)
        self.assertTrue(self.store.read()[0]["rejectedAt"])
        self.assertEqual(self.admin.put("/api/manage/book-layout", json=self.book(cid)).status_code, 400)
        self.assertEqual(self.change(cid, "approved").status_code, 200)
        approved_at = self.store.read()[0]["approvedAt"]
        self.assertTrue(approved_at)
        self.assertEqual(self.change(cid, "approved").status_code, 200)
        self.assertEqual(self.store.read()[0]["approvedAt"], approved_at)
        self.assertEqual(self.b.get(self.url).json["total"], 1)
        self.assertEqual(self.a.get(self.url, headers=self.headers).json["comments"][0]["status"], "approved")
        self.assertEqual(self.b.get(self.url).json["commentCount"], 1)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.admin.put("/api/manage/book-layout", json=self.book(cid)).status_code, 200)
        self.assertIn(cid, self.b.get("/api/book-layout").json["featuredComments"])
        self.assertEqual(self.change(cid, "hidden").status_code, 200)
        self.assertEqual(self.a.get(self.url, headers=self.headers).json["total"], 0)
        self.assertEqual(self.count(), 0)
        self.assertNotIn(cid, self.b.get("/api/book-layout").json["featuredComments"])
        # Saving an existing hidden/missing reference remains possible; creating a new one is blocked.
        self.assertEqual(self.admin.put("/api/manage/book-layout", json=self.book(cid)).status_code, 200)
        self.assertEqual(self.admin.put("/api/manage/book-layout", json=self.book(cid, "new-page")).status_code, 400)
        self.assertEqual(self.change(cid, "approved").status_code, 200)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.admin.delete("/api/manage/comments/" + cid).status_code, 200)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.b.get("/api/book-layout").json["featuredComments"], {})
        self.assertEqual(self.b.get("/api/book-layout").json["years"]["2026"][0]["note"]["commentId"], cid)

    def test_auth_rate_limit_and_input_validation(self):
        for content in (" ", "x" * 301):
            self.assertEqual(self.a.post(self.url, headers=self.headers, json={"content": content}).status_code, 400)
        self.assertEqual(self.a.post(self.url, headers=self.headers, json={"content": "x", "nickname": "n" * 21}).status_code, 400)
        self.assertEqual(self.a.post(self.url, headers={"X-Visitor-Token": "short"}, json={"content": "x"}).status_code, 400)
        cid = self.create()
        self.assertEqual(self.b.post(self.url, headers=self.other_headers, json={"content": "spam"}).status_code, 429)
        for status in ("approved", "rejected", "hidden"):
            self.assertEqual(self.b.patch("/api/manage/comments/" + cid, json={"status": status}).status_code, 401)
        self.assertEqual(self.b.get("/api/manage/comments").status_code, 401)
        self.assertEqual(self.b.delete("/api/manage/comments/" + cid).status_code, 401)
        self.assertEqual(self.b.put("/api/manage/book-layout", json=self.book(cid)).status_code, 401)

    def test_legacy_migration_is_incremental_backed_up_and_repeatable(self):
        from scripts.migrate_comment_moderation import migrate
        records = [{"id": "old-1", "photoId": "seed-01", "nickname": "原昵称", "content": "原留言",
                    "createdAt": "2026-09-01T00:00:00+00:00", "status": "visible"},
                   {"id": "old-2", "photoId": "seed-01", "nickname": "原昵称", "content": "原留言",
                    "createdAt": "2026-09-01T00:00:00+00:00"},
                   {"id": "old-3", "photoId": "seed-01", "nickname": "原昵称", "content": "隐藏留言",
                    "createdAt": "2026-09-01T00:00:00+00:00", "status": "hidden"}]
        raw = json.dumps(records, ensure_ascii=False)
        self.store.DATA_FILE.write_text(raw, encoding="utf-8")
        self.assertEqual(self.count(), 2)
        self.assertEqual(migrate(self.store.DATA_FILE)["changed"], 3)
        self.assertEqual(self.store.DATA_FILE.read_text(encoding="utf-8"), raw)
        result = migrate(self.store.DATA_FILE, apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), raw)
        updated = self.store.read()
        self.assertEqual([item["status"] for item in updated], ["approved", "approved", "hidden"])
        for before, after in zip(records, updated):
            for key in ("id", "photoId", "nickname", "content", "createdAt"):
                self.assertEqual(before[key], after[key])
        self.assertEqual(migrate(self.store.DATA_FILE, apply=True)["changed"], 0)

    def test_pagination_filters_do_not_leak_private_comments(self):
        self.store.update(lambda items: items.extend([
            {"id": str(i), "photoId": "seed-01", "nickname": "测试", "content": "test",
             "createdAt": f"2026-09-01T00:00:{i:02d}+00:00", "status": "approved" if i < 23 else "pending",
             "visitorTokenHash": "other-owner"} for i in range(25)]))
        first = self.b.get(self.url).json
        self.assertEqual((len(first["comments"]), first["total"], first["hasMore"]), (20, 23, True))
        self.assertEqual(len(self.b.get(self.url + "?page=2").json["comments"]), 3)
        pending = self.admin.get("/api/manage/comments?status=pending").json
        self.assertEqual(pending["total"], 2)
        self.assertTrue(all(item["status"] == "pending" for item in pending["comments"]))


if __name__ == "__main__":
    unittest.main()
