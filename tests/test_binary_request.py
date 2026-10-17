import unittest,json
from types import SimpleNamespace
from test_guard import Guard,Route
from server import request_fields,NonTextRequestBody

class BinaryRequest:
    method='POST'
    def __init__(self,url):self.url=url
    @property
    def post_data(self):return b'\xff\x00'.decode('utf-8')

class BinaryBody(Guard):
    async def test_binary_telemetry_does_not_poison_next_send(self):
        w=self.watcher();r=Route({});r.request=BinaryRequest('https://mcs.douyin.com/report')
        await w.guard_post(r)
        self.assertEqual(r.result,'continue');self.assertTrue(w.running)
        self.assertFalse(w.pending['dispatched']);self.assertFalse(w.pending['blocked'])
        good=Route(dict(aweme_id='video1',reply_id='c1',text='谢谢'))
        await w.guard_post(good)
        self.assertEqual(good.result,'continue');self.assertTrue(w.pending['dispatched'])
    async def test_binary_comment_publish_fails_closed(self):
        for path in ('/comment/reply/','/comment/publish/','/comment/create/','/comment/reply/list/'):
            w=self.watcher();r=Route({});r.request=BinaryRequest('https://creator.douyin.com'+path)
            await w.guard_post(r)
            self.assertEqual(r.result,'abort');self.assertFalse(w.running)
            self.assertFalse(w.pending['dispatched']);self.assertTrue(w.pending['blocked'])
    async def test_binary_without_pending_is_contained(self):
        w=self.watcher();w.pending=None;r=Route({});r.request=BinaryRequest('https://mcs.douyin.com/report')
        await w.guard_post(r);self.assertTrue(w.running);self.assertEqual(r.result,'continue')
    async def test_binary_readonly_publish_is_blocked_without_pending(self):
        w=self.watcher();w.pending=None;r=Route({});r.request=BinaryRequest('https://creator.douyin.com/comment/publish/')
        await w.guard_post(r);self.assertFalse(w.running);self.assertEqual(r.result,'abort')
    def test_parser_does_not_silently_replace_invalid_bytes(self):
        with self.assertRaises(NonTextRequestBody):request_fields(BinaryRequest('https://douyin.com/comment/reply/'))
        self.assertEqual(request_fields(SimpleNamespace(post_data='text=%E8%B0%A2%E8%B0%A2&cid=123')),{'text':'谢谢','cid':'123'})
