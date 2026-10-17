import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from server import Watcher


class Route:
    def __init__(self, fields):
        self.request = SimpleNamespace(url='https://creator.douyin.com/comment/reply/', method='POST', post_data=json.dumps(fields))
        self.result = ''
    async def abort(self): self.result='abort'
    async def continue_(self): self.result='continue'


class Guard(unittest.IsolatedAsyncioTestCase):
    def watcher(self):
        w = Watcher.__new__(Watcher)
        w.running=True;w.epoch=1;w.selected='video1';w.next_check=0;w.events=[]
        w.pending=dict(comment={'id':'c1'},reply='谢谢',epoch=1,dispatched=False,blocked=False)
        return w

    async def test_correct_target_once(self):
        w=self.watcher();r=Route(dict(aweme_id='video1',reply_id='c1',text='谢谢'))
        await w.guard_post(r)
        self.assertEqual(r.result,'continue')
        r2=Route(dict(aweme_id='video1',reply_id='c1',text='谢谢'))
        await w.guard_post(r2)
        self.assertEqual(r2.result,'abort')

    async def test_wrong_target_and_missing_target(self):
        for fields in (dict(aweme_id='wrong',reply_id='c1',text='谢谢'),dict(aweme_id='video1',reply_id='wrong',text='谢谢'),dict(text='谢谢')):
            w=self.watcher();r=Route(fields);await w.guard_post(r)
            self.assertEqual(r.result,'abort');self.assertFalse(w.running)

    async def test_stop_blocks_late_send(self):
        w=self.watcher();w.stop();r=Route(dict(aweme_id='video1',reply_id='c1',text='谢谢'))
        await w.guard_post(r)
        self.assertEqual(r.result,'abort')

    async def test_changed_reply_blocked(self):
        w=self.watcher();r=Route(dict(aweme_id='video1',reply_id='c1',text='changed'))
        await w.guard_post(r)
        self.assertEqual(r.result,'abort')

    async def test_creator_single_id_and_batch_rejected(self):
        cid='7000000000000000002'
        for value,expected in ((cid,'continue'),(cid+',7000000000000000003','abort'),('['+cid+']','abort'),('7000000000000000003','abort')):
            w=self.watcher();w.pending['comment']['id']=cid
            r=Route(dict(aweme_id='video1',reply_to_comment_ids=value,text='谢谢'))
            await w.guard_post(r);self.assertEqual(r.result,expected)
        w=self.watcher();w.pending['comment']['id']=cid
        r=Route(dict(aweme_id='video1',reply_to_comment_ids=cid,reply_id='different',text='谢谢'))
        await w.guard_post(r);self.assertEqual(r.result,'abort')
