"""Metadata edits use isolated JSON fixtures and mocked OSS, never production data."""
import copy
import json
import unittest
from unittest.mock import patch
import test_photo_recycle_bin as recycle_fixtures


class PhotoMetadataTest(unittest.TestCase):
    setUp = recycle_fixtures.PhotoRecycleTest.setUp
    tearDown = recycle_fixtures.PhotoRecycleTest.tearDown
    trash = recycle_fixtures.PhotoRecycleTest.trash
    restore = recycle_fixtures.PhotoRecycleTest.restore

    def edit(self, payload, photo_id='test-x.webp', client=None):
        return (client or self.client).patch('/api/manage/photos/'+photo_id, json=payload)

    def test_title_trim_persists_and_public_updates(self):
        response = self.edit({'title':'  一起去看海 · Sea 2025!  '})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.site.read_data()['photos'][0]['title'],'一起去看海 · Sea 2025!')
        self.assertEqual(self.public.get('/api/photos').json['photos'][0]['title'],'一起去看海 · Sea 2025!')
        self.assertEqual(self.client.get('/api/manage/photos').json['photos'][2]['title'],'一起去看海 · Sea 2025!')

    def test_date_only_and_combined_calendar_date_without_timezone(self):
        self.assertEqual(self.edit({'shotDate':'2025-08-18'}).status_code,200)
        self.assertEqual(self.site.read_data()['photos'][0]['shotDate'],'2025-08-18')
        self.assertEqual(self.edit({'title':'海边','shotDate':'2024-02-29'}).status_code,200)
        result=self.public.get('/api/photos').json['photos'][0]
        self.assertEqual((result['title'],result['shotDate'],result['year']),('海边','2024-02-29','2026'))

    def test_unknown_date_can_clear_without_changing_title(self):
        for value in (None,''):
            self.edit({'shotDate':'2025-08-18'})
            self.assertEqual(self.edit({'shotDate':value}).status_code,200)
            self.assertIsNone(self.site.read_data()['photos'][0]['shotDate'])

    def test_invalid_titles_leave_data_unchanged(self):
        before=self.site.DATA_FILE.read_bytes()
        for value in ('','   ','好'*61,None,42,[]):
            with self.subTest(value=value):
                self.assertEqual(self.edit({'title':value}).status_code,400)
                self.assertEqual(before,self.site.DATA_FILE.read_bytes())

    def test_invalid_dates_leave_data_unchanged(self):
        before=self.site.DATA_FILE.read_bytes()
        for value in ('2025-02-29','2025-13-01','2025-04-31','2025-8-18','2025-08-18T00:00:00Z','0000-01-01',123,[],{}):
            with self.subTest(value=value):
                self.assertEqual(self.edit({'shotDate':value}).status_code,400)
                self.assertEqual(before,self.site.DATA_FILE.read_bytes())

    def test_strict_allowlist_and_authentication(self):
        before=self.site.DATA_FILE.read_bytes()
        for key in ('id','photoId','objectKey','thumbnailKey','src','deletedAt','year','uploaded','commentId'):
            self.assertEqual(self.edit({'title':'不能保存',key:'evil'}).status_code,400,key)
        for body in ({},[],None):
            self.assertEqual(self.edit(body).status_code,400)
        self.assertEqual(self.edit({'title':'无权限'},client=self.public).status_code,401)
        self.assertEqual(before,self.site.DATA_FILE.read_bytes())

    def test_resources_upload_time_comments_layout_cover_unchanged(self):
        photo=self.site.read_data()['photos'][0]
        photo['uploaded']='2026-09-28T14:30:00Z'
        data=self.site.read_data();data['photos'][0]=photo;self.site.write_data(data)
        cover={'photoId':'test-x.webp','title':'保留封面','showPhoto':True}
        self.site.book_cover_store.DATA_FILE.write_text(json.dumps(cover),encoding='utf-8')
        files=[self.store.DATA_FILE,self.site.BOOK_LAYOUT_FILE,self.site.book_cover_store.DATA_FILE]
        before=[file.read_bytes() for file in files];objects=copy.deepcopy(self.objects)
        self.assertEqual(self.edit({'title':'新名称','shotDate':'2025-08-18'}).status_code,200)
        after=self.site.read_data()['photos'][0]
        for key,value in photo.items():
            if key!='title':self.assertEqual(value,after[key],key)
        self.assertEqual(before,[file.read_bytes() for file in files])
        self.assertEqual(objects,self.objects);self.delete.assert_not_called();self.get.assert_not_called()
        self.assertIn('c-approved',self.public.get('/api/book-layout').json['featuredComments'])
        self.assertEqual(self.public.get('/api/book-cover').json['photo']['id'],'test-x.webp')

    def test_trash_rejects_edit_restore_preserves_metadata(self):
        self.edit({'title':'恢复原信息','shotDate':'2025-08-18'})
        self.trash();before=self.site.DATA_FILE.read_bytes()
        self.assertEqual(self.edit({'title':'不允许'}).status_code,409)
        self.assertEqual(before,self.site.DATA_FILE.read_bytes())
        self.assertEqual(self.restore().status_code,200)
        photo=self.site.read_data()['photos'][0]
        self.assertEqual((photo['title'],photo['shotDate']),('恢复原信息','2025-08-18'))
        self.assertEqual(self.edit({'title':'恢复后可编辑'}).status_code,200)

    def test_missing_photo(self):
        self.assertEqual(self.edit({'title':'不存在'},'missing').status_code,404)

    def test_storage_failure_keeps_saved_data(self):
        before = self.site.DATA_FILE.read_bytes()
        with patch.object(self.site, 'write_data', side_effect=OSError('fixture disk failure')):
            response = self.edit({'title':'不应保存','shotDate':'2025-08-18'})
        self.assertEqual(response.status_code,503)
        self.assertNotIn('fixture disk failure',response.json['error'])
        self.assertEqual(before,self.site.DATA_FILE.read_bytes())

    def test_date_script_public_route(self):
        (self.site.PUBLIC_DIR/'photo-metadata.js').write_text('/* fixture */',encoding='utf-8')
        self.assertEqual(self.public.get('/photo-metadata.js?v=1').status_code,200)

    def test_seed_metadata_overrides_and_public_safe_fields(self):
        self.assertEqual(self.edit({'title':'历史照片新名称','shotDate':'2025-08-18'},'seed-01').status_code,200)
        response=self.public.get('/api/photos').json
        seed=next(p for p in response['seedPhotos'] if p['id']=='seed-01')
        self.assertEqual((seed['title'],seed['shotDate']),('历史照片新名称','2025-08-18'))
        for item in response['seedPhotos']+response['photos']:
            self.assertFalse({'objectKey','thumbnailKey','deletedAt','visitorTokenHash'} & item.keys())
        self.trash('seed-01')
        self.assertNotIn('seed-01',[p['id'] for p in self.public.get('/api/photos').json['seedPhotos']])
        self.restore('seed-01')
        self.assertEqual(self.site.photo_record(self.site.read_data(),'seed-01')['title'],'历史照片新名称')

if __name__=='__main__':unittest.main()
