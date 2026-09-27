"""Music routes use a fake OSS store; tests never touch the production bucket."""

import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash


class MusicTest(unittest.TestCase):
    def setUp(self):
        self.site = importlib.import_module("app")
        self.store = importlib.import_module("music_store")
        self.temp = tempfile.TemporaryDirectory(dir=self.site.BASE_DIR)
        self.previous = self.store.DATA_FILE, self.store.LOCK_FILE
        self.store.DATA_FILE = Path(self.temp.name) / "music.json"
        self.store.LOCK_FILE = Path(self.temp.name) / "music.lock"
        self.environment = patch.dict(os.environ, {"PHOTO_RING_PASSWORD_HASH": generate_password_hash("test-password")})
        self.environment.start()
        self.objects = {}
        self.patches = [
            patch.object(self.site.oss_storage, "configured", return_value=True),
            patch.object(self.site.oss_storage, "put", side_effect=self.put),
            patch.object(self.site.oss_storage, "delete", side_effect=self.delete),
            patch.object(self.site.oss_storage, "size", side_effect=lambda key: len(self.objects[key])),
            patch.object(self.site.oss_storage, "open_range", side_effect=self.open_range),
        ]
        for item in self.patches:
            item.start()
        self.client = self.site.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.environment.stop()
        self.store.DATA_FILE, self.store.LOCK_FILE = self.previous
        self.temp.cleanup()

    def login(self):
        self.assertEqual(self.client.post("/api/login", json={"password": "test-password"}).status_code, 200)

    def put(self, key, content, content_type):
        self.assertTrue(key.startswith("music/"))
        self.assertEqual(content_type, "audio/mpeg")
        self.objects[key] = content

    def delete(self, key):
        self.objects.pop(key)

    def open_range(self, key, start=None, end=None):
        binary = self.objects[key]
        return io.BytesIO(binary if start is None else binary[start:end + 1])

    def upload(self):
        fake_package = types.ModuleType("mutagen")
        fake_mp3 = types.ModuleType("mutagen.mp3")
        fake_mp3.MP3 = lambda source: types.SimpleNamespace(info=types.SimpleNamespace(length=123.45))
        with patch.dict(sys.modules, {"mutagen": fake_package, "mutagen.mp3": fake_mp3}):
            return self.client.post("/api/manage/music", data={
                "title": "我们的歌", "artist": "歌手", "music": (io.BytesIO(b"MP3 sample"), "song.mp3", "audio/mpeg")
            }, content_type="multipart/form-data")

    def test_management_requires_login(self):
        self.assertEqual(self.client.get("/api/manage/music").status_code, 401)
        self.assertEqual(self.upload().status_code, 401)
        self.assertEqual(self.client.put("/api/manage/music/settings", json={"musicEnabled": True}).status_code, 401)
        self.assertEqual(self.client.delete("/api/manage/music/unknown").status_code, 401)

    def test_upload_activate_public_info_and_range(self):
        self.login()
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        track = response.json["music"]
        self.assertRegex(track["objectKey"], r"^music/\d{4}/[0-9a-f]{32}\.mp3$")
        self.assertEqual(self.store.read()["music"][0]["duration"], 123.45)
        self.assertEqual(self.client.get("/api/music/stream/" + track["id"]).status_code, 200)
        changed = self.client.put("/api/manage/music/settings", json={
            "activeMusicId": track["id"], "musicEnabled": True, "musicVolume": .25, "musicLoop": False,
        })
        self.assertEqual(changed.status_code, 200)
        self.client.post("/api/logout")
        public = self.client.get("/api/music/current").json
        self.assertEqual(public["volume"], .25)
        self.assertEqual(public["music"]["id"], track["id"])
        self.assertNotIn("objectKey", public["music"])
        streamed = self.client.get("/api/music/stream/" + track["id"], headers={"Range": "bytes=2-5"})
        self.assertEqual(streamed.status_code, 206)
        self.assertEqual(streamed.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(streamed.data, b"3 sa")
        self.assertEqual(self.client.get("/api/music/stream/" + track["id"],
                                         headers={"Range": "bytes=99-"}).status_code, 416)

    def test_delete_active_clears_settings_and_object(self):
        self.login()
        track = self.upload().json["music"]
        self.client.put("/api/manage/music/settings", json={"activeMusicId": track["id"], "musicEnabled": True})
        response = self.client.delete("/api/manage/music/" + track["id"])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["cleanupPending"])
        data = self.store.read()
        self.assertEqual(data["music"], [])
        self.assertIsNone(data["settings"]["activeMusicId"])
        self.assertFalse(data["settings"]["musicEnabled"])
        self.assertEqual(data["pendingOssDeletes"], [])
        self.assertNotIn(track["objectKey"], self.objects)

    def test_delete_failure_is_recorded(self):
        self.login()
        track = self.upload().json["music"]
        with patch.object(self.site.oss_storage, "delete", side_effect=RuntimeError("OSS unavailable")):
            response = self.client.delete("/api/manage/music/" + track["id"])
        self.assertTrue(response.json["cleanupPending"])
        self.assertEqual(self.store.read()["pendingOssDeletes"], [track["objectKey"]])
        retried = self.client.post("/api/manage/music/retry-deletes")
        self.assertEqual(retried.json["pending"], 0)
        self.assertEqual(self.store.read()["pendingOssDeletes"], [])

    def test_real_mp3_is_validated_and_gets_duration(self):
        self.login()
        # Repeated silent MPEG frames are sufficient to exercise the real parser.
        sample = (bytes.fromhex("fffb9064") + bytes(413)) * 100
        response = self.client.post("/api/manage/music", data={
            "title": "本地测试音频", "music": (io.BytesIO(sample), "test.mp3", "audio/mpeg")
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201)
        self.assertGreater(response.json["music"]["duration"], 2)
        self.assertLess(response.json["music"]["duration"], 3)

    def test_settings_reject_unknown_song_and_invalid_volume(self):
        self.login()
        self.assertEqual(self.client.put("/api/manage/music/settings", json={"activeMusicId": "missing"}).status_code, 400)
        self.assertEqual(self.client.put("/api/manage/music/settings", json={"musicVolume": 2}).status_code, 400)
        self.assertEqual(self.client.put("/api/manage/music/settings", json={"musicVolume": True}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
