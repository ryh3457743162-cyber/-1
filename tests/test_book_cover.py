"""Cover configuration is independent of photos, comments, music and body pages."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_cover_store as store


class BookCoverTest(unittest.TestCase):
    def setUp(self):
        self.site = importlib.import_module("app")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        public = self.root / "public"
        (public / "photos").mkdir(parents=True)
        (public / "photos" / "photo-01.jpg").write_bytes(b"fixture")
        self.data = self.root / "photos.json"
        self.data.write_text(json.dumps({"photos": [
            {"id": "upload-1", "title": "照片", "year": "2026", "src": "/api/images/upload-1",
             "thumbnailSrc": "/api/thumbnails/upload-1", "objectKey": "private/original", "thumbnailKey": "private/thumb"}],
            "hiddenSeedIds": [], "seedOss": {"seed-01": {"objectKey": "private/seed", "thumbnailKey": "private/thumb"}},
            "pendingOssDeletes": []}), encoding="utf-8")
        self.book = self.root / "book-layout.json"
        self.book.write_text(json.dumps({"years": {"2026": [
            {"id": "page-stays", "order": 1, "layout": "single", "photos": [{"photoId": "upload-1", "slot": 1}]}], "2027": []}}))
        for target, field, value in ((store, "DATA_FILE", self.root / "book-cover.json"),
                                      (store, "LOCK_FILE", self.root / "book-cover.lock"),
                                      (self.site, "DATA_FILE", self.data), (self.site, "BOOK_LAYOUT_FILE", self.book),
                                      (self.site, "PUBLIC_DIR", public)):
            patcher = patch.object(target, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.site.app.test_client()

    def admin(self):
        with self.client.session_transaction() as session:
            session["admin"] = True

    def save(self, **values):
        return self.client.put("/api/manage/book-cover", json={**store.DEFAULT, **values})

    def test_default_without_configuration_does_not_create_file(self):
        result = self.client.get("/api/book-cover")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["cover"], store.DEFAULT)
        self.assertIsNone(result.json["photo"])
        self.assertFalse(store.DATA_FILE.exists())

    def test_admin_authentication(self):
        self.assertEqual(self.client.get("/api/manage/book-cover").status_code, 401)
        self.assertEqual(self.save().status_code, 401)
        self.assertFalse(store.DATA_FILE.exists())

    def test_all_templates_and_optional_text_persist_without_changing_body(self):
        self.admin()
        before = self.data.read_bytes(), self.book.read_bytes()
        for layout in ("classic", "photo", "minimal", "polaroid"):
            response = self.save(layout=layout, title="", subtitle="", dateText="", footerText="")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(store.read()["layout"], layout)
            self.assertIn("updatedAt", response.json["cover"])
            self.assertEqual(self.client.get("/api/manage/book-cover").json["cover"], store.read())
        self.assertEqual((self.data.read_bytes(), self.book.read_bytes()), before)

    def test_existing_photo_id_resolves_safe_urls_without_oss_keys(self):
        self.admin()
        self.assertEqual(self.save(photoId="upload-1", showPhoto=True).status_code, 200)
        result = self.client.get("/api/book-cover").json
        self.assertEqual(result["photo"]["thumbnailSrc"], "/api/thumbnails/upload-1")
        self.assertEqual(result["photo"]["src"], "/api/images/upload-1")
        self.assertNotIn("objectKey", result["photo"])
        self.assertNotIn("updatedAt", result["cover"])
        saved = json.loads(store.DATA_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("src", saved)

    def test_replace_and_remove_photo_preserves_library(self):
        self.admin()
        before = self.data.read_bytes()
        self.save(photoId="upload-1", showPhoto=True)
        self.save(photoId="seed-01", showPhoto=True)
        self.assertEqual(self.client.get("/api/book-cover").json["photo"]["src"], "/photos/photo-01.jpg")
        self.save(photoId=None, showPhoto=False)
        self.assertIsNone(self.client.get("/api/book-cover").json["photo"])
        self.assertEqual(self.data.read_bytes(), before)

    def test_missing_hidden_and_future_deleted_photo_degrade_without_clearing_id(self):
        self.admin()
        self.assertEqual(self.save(photoId="missing", showPhoto=True).status_code, 409)
        store.save({**store.DEFAULT, "photoId": "missing", "showPhoto": True})
        self.assertIsNone(self.client.get("/api/book-cover").json["photo"])
        data = json.loads(self.data.read_text(encoding="utf-8"))
        data["hiddenSeedIds"] = ["seed-01"]
        data["photos"][0]["deletedAt"] = "2026-09-29"
        self.data.write_text(json.dumps(data), encoding="utf-8")
        for photo_id in ("seed-01", "upload-1"):
            store.save({**store.DEFAULT, "photoId": photo_id, "showPhoto": True})
            self.assertIsNone(self.client.get("/api/book-cover").json["photo"])
            self.assertEqual(store.read()["photoId"], photo_id)
        data["photos"][0].pop("deletedAt")
        self.data.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.client.get("/api/book-cover").json["photo"]["id"], "upload-1")

    def test_invalid_values_do_not_overwrite_saved_cover(self):
        self.admin()
        self.save(title="保留")
        before = store.DATA_FILE.read_bytes()
        for bad in ({"layout": []}, {"title": "字" * 121}, {"subtitle": None},
                    {"titleAlign": "unknown"}, {"photoPosition": "any"}, {"showPhoto": "true"}, {"photoId": 1}):
            self.assertEqual(self.save(**bad).status_code, 400)
            self.assertEqual(store.DATA_FILE.read_bytes(), before)

    def test_corrupted_optional_cover_falls_back(self):
        store.DATA_FILE.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.client.get("/api/book-cover").json["cover"], store.DEFAULT)

    def test_text_is_stored_as_text_and_unknown_fields_are_not_persisted(self):
        self.admin()
        content = '<script>alert("cover")</script>'
        result = self.save(title=content, objectKey="must-not-be-saved", adminSecret="ignored")
        self.assertEqual(result.json["cover"]["title"], content)
        self.assertNotIn("adminSecret", store.read())
        self.assertNotIn("objectKey", store.read())

    def test_write_failure_preserves_saved_cover(self):
        self.admin()
        self.save(title="原封面")
        before = store.DATA_FILE.read_bytes()
        with patch.object(store.os, "replace", side_effect=OSError("simulated disk failure")):
            self.assertEqual(self.save(title="新封面").status_code, 500)
        self.assertEqual(store.DATA_FILE.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".book-cover.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
