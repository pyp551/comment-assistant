import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from core import Store, eligible, extract_comments, parse_reply, video_id

VID = '7560000000000000000'


class NewComments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db.sqlite'
        self.store = Store(self.path)
        self.start = time.time()
        self.c = dict(id='100', video=VID, created=self.start+2, parent='0', name='测试', author='u1', text='好可爱')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_new_only_and_exact_start_excluded(self):
        self.assertTrue(eligible(self.c, VID, self.start, self.start+10, self.store))
        for stamp in (self.start-3600, self.start-1, self.start):
            self.assertFalse(eligible(dict(self.c, created=stamp), VID, self.start, self.start+10, self.store))

    def test_wrong_video_future_and_nested_excluded(self):
        for c in (dict(self.c, video='other'), dict(self.c, created=self.start+100), dict(self.c, parent='123')):
            self.assertFalse(eligible(c, VID, self.start, self.start+10, self.store))

    def test_uncertain_survives_restart(self):
        self.store.mark(self.c, 'uncertain')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertFalse(eligible(self.c, VID, self.start, self.start+10, self.store))
        self.assertGreater(self.store.last_attempt(), self.start)
        self.assertEqual(self.store.attempts_since(self.start), 1)

    def test_restart_cutoff_does_not_backfill(self):
        self.assertFalse(eligible(self.c, VID, self.start+30, self.start+40, self.store))

    def test_parser_requires_identifiers_and_time(self):
        row = dict(cid='1', aweme_id=VID, create_time=1720000000, text='hi', user=dict(uid='u',nickname='user'))
        self.assertEqual(len(extract_comments({'comments':[row]})), 1)
        for key in ('cid','aweme_id','create_time','user'):
            broken = row.copy()
            broken.pop(key)
            self.assertEqual(extract_comments(broken), [])
        self.assertEqual(extract_comments(dict(row, create_time='刚刚')), [])

    def test_milliseconds_and_parent(self):
        row = dict(cid='1', aweme_id=VID, create_time=1720000000000, text='hi', reply_id='9', user=dict(uid='u',nickname='user'))
        c = extract_comments(row)[0]
        self.assertEqual(c['created'], 1720000000)
        self.assertEqual(c['parent'], '9')

    def test_same_user_distinct_comments(self):
        self.store.mark(self.c,'sent')
        self.assertTrue(eligible(dict(self.c, id='101'), VID, self.start, self.start+10,self.store))

    def test_reply_contract(self):
        self.assertEqual(parse_reply('{"action":"skip"}'),'')
        self.assertEqual(parse_reply('{"action":"reply","reply":"谢谢喜欢～"}'),'谢谢喜欢～')
        for content in ('hello', '{"action":"reply","reply":""}', '{"action":"reply","reply":"https://example.com"}'):
            with self.assertRaises(ValueError):
                parse_reply(content)

    def test_video_scope(self):
        self.assertEqual(video_id('https://www.douyin.com/video/'+VID+'?x=1'),VID)
        self.assertEqual(video_id('https://creator.douyin.com/creator-micro/work-management/work-detail/'+VID+'?tabKey=commentManagement'),VID)
        for value in ('https://evil.com/video/'+VID,'http://www.douyin.com/video/'+VID,'https://v.douyin.com/123','https://www.douyin.com@evil.com/video/'+VID):
            with self.assertRaises(ValueError):
                video_id(value)


if __name__ == '__main__':
    unittest.main()
