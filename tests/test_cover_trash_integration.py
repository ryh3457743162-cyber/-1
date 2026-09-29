"""The same photo identity must survive trash, then disappear from every purge reference."""
import json
import unittest
from unittest.mock import patch
import test_photo_recycle_bin as fixtures


class CoverTrashIntegrationTest(unittest.TestCase):
    setUp = fixtures.PhotoRecycleTest.setUp
    tearDown = fixtures.PhotoRecycleTest.tearDown
    trash = fixtures.PhotoRecycleTest.trash
    restore = fixtures.PhotoRecycleTest.restore
    purge = fixtures.PhotoRecycleTest.purge

    def save_cover(self, photo_id='test-x.webp'):
        return self.client.put('/api/manage/book-cover', json={
            **self.site.book_cover_store.DEFAULT, 'photoId': photo_id,
            'showPhoto': True, 'title': '保留的封面文字'})

    def test_cover_and_body_restore_same_identity_slot_comments_and_note(self):
        self.assertEqual(self.save_cover().status_code, 200)
        self.assertEqual(self.public.get('/api/book-cover').json['photo']['id'], self.photo['id'])
        before = self.site.BOOK_LAYOUT_FILE.read_bytes(), self.store.DATA_FILE.read_bytes()
        self.trash()
        result = self.public.get('/api/book-cover')
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json['photo'])
        self.assertEqual(result.json['cover']['photoId'], self.photo['id'])
        self.assertEqual(result.json['cover']['title'], '保留的封面文字')
        self.assertEqual(self.public.get('/api/book-layout').json['featuredComments'], {})
        self.assertEqual(before, (self.site.BOOK_LAYOUT_FILE.read_bytes(), self.store.DATA_FILE.read_bytes()))
        self.assertEqual(len(self.objects), 2)
        self.restore()
        self.assertEqual(self.public.get('/api/book-cover').json['photo']['id'], self.photo['id'])
        self.assertEqual(self.site.read_book_layout()['years']['2026'][4]['photos'][1],
                         {'photoId': self.photo['id'], 'slot': 2})
        self.assertIn('c-approved', self.public.get('/api/book-layout').json['featuredComments'])
        self.assertEqual({c['status'] for c in self.store.read()}, {'approved','pending','rejected','hidden'})

    def test_permanent_delete_cleans_cover_without_changing_text_or_page_ids(self):
        self.save_cover(); self.trash()
        self.assertEqual(self.purge().status_code, 200)
        result = self.public.get('/api/book-cover').json
        self.assertIsNone(result['cover']['photoId'])
        self.assertFalse(result['cover']['showPhoto'])
        self.assertEqual(result['cover']['title'], '保留的封面文字')
        self.assertEqual(self.objects, {})
        self.assertEqual(self.store.read(), [])
        pages = self.site.read_book_layout()['years']['2026']
        self.assertEqual([p['id'] for p in pages], [p['id'] for p in self.layout['years']['2026']])
        self.assertTrue(all('note' not in p for p in pages))

    def test_seed_cover_soft_delete_and_restore(self):
        self.save_cover('seed-01'); self.trash('seed-01')
        self.assertIsNone(self.public.get('/api/book-cover').json['photo'])
        self.restore('seed-01')
        self.assertEqual(self.public.get('/api/book-cover').json['photo']['id'], 'seed-01')

    def test_reject_new_trash_selection_but_keep_existing_reference_editable(self):
        self.trash()
        self.assertEqual(self.save_cover().status_code, 409)
        self.restore(); self.save_cover(); self.trash()
        self.assertEqual(self.save_cover().status_code, 200)
        normal = self.client.get('/api/manage/photos').json['photos']
        self.assertNotIn(self.photo['id'], [p['id'] for p in normal])

    def test_partial_oss_failure_keeps_cover_and_relationships_for_retry(self):
        self.save_cover(); self.trash()
        def remove(key):
            if '/thumbnails/' in key: raise OSError('mock only')
            self.objects.pop(key, None)
        with patch.object(self.site.oss_storage, 'delete', side_effect=remove):
            self.assertEqual(self.purge().status_code, 502)
        self.assertEqual(self.site.book_cover_store.read()['photoId'], self.photo['id'])
        self.assertEqual(len(self.store.read()), 4)
        self.assertEqual(self.restore().status_code, 409)
        self.assertEqual(self.purge().status_code, 200)
        self.assertIsNone(self.site.book_cover_store.read()['photoId'])

    def test_interrupted_cover_commit_recovers_all_four_files(self):
        self.save_cover(); self.trash()
        real = self.site.photo_lifecycle.atomic_write
        def fail_cover(path, value):
            if path == self.site.book_cover_store.DATA_FILE: raise OSError('mock cover write interruption')
            return real(path, value)
        with patch.object(self.site.photo_lifecycle, 'atomic_write', side_effect=fail_cover):
            self.assertEqual(self.purge().status_code, 502)
            self.assertEqual(self.public.get('/api/book-cover').status_code, 503)
        self.assertEqual(self.public.get('/api/book-cover').status_code, 200)
        self.assertIsNone(self.site.book_cover_store.read()['photoId'])
        self.assertFalse(self.site.DATA_FILE.with_suffix('.purge-journal.json').exists())

    def test_corrupt_cover_blocks_purge_before_oss(self):
        self.trash(); self.site.book_cover_store.DATA_FILE.write_text('{broken')
        self.assertEqual(self.purge().status_code, 409)
        self.delete.assert_not_called()
        self.assertEqual(len(self.store.read()), 4)
