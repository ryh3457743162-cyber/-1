"""OSS behavior is tested with an in-memory store, never a real Bucket."""

import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from werkzeug.security import generate_password_hash


class OssIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.site = importlib.import_module("app")
        self.temp = tempfile.TemporaryDirectory(dir=self.site.BASE_DIR)
        root = Path(self.temp.name)
        self.previous = (self.site.DATA_FILE, self.site.BOOK_LAYOUT_FILE, self.site.UPLOAD_DIR)
        self.previous_comments = self.site.comment_store.DATA_FILE, self.site.comment_store.LOCK_FILE
        self.site.comment_store.DATA_FILE, self.site.comment_store.LOCK_FILE = root / 'comments.json', root / 'comments.lock'
        self.site.DATA_FILE = root / "photos.json"
        self.site.BOOK_LAYOUT_FILE = root / "book-layout.json"
        self.site.UPLOAD_DIR = root / "uploads"
        self.site.UPLOAD_DIR.mkdir()
        self.env = patch.dict(os.environ, {
            "PHOTO_RING_PASSWORD_HASH": generate_password_hash("test-password"),
            "OSS_REGION": "cn-shanghai",
            "OSS_BUCKET": "test-private-bucket",
            "OSS_ACCESS_KEY_ID": "test-id",
            "OSS_ACCESS_KEY_SECRET": "test-secret",
            "OSS_UPLOAD_MODE": "local",
            "OSS_TEST_UPLOAD_ENABLED": "1",
        })
        self.env.start()
        self.client = self.site.app.test_client()
        self.assertEqual(self.client.post("/api/login", json={"password": "test-password"}).status_code, 200)
        self.objects = {}
        self.patches = [
            patch.object(self.site.oss_storage, "put", side_effect=self.put),
            patch.object(self.site.oss_storage, "get", side_effect=lambda key: self.objects[key]),
            patch.object(self.site.oss_storage, "delete", side_effect=lambda key: self.objects.pop(key, None)),
        ]
        for active in self.patches:
            active.start()

    def tearDown(self):
        for active in reversed(self.patches):
            active.stop()
        self.env.stop()
        self.site.DATA_FILE, self.site.BOOK_LAYOUT_FILE, self.site.UPLOAD_DIR = self.previous
        self.site.comment_store.DATA_FILE, self.site.comment_store.LOCK_FILE = self.previous_comments
        self.temp.cleanup()

    def put(self, key, content, content_type):
        self.assertEqual(content_type, "image/webp")
        self.assertNotIn(key, self.objects)
        self.objects[key] = content

    @staticmethod
    def image():
        binary = io.BytesIO()
        Image.new("RGB", (48, 32), "#a04b53").save(binary, format="JPEG")
        binary.seek(0)
        return binary

    def upload(self, storage="oss-test"):
        return self.client.post("/api/manage/upload", data={
            "year": "2026", "storage": storage,
            "photos": (self.image(), "sample.jpg", "image/jpeg"),
        }, content_type="multipart/form-data")

    def test_private_oss_upload_read_book_reference_and_delete(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        photo = response.json["photos"][0]
        self.assertEqual(len(self.objects), 2)
        self.assertEqual(list(self.site.UPLOAD_DIR.iterdir()), [])
        self.assertTrue(photo["objectKey"].startswith("photos/originals/2026/"))
        self.assertTrue(photo["thumbnailKey"].startswith("photos/thumbnails/2026/"))
        self.assertEqual(self.client.get(photo["src"]).status_code, 200)
        self.assertEqual(self.client.get(photo["thumbnailSrc"]).status_code, 200)
        self.assertEqual(self.client.get(photo["src"]).mimetype, "image/webp")

        layout = {"years": {"2026": [{"id": "test-page", "layout": "single", "photos": [
            {"photoId": photo["id"], "slot": 1}
        ]}], "2027": []}}
        self.assertEqual(self.client.put("/api/manage/book-layout", json=layout).status_code, 200)
        self.assertEqual(self.client.delete("/api/manage/photos/" + photo["id"]).status_code, 200)
        self.assertEqual(len(self.objects), 2)
        self.assertTrue(self.site.read_data()["photos"][0]["deletedAt"])
        self.assertEqual(self.client.get("/api/book-layout").json["years"]["2026"][0]["photos"][0]["photoId"], photo["id"])
        self.assertEqual(self.client.delete("/api/manage/trash/photos/" + photo["id"]).status_code, 200)
        self.assertEqual(len(self.objects), 0)
        self.assertEqual(self.site.read_data()["photos"], [])
        self.assertEqual(self.site.read_data()["pendingOssDeletes"], [])
        self.assertEqual(self.client.get("/api/book-layout").json["years"]["2026"][0]["photos"], [])

    def test_thumbnail_failure_rolls_back_uploaded_original(self):
        original_put = self.site.oss_storage.put.side_effect

        def fail_thumbnail(key, content, content_type):
            if "/thumbnails/" in key:
                raise RuntimeError("simulated OSS failure")
            original_put(key, content, content_type)

        self.site.oss_storage.put.side_effect = fail_thumbnail
        response = self.upload()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.objects, {})
        self.assertEqual(self.site.read_data()["photos"], [])

    def test_metadata_failure_rolls_back_both_objects(self):
        self.site.read_data()
        with patch.object(self.site, "write_data", side_effect=OSError("simulated disk failure")):
            response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.objects, {})
        self.assertEqual(self.site.read_data()["photos"], [])

    def test_legacy_upload_still_uses_local_storage(self):
        response = self.upload(storage="")
        self.assertEqual(response.status_code, 200)
        photo = response.json["photos"][0]
        self.assertNotIn("objectKey", photo)
        self.assertEqual(len(self.objects), 0)
        self.assertTrue((self.site.UPLOAD_DIR / photo["id"]).exists())
        self.assertEqual(self.client.get(photo["src"]).status_code, 200)

    def test_three_photo_batch_has_unique_keys(self):
        response = self.client.post("/api/manage/upload", data={
            "year": "2027", "storage": "oss-test",
            "photos": [(self.image(), f"photo-{index}.jpg", "image/jpeg") for index in range(3)],
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["count"], 3)
        self.assertEqual(len(self.objects), 6)
        self.assertEqual(len({photo["id"] for photo in response.json["photos"]}), 3)
        self.assertTrue(all(photo["year"] == "2027" for photo in response.json["photos"]))

    def test_invalid_file_is_rejected_without_oss_write(self):
        response = self.client.post("/api/manage/upload", data={
            "year": "2026", "storage": "oss-test",
            "photos": (io.BytesIO(b"not an image"), "fake.jpg", "image/jpeg"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.objects, {})

    def test_oss_configuration_is_not_required_for_legacy_upload(self):
        with patch.dict(os.environ, {"OSS_ACCESS_KEY_SECRET": "", "OSS_TEST_UPLOAD_ENABLED": "0"}):
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.upload(storage="").status_code, 200)
            self.assertEqual(self.upload().status_code, 403)

    def test_migrated_seed_uses_oss_and_falls_back_to_original(self):
        def body(path):
            response = self.client.get(path)
            value = response.data
            response.close()
            return value

        original = body("/photos/photo-01.jpg")
        data = self.site.read_data()
        data["seedOss"] = {"seed-01": {
            "objectKey": "photos/originals/2026/seed-01-test.webp",
            "thumbnailKey": "photos/thumbnails/2026/seed-01-test.webp",
        }}
        self.site.write_data(data)
        self.objects[data["seedOss"]["seed-01"]["objectKey"]] = b"oss-original"
        self.objects[data["seedOss"]["seed-01"]["thumbnailKey"]] = b"oss-thumb"
        self.assertEqual(body("/photos/photo-01.jpg"), b"oss-original")
        self.assertEqual(body("/api/thumbnails/seed-01"), b"oss-thumb")
        self.assertIn("thumbnailSrc", self.client.get("/api/manage/photos").json["photos"][0])
        self.site.oss_storage.get.side_effect = OSError("simulated OSS outage")
        self.assertEqual(body("/photos/photo-01.jpg"), original)
        self.assertEqual(body("/api/thumbnails/seed-01"), original)

    def test_migrated_upload_keeps_photo_id_and_local_fallback(self):
        def body(path):
            response = self.client.get(path)
            value = response.data
            response.close()
            return value

        photo = self.upload(storage="").json["photos"][0]
        original = body(photo["src"])
        data = self.site.read_data()
        data["photos"][0].update({
            "objectKey": "photos/originals/2026/upload-test.webp",
            "thumbnailKey": "photos/thumbnails/2026/upload-test.webp",
            "thumbnailSrc": f'/api/thumbnails/{photo["id"]}',
        })
        self.site.write_data(data)
        self.objects["photos/originals/2026/upload-test.webp"] = b"oss-original"
        self.assertEqual(body(photo["src"]), b"oss-original")
        self.site.oss_storage.get.side_effect = OSError("simulated OSS outage")
        self.assertEqual(body(photo["src"]), original)
        self.assertEqual(body(data["photos"][0]["thumbnailSrc"]), original)
        self.assertEqual(self.site.read_data()["photos"][0]["id"], photo["id"])


if __name__ == "__main__":
    unittest.main()
