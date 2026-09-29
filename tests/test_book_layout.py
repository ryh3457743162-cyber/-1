"""Book layout references stay independent of the existing photo storage."""

import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from werkzeug.security import generate_password_hash


class BookLayoutTest(unittest.TestCase):
    def test_layout_save_upload_and_delete_are_independent(self):
        site = importlib.import_module("app")
        original_data, original_book, original_upload = site.DATA_FILE, site.BOOK_LAYOUT_FILE, site.UPLOAD_DIR
        previous_hash = os.environ.get("PHOTO_RING_PASSWORD_HASH")
        with tempfile.TemporaryDirectory(dir=site.BASE_DIR) as directory:
            root = Path(directory)
            site.DATA_FILE = root / "photos.json"
            site.BOOK_LAYOUT_FILE = root / "book-layout.json"
            site.UPLOAD_DIR = root / "uploads"
            site.UPLOAD_DIR.mkdir()
            os.environ["PHOTO_RING_PASSWORD_HASH"] = generate_password_hash("test-password")
            try:
                client = site.app.test_client()
                self.assertEqual(client.get("/admin/book").status_code, 200)
                self.assertEqual(client.get("/book-layouts.js").status_code, 200)
                self.assertEqual(client.get("/api/book-layout").json["configured"], False)
                self.assertEqual(client.put("/api/manage/book-layout", json={"years": {}}).status_code, 401)
                self.assertEqual(client.post("/api/login", json={"password": "test-password"}).status_code, 200)

                image = Image.new("RGB", (20, 20), "red")
                binary = io.BytesIO()
                image.save(binary, format="JPEG")
                binary.seek(0)
                upload = client.post("/api/manage/upload", data={"year": "2026", "photos": (binary, "sample.jpg")}, content_type="multipart/form-data")
                self.assertEqual(upload.status_code, 200)
                photo_id = upload.json["photos"][0]["id"]
                original_photo_bytes = site.DATA_FILE.read_bytes()

                layout = {"years": {"2026": [
                    {"id": "page-1", "layout": "single", "photos": [{"photoId": photo_id, "slot": 1}]},
                    {"id": "page-2", "layout": "two-horizontal", "photos": [{"photoId": photo_id, "slot": 1}, {"photoId": photo_id, "slot": 2}]},
                    {"id": "page-3", "layout": "four-grid", "photos": [{"photoId": "seed-01", "slot": 4}]},
                ], "2027": []}}
                response = client.put("/api/manage/book-layout", json=layout)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(site.DATA_FILE.read_bytes(), original_photo_bytes)
                saved = json.loads(site.BOOK_LAYOUT_FILE.read_text(encoding="utf-8"))
                self.assertEqual(len(saved["years"]["2026"]), 3)
                self.assertEqual(saved["years"]["2026"][1]["photos"][1], {"photoId": photo_id, "slot": 2})
                self.assertNotIn("src", site.BOOK_LAYOUT_FILE.read_text(encoding="utf-8"))
                self.assertEqual(client.get("/api/book-layout").json["configured"], True)

                bad = {"years": {"2026": [{"id": "bad", "layout": "single", "photos": [{"photoId": photo_id, "slot": 2}]}], "2027": []}}
                self.assertEqual(client.put("/api/manage/book-layout", json=bad).status_code, 400)
                bad["years"]["2026"][0]["layout"] = []
                self.assertEqual(client.put("/api/manage/book-layout", json=bad).status_code, 400)
                self.assertEqual(client.delete("/api/manage/photos/" + photo_id).status_code, 200)
                self.assertTrue((site.UPLOAD_DIR / photo_id).exists())
                self.assertTrue(site.read_data()["photos"][0]["deletedAt"])
                self.assertEqual(client.get("/api/book-layout").json["years"]["2026"][0]["photos"][0]["photoId"], photo_id)
            finally:
                site.DATA_FILE, site.BOOK_LAYOUT_FILE, site.UPLOAD_DIR = original_data, original_book, original_upload
                if previous_hash is None:
                    os.environ.pop("PHOTO_RING_PASSWORD_HASH", None)
                else:
                    os.environ["PHOTO_RING_PASSWORD_HASH"] = previous_hash


if __name__ == "__main__":
    unittest.main()
