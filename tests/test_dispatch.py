import sys,time,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import Store
from server import Watcher
from reconcile import ReplyLedger

VID='7000000000000000001'
NOW=1800000000
def comment(cid):
    return dict(video=VID,id=cid,author=cid,name=cid,text='你好'+cid,created=NOW-100,parent='0',root=cid)

class Dispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.w=Watcher();self.w.store.db.close()
        self.w.store=Store(Path(self.tmp.name)/'db')
        self.w.store.remember_own(dict(comment('proof'),author='owner'))
        self.w.selected=VID;self.w.started=NOW-1;self.w.running=True
        self.w.healthy=AsyncMock();self.w.refresh_comments=AsyncMock(return_value=True)
        self.now=NOW;self.sent=[]
        async def pause(epoch,seconds=1):
            self.now+=max(seconds,.25)
            if self.now>NOW+100:self.fail('scheduler stalled')
            return self.w.running
        self.w.pause_scan=pause
    def tearDown(self):
        self.w.store.db.close();self.tmp.cleanup()
    async def run_queue(self,rows,count=3):
        w=self.w;w.store.observe(rows)
        ReplyLedger(w.store).plan(VID,'run',rows,True)
        w.scan_history=AsyncMock(return_value=True)
        w.history_step=AsyncMock()
        w.history_next=float('inf')
        w.reconcile_status={'phase':'complete'}
        async def send(c,reply,epoch,part=1):
            self.sent.append((c['id'],part,self.now))
            w.store.reserve_send(c,part);w.store.confirm_send(c,part)
            w.store.remember_reply(c,part,reply);w.store.mark(c,'sent',reply)
            w.last_send=self.now;w.sent_this_run+=1
            if len(self.sent)>=count:w.stop()
            return True
        w.send=send
        def decide(c,*args):
            return dict(action='reply',tone='greeting',confidence=1,intent='chat',replies=['欢迎'+c['id'],'接着聊'+c['id']])
        with patch('server.time.time',side_effect=lambda:self.now),patch('server.decide',side_effect=decide),patch('server.trial_caps_enabled',return_value=False):
            await w.loop(w.epoch)
    async def test_first_replies_win_and_backlog_does_not_wait_for_poll(self):
        await self.run_queue([comment('a'),comment('b')])
        self.assertEqual([(cid,part) for cid,part,_ in self.sent],[('a',1),('b',1),('a',2)])
        self.assertEqual([at-NOW for _,_,at in self.sent],[0,5,10])
        self.assertEqual(self.w.refresh_comments.await_count,1)
    async def test_existing_manual_reply_is_not_sent_again(self):
        a,b=comment('a'),comment('b')
        self.w.store.observe([dict(comment('manual'),author='owner',parent='a',root='a')])
        await self.run_queue([a,b],count=1)
        self.assertEqual(self.sent[0][:2],('b',1))
    async def test_stop_clears_second_parts(self):
        w=self.w;c=comment('a')
        w.followups=[dict(comment=c,reply='再聊',created=NOW,epoch=w.epoch)]
        w.stop();w.send=AsyncMock()
        await w.send_followup(w.epoch)
        w.send.assert_not_awaited();self.assertEqual(w.followups,[])
    async def test_unknown_second_send_never_retries(self):
        w=self.w;c=comment('a');w.store.reserve_send(c,2)
        w.followups=[dict(comment=c,reply='再聊',created=time.time(),epoch=w.epoch)]
        w.send=AsyncMock();await w.send_followup(w.epoch);w.send.assert_not_awaited()
    async def test_folded_reply_pages_are_all_expanded(self):
        w=self.w;counts=iter([1,1,1,0])
        control=SimpleNamespace(count=AsyncMock(side_effect=lambda:next(counts)),click=AsyncMock())
        row=SimpleNamespace(get_by_role=lambda *args,**kwargs:control)
        await w.load_visible_thread(row,w.epoch)
        self.assertEqual(control.click.await_count,3)
    async def test_stop_in_folded_scan_prevents_further_clicks(self):
        w=self.w;control=SimpleNamespace(count=AsyncMock(return_value=1),click=AsyncMock())
        row=SimpleNamespace(get_by_role=lambda *args,**kwargs:control)
        async def stop(*args):w.stop();return False
        w.pause_scan=stop
        with self.assertRaises(ValueError):await w.load_visible_thread(row,w.epoch)
        self.assertEqual(control.click.await_count,1)

class Pending(unittest.TestCase):
    def test_persisted_new_comments_are_not_lost_and_limit_pages_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'db');ledger=ReplyLedger(s)
            try:
                rows=[comment(str(i).zfill(3)) for i in range(120)];s.observe(rows)
                first=ledger.pending_comments(VID,started=NOW-101)
                self.assertEqual(len(first),100)
                self.assertEqual(ledger.queue_state(VID,NOW-101)['waiting'],120)
                for c in first:s.mark(c,'sent')
                self.assertEqual(len(ledger.pending_comments(VID,started=NOW-101)),20)
                self.assertEqual(ledger.queue_state(VID,NOW-101)['waiting'],20)
                self.assertEqual(ledger.pending_comments(VID,started=NOW),[])
            finally:s.db.close()
