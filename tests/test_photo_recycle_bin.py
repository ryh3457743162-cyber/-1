"""Recycle lifecycle tests: isolated JSON, generated photos and in-memory OSS only."""
import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


class PhotoRecycleTest(unittest.TestCase):
    def setUp(self):
        self.site = importlib.import_module('app')
        self.store = self.site.comment_store
        self.temp = tempfile.TemporaryDirectory(dir=self.site.BASE_DIR)
        root = Path(self.temp.name)
        self.original = self.site.DATA_FILE, self.site.BOOK_LAYOUT_FILE, self.site.PUBLIC_DIR, self.site.UPLOAD_DIR, self.store.DATA_FILE, self.store.LOCK_FILE
        self.site.DATA_FILE = root/'photos.json'
        self.site.BOOK_LAYOUT_FILE = root/'book-layout.json'
        self.site.PUBLIC_DIR = root/'public'
        self.site.UPLOAD_DIR = root/'uploads'
        (self.site.PUBLIC_DIR/'photos').mkdir(parents=True)
        self.site.UPLOAD_DIR.mkdir()
        self.store.DATA_FILE, self.store.LOCK_FILE = root/'comments.json', root/'comments.lock'
        for name in ('photo-01.jpg', 'photo-02.jpg'):
            Image.new('RGB', (80,60), '#99515d').save(self.site.PUBLIC_DIR/'photos'/name)
        self.photo = {'id':'test-x.webp','title':'测试回忆','year':'2026','src':'/api/images/test-x.webp',
                      'objectKey':'photos/originals/2026/test-x.webp','thumbnailKey':'photos/thumbnails/2026/test-x.webp',
                      'thumbnailSrc':'/api/thumbnails/test-x.webp','deletedAt':None}
        self.other = {'id':'test-y.webp','title':'另一张','year':'2027','src':'/api/images/test-y.webp'}
        self.site.write_data({'photos':[self.photo,self.other], 'hiddenSeedIds':['seed-23'], 'seedOss':{},'pendingOssDeletes':[]})
        self.layout = {'years':{'2026':[{'id':f'p{index}', 'order':index,'layout':'two-horizontal',
                     'photos':[{'photoId':'test-y.webp','slot':1},{'photoId':'test-x.webp','slot':2}],
                     'note':{'type':'comment','commentId':'c-approved'}} for index in range(1,6)], '2027':[]}}
        self.site.BOOK_LAYOUT_FILE.write_text(json.dumps(self.layout),encoding='utf-8')
        self.comments = [{'id':'c-'+status,'photoId':'test-x.webp','nickname':'访客','content':status,
                         'createdAt':'2026-09-28T00:00:00Z','updatedAt':'2026-09-28T00:00:00Z',
                         'status':status} for status in ('approved','pending','rejected','hidden')]
        self.store.update(lambda items:items.extend(self.comments))
        self.objects = {self.photo['objectKey']:b'original',self.photo['thumbnailKey']:b'thumb'}
        self.real_oss_delete = self.site.oss_storage.delete
        self.delete = patch.object(self.site.oss_storage,'delete',side_effect=lambda key:self.objects.pop(key,None)).start()
        self.get = patch.object(self.site.oss_storage,'get',side_effect=lambda key:self.objects[key]).start()
        self.client = self.site.app.test_client()
        with self.client.session_transaction() as session:
            session['admin'] = True
        self.public = self.site.app.test_client()

    def tearDown(self):
        patch.stopall()
        self.site.DATA_FILE,self.site.BOOK_LAYOUT_FILE,self.site.PUBLIC_DIR,self.site.UPLOAD_DIR,self.store.DATA_FILE,self.store.LOCK_FILE = self.original
        self.temp.cleanup()

    def trash(self, photo_id='test-x.webp'):
        return self.client.delete('/api/manage/photos/'+photo_id)

    def purge(self, photo_id='test-x.webp'):
        return self.client.delete('/api/manage/trash/photos/'+photo_id)

    def restore(self, photo_id='test-x.webp'):
        return self.client.post('/api/manage/photos/'+photo_id+'/restore')

    def test_soft_delete_preserves_all_resources_and_states(self):
        before = self.site.BOOK_LAYOUT_FILE.read_bytes(),self.store.DATA_FILE.read_bytes()
        self.assertEqual(self.trash().status_code,200)
        self.assertTrue(self.site.read_data()['photos'][0]['deletedAt'])
        self.assertEqual(before,(self.site.BOOK_LAYOUT_FILE.read_bytes(),self.store.DATA_FILE.read_bytes()))
        self.assertEqual(len(self.objects),2)
        self.delete.assert_not_called()
        self.assertEqual(self.trash().status_code,200)

    def test_public_filtering_and_direct_urls_and_comments(self):
        self.trash()
        data = self.public.get('/api/photos').json
        self.assertNotIn('test-x.webp',[p['id'] for p in data['photos']])
        self.assertNotIn('test-x.webp',data['commentCounts'])
        self.assertNotIn('test-x.webp',[p['id'] for p in self.client.get('/api/manage/photos').json['photos']])
        for url in (self.photo['src'],self.photo['thumbnailSrc'],'/api/photos/test-x.webp/comments'):
            response = self.public.get(url)
            self.assertEqual(response.status_code,404,url)
            self.assertNotIn('deletedAt',response.json)
        self.assertEqual(self.public.post('/api/photos/test-x.webp/comments',json={'content':'hello'}).status_code,404)
        self.assertEqual(self.client.get(self.photo['thumbnailSrc']).status_code,200)
        self.assertEqual(self.public.get('/api/images/../anything').status_code,404)

    def test_book_and_featured_comments_hide_and_restore_same_slot(self):
        initial = self.site.BOOK_LAYOUT_FILE.read_bytes()
        self.assertIn('c-approved',self.public.get('/api/book-layout').json['featuredComments'])
        self.trash()
        hidden = self.public.get('/api/book-layout').json
        self.assertNotIn('c-approved',hidden['featuredComments'])
        self.assertEqual(hidden['years']['2026'][4]['photos'][1],{'photoId':'test-x.webp','slot':2})
        self.assertEqual(self.restore().status_code,200)
        self.assertEqual(self.site.BOOK_LAYOUT_FILE.read_bytes(),initial)
        restored = self.public.get('/api/book-layout').json
        self.assertIn('c-approved',restored['featuredComments'])
        self.assertEqual(restored['years']['2026'][4]['photos'][1],{'photoId':'test-x.webp','slot':2})
        self.assertEqual(self.site.read_data()['photos'][0]['objectKey'],self.photo['objectKey'])
        self.assertEqual(self.public.get('/api/photos/test-x.webp/comments').json['total'],1)
        self.assertEqual(self.public.get('/api/photos').json['commentCounts']['test-x.webp'],1)

    def test_trash_listing_and_latest_first(self):
        self.trash('test-y.webp'); self.trash()
        data = self.client.get('/api/manage/trash/photos').json
        self.assertEqual(data['total'],2)
        self.assertEqual(data['photos'][0]['id'],'test-x.webp')
        self.assertEqual(data['photos'][0]['commentTotal'],4)
        self.assertTrue(data['photos'][0]['bookReferenced'])
        self.assertNotIn('objectKey',data['photos'][0])
        self.assertEqual(self.restore().status_code,200)
        self.assertEqual(self.client.get('/api/manage/trash/photos').json['total'],1)

    def test_requires_admin_and_recycle_first(self):
        for method,url in [('get','/api/manage/trash/photos'),('delete','/api/manage/photos/test-x.webp'),
                           ('delete','/api/manage/trash/photos/test-x.webp'),('post','/api/manage/photos/test-x.webp/restore')]:
            self.assertEqual(getattr(self.public,method)(url).status_code,401)
        self.assertEqual(self.purge().status_code,409)
        self.delete.assert_not_called()
        self.assertEqual(self.trash('missing').status_code,404)

    def test_permanent_delete_cleans_comments_refs_and_preserves_pages(self):
        self.trash()
        self.assertEqual(self.purge().status_code,200)
        self.assertEqual(self.objects,{})
        self.assertEqual(self.store.read(),[])
        self.assertEqual([p['id'] for p in self.site.read_data()['photos']],['test-y.webp'])
        pages=self.site.read_book_layout()['years']['2026']
        self.assertEqual(len(pages),5)
        for page in pages:
            self.assertEqual(page['photos'],[{'photoId':'test-y.webp','slot':1}])
            self.assertNotIn('note',page)
        self.assertEqual(self.purge().status_code,404)
        self.assertFalse(self.site.DATA_FILE.with_suffix('.purge-journal.json').exists())

    def test_oss_failure_retains_photo_and_all_associations_for_retry(self):
        self.trash()
        original_book=self.site.BOOK_LAYOUT_FILE.read_bytes()
        self.delete.side_effect=OSError('mock network unavailable')
        self.assertEqual(self.purge().status_code,502)
        self.assertEqual(len(self.site.read_data()['photos']),2)
        self.assertEqual(len(self.store.read()),4)
        self.assertEqual(self.site.BOOK_LAYOUT_FILE.read_bytes(),original_book)
        self.assertEqual(self.restore().status_code,409)
        self.assertEqual(len(self.objects),2)

    def test_partial_failure_records_progress_and_idempotent_retry(self):
        self.trash()
        def fail_thumb(key):
            if '/thumbnails/' in key: raise PermissionError('mock access denied')
            self.objects.pop(key,None)
        self.delete.side_effect=fail_thumb
        self.assertEqual(self.purge().status_code,502)
        self.assertNotIn(self.photo['objectKey'],self.objects)
        target=self.site.read_data()['photos'][0]
        self.assertEqual(target['purgeCompletedKeys'],[self.photo['objectKey']])
        self.assertEqual(self.restore().status_code,409)
        self.delete.reset_mock(); self.delete.side_effect=lambda key:self.objects.pop(key,None)
        self.assertEqual(self.purge().status_code,200)
        self.delete.assert_called_once_with(self.photo['thumbnailKey'])

    def test_missing_object_is_idempotent_but_permission_error_is_not(self):
        self.trash()
        class Missing(Exception): code='NoSuchKey'
        self.delete.side_effect=Missing()
        self.assertEqual(self.purge().status_code,200)
        self.assertEqual(len(self.site.read_data()['photos']),1)

    def test_shared_or_wrong_namespace_key_blocked_before_deletion(self):
        data=self.site.read_data(); data['photos'][1]['objectKey']=self.photo['objectKey']; self.site.write_data(data)
        self.trash()
        self.assertEqual(self.purge().status_code,409)
        self.delete.assert_not_called()
        data=self.site.read_data(); data['photos'][1].pop('objectKey'); data['photos'][0]['objectKey']='music/2026/test.mp3'; self.site.write_data(data)
        self.assertEqual(self.purge().status_code,409)
        self.delete.assert_not_called()

    def test_json_transaction_recovers_after_interrupted_commit(self):
        self.trash()
        original_write=self.site.photo_lifecycle.atomic_write
        def fail_book(path,value):
            if path==self.site.BOOK_LAYOUT_FILE: raise OSError('mock write interrupted')
            return original_write(path,value)
        with patch.object(self.site.photo_lifecycle,'atomic_write',side_effect=fail_book):
            self.assertEqual(self.purge().status_code,502)
            self.assertTrue(self.site.DATA_FILE.with_suffix('.purge-journal.json').exists())
            self.assertEqual(self.public.get('/api/photos').status_code,503)
        self.assertEqual(self.public.get('/api/photos').status_code,200)
        self.assertFalse(self.site.DATA_FILE.with_suffix('.purge-journal.json').exists())
        self.assertEqual(self.store.read(),[])
        self.assertEqual(self.site.read_book_layout()['years']['2026'][4]['photos'],[{'photoId':'test-y.webp','slot':1}])
        self.assertEqual(len(self.site.read_data()['photos']),1)

    def test_builtin_soft_restore_and_permanent_local_cleanup(self):
        self.assertEqual(self.trash('seed-01').status_code,200)
        self.assertTrue((self.site.PUBLIC_DIR/'photos/photo-01.jpg').exists())
        self.assertIn('seed-01',self.public.get('/api/photos').json['hiddenSeedIds'])
        self.assertEqual(self.public.get('/photos/photo-01.jpg').status_code,404)
        self.assertEqual(self.restore('seed-01').status_code,200)
        response=self.public.get('/photos/photo-01.jpg'); self.assertEqual(response.status_code,200); response.close()
        self.trash('seed-01'); self.assertEqual(self.purge('seed-01').status_code,200)
        self.assertFalse((self.site.PUBLIC_DIR/'photos/photo-01.jpg').exists())
        self.assertIn('seed-01',self.public.get('/api/photos').json['hiddenSeedIds'])
        self.assertEqual(self.restore('seed-01').status_code,404)

    def test_incremental_migration_backs_up_and_preserves_legacy_hidden(self):
        from scripts.migrate_photo_recycle_bin import migrate
        before=self.site.DATA_FILE.read_bytes()
        dry=migrate(self.site.DATA_FILE)
        self.assertFalse(dry['applied']); self.assertEqual(self.site.DATA_FILE.read_bytes(),before)
        result=migrate(self.site.DATA_FILE,True)
        self.assertTrue(result['applied']); self.assertEqual(Path(result['backup']).read_bytes(),before)
        data=self.site.read_data(); self.assertIsNone(data['photos'][1]['deletedAt'])
        self.assertEqual(data['hiddenSeedIds'],['seed-23'])
        self.assertFalse(migrate(self.site.DATA_FILE,True)['applied'])

    def test_stale_book_save_cannot_reintroduce_purged_reference(self):
        stale=copy.deepcopy(self.layout)
        self.trash(); self.purge()
        self.assertEqual(self.client.put('/api/manage/book-layout',json=stale).status_code,409)

    def test_existing_recycled_reference_can_be_saved_but_not_new_feature(self):
        self.trash()
        self.assertEqual(self.client.put('/api/manage/book-layout',json=self.layout).status_code,200)
        changed=copy.deepcopy(self.layout); changed['years']['2026'][0]['id']='new-page'
        self.assertEqual(self.client.put('/api/manage/book-layout',json=changed).status_code,409)

    def test_cannot_delete_resources_without_durable_retry_record(self):
        self.trash()
        with patch.object(self.site, 'write_data', side_effect=OSError('mock disk full')):
            self.assertEqual(self.purge().status_code, 502)
        self.delete.assert_not_called()
        self.assertEqual(len(self.objects), 2)
        self.assertEqual(len(self.store.read()), 4)

    def test_invalid_book_data_blocks_permanent_delete_before_oss(self):
        self.trash()
        self.site.BOOK_LAYOUT_FILE.write_text('{invalid', encoding='utf-8')
        self.assertEqual(self.purge().status_code, 409)
        self.delete.assert_not_called()
        self.assertEqual(len(self.site.read_data()['photos']), 2)

    def test_legacy_delete_queue_cannot_delete_referenced_trash_keys(self):
        self.trash()
        data = self.site.read_data()
        data['pendingOssDeletes'] = list(self.objects)
        self.site.write_data(data)
        result = self.client.post('/api/manage/oss/retry-deletes')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['deleted'], 0)
        self.assertEqual(result.json['pending'], 2)
        self.delete.assert_not_called()

    def test_oss_sdk_unsuccessful_status_is_not_silently_accepted(self):
        from types import SimpleNamespace
        # Call the real helper with a fake SDK bucket; no network or credentials.
        with patch.object(self.site.oss_storage, '_bucket') as bucket:
            bucket.return_value.delete_object.return_value = SimpleNamespace(status=500)
            with self.assertRaises(RuntimeError):
                self.real_oss_delete('photos/originals/2026/local-test.webp')
            bucket.return_value.delete_object.return_value = SimpleNamespace(status=204)
            self.real_oss_delete('photos/originals/2026/local-test.webp')

    def test_last_photo_purge_keeps_empty_pages_and_manual_notes(self):
        book = copy.deepcopy(self.layout)
        for page in book['years']['2026']:
            page['layout'] = 'single'
            page['photos'] = [{'photoId': self.photo['id'], 'slot': 1}]
        manual = {'type': 'manual', 'text': '独立的纪念文字', 'author': '凡凡'}
        book['years']['2026'][4]['note'] = manual
        self.site.BOOK_LAYOUT_FILE.write_text(json.dumps(book), encoding='utf-8')
        self.trash()
        self.assertEqual(self.purge().status_code, 200)
        pages = self.site.read_book_layout()['years']['2026']
        self.assertEqual(len(pages), 5)
        self.assertTrue(all(page['photos'] == [] for page in pages))
        self.assertNotIn('note', pages[0])
        self.assertEqual(pages[4]['note'], manual)


if __name__ == '__main__': unittest.main()
