"""Comment identity, moderation and book references stay independent of photos."""

import importlib
import os
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash


class CommentsTest(unittest.TestCase):
    def test_public_comments_moderation_and_book_reference(self):
        site = importlib.import_module("app")
        store = importlib.import_module("comment_store")
        original = site.DATA_FILE, site.BOOK_LAYOUT_FILE, store.DATA_FILE, store.LOCK_FILE
        previous_hash = os.environ.get("PHOTO_RING_PASSWORD_HASH")
        with tempfile.TemporaryDirectory(dir=site.BASE_DIR) as directory:
            root = Path(directory)
            site.DATA_FILE, site.BOOK_LAYOUT_FILE = root / "photos.json", root / "book-layout.json"
            store.DATA_FILE, store.LOCK_FILE = root / "comments.json", root / "comments.lock"
            os.environ["PHOTO_RING_PASSWORD_HASH"] = generate_password_hash("test-password")
            try:
                client = site.app.test_client()
                photo_id = "seed-01"
                self.assertEqual(client.get(f"/api/photos/{photo_id}/comments").json["total"], 0)
                self.assertEqual(client.post(f"/api/photos/{photo_id}/comments", json={"content": "   "}).status_code, 400)
                self.assertEqual(client.post(f"/api/photos/{photo_id}/comments", json={"content": "x" * 301}).status_code, 400)
                self.assertEqual(client.post("/api/photos/missing/comments", json={"content": "x"}).status_code, 404)
                self.assertEqual(client.get("/api/manage/comments").status_code, 401)
                self.assertEqual(client.patch("/api/manage/comments/unknown", json={"status": "hidden"}).status_code, 401)
                self.assertEqual(client.delete("/api/manage/comments/unknown").status_code, 401)

                response = client.post(f"/api/photos/{photo_id}/comments",
                                       json={"nickname": " 淼淼 ", "content": " <script>alert(1)</script> "})
                self.assertEqual(response.status_code, 201)
                comment_id = response.json["comment"]["id"]
                self.assertEqual(response.json["comment"]["nickname"], "淼淼")
                self.assertNotIn("senderHash", response.json["comment"])
                self.assertEqual(client.post(f"/api/photos/{photo_id}/comments",
                                             json={"content": "又一条"}).status_code, 429)
                self.assertEqual(client.get(f"/api/photos/{photo_id}/comments").json["comments"][0]["content"],
                                 "<script>alert(1)</script>")
                self.assertEqual(response.json["comment"]["status"], "pending")
                self.assertEqual(client.get("/api/photos").json["commentCounts"].get(photo_id, 0), 0)

                self.assertEqual(client.post("/api/login", json={"password": "test-password"}).status_code, 200)
                self.assertEqual(client.patch(f"/api/manage/comments/{comment_id}", json={"status": "approved"}).status_code, 200)
                self.assertEqual(client.get("/api/photos").json["commentCounts"][photo_id], 1)
                layout = {"years": {"2026": [{"id": "p1", "layout": "note-left-photo",
                                              "photos": [{"photoId": photo_id, "slot": 1}],
                                              "note": {"type": "comment", "commentId": comment_id}},
                                             {"id": "p2", "layout": "note-only", "photos": [],
                                              "note": {"type": "manual", "text": " 那天很好 ", "author": "凡凡"}}],
                                    "2027": []}}
                self.assertEqual(client.put("/api/manage/book-layout", json=layout).status_code, 200)
                invalid_layout = {"years": {"2026": [{"id": "bad-note", "layout": "note-only", "photos": [],
                                                       "note": {"type": "manual", "text": " "}}], "2027": []}}
                self.assertEqual(client.put("/api/manage/book-layout", json=invalid_layout).status_code, 400)
                self.assertEqual(client.get("/api/book-layout").json["featuredComments"][comment_id]["nickname"], "淼淼")
                self.assertEqual(client.get("/api/book-layout").json["years"]["2026"][1]["note"]["text"], "那天很好")
                self.assertEqual(client.patch(f"/api/manage/comments/{comment_id}", json={"status": "hidden"}).status_code, 200)
                self.assertEqual(client.get(f"/api/photos/{photo_id}/comments").json["total"], 0)
                self.assertNotIn(comment_id, client.get("/api/book-layout").json["featuredComments"])
                self.assertEqual(client.get("/api/manage/comments").json["comments"][0]["status"], "hidden")
                self.assertEqual(client.patch(f"/api/manage/comments/{comment_id}", json={"status": "approved"}).status_code, 200)
                self.assertEqual(client.delete(f"/api/manage/comments/{comment_id}").status_code, 200)
                self.assertNotIn(comment_id, client.get("/api/book-layout").json["featuredComments"])
                self.assertEqual(client.get("/api/book-layout").json["years"]["2026"][0]["note"]["commentId"], comment_id)
                self.assertEqual(client.get("/api/photos").json["commentCounts"].get(photo_id, 0), 0)
                self.assertEqual(site.DATA_FILE.read_text(encoding="utf-8").find("comment"), -1)
            finally:
                site.DATA_FILE, site.BOOK_LAYOUT_FILE, store.DATA_FILE, store.LOCK_FILE = original
                if previous_hash is None:
                    os.environ.pop("PHOTO_RING_PASSWORD_HASH", None)
                else:
                    os.environ["PHOTO_RING_PASSWORD_HASH"] = previous_hash


if __name__ == "__main__":
    unittest.main()
