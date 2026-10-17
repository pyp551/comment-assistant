import asyncio,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import Store
from server import Watcher
from test_reconcile import comment,VID

class AutoReceipt(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.w=Watcher();self.w.store.db.close();self.w.store=Store(Path(self.tmp.name)/'db')
  w=self.w;w.running=True;w.selected=VID;w.healthy=AsyncMock();self.c=comment();w.store.observe([self.c]);w.store.remember_own(comment('owner-proof',author='owner'))
  w.store.reserve_send(self.c,1);w.store.mark(self.c,'uncertain','准备发送，若中断不自动重发：你好呀')
  w.pending=dict(comment=self.c,reply='你好呀',part=1,epoch=w.epoch,dispatched=True,blocked=False,confirmed=False)
  self.page=SimpleNamespace(on=lambda *a:None,goto=AsyncMock(),close=AsyncMock());w.context=SimpleNamespace(new_page=AsyncMock(return_value=self.page))
 def tearDown(self):self.w.store.db.close();self.tmp.cleanup()
 async def probe(self,rows=(),stop=False,click_timed_out=False):
  w=self.w
  async def load(*args):
   w.store.observe(rows)
   if stop:w.stop()
  with patch.object(Watcher,'wait_video_read',new=AsyncMock(return_value=True)),patch.object(Watcher,'search_root',new=AsyncMock()),patch.object(Watcher,'find_comment_row',new=AsyncMock()),patch.object(Watcher,'load_visible_thread',new=AsyncMock(side_effect=load)),patch.object(Watcher,'healthy',new=AsyncMock()):
   return await w.reconcile_pending_receipt(w.pending,w.epoch,click_timed_out=click_timed_out)
 def reply(self,cid='reply',author='owner',parent='c'):
  import time
  return dict(comment(cid,author=author,parent=parent,root='c',text='你好呀'),created=time.time())
 async def test_unique_positive_reply_confirms_without_restart_or_count_twice(self):
  w=self.w;p=w.pending;before=w.sent_this_run;epoch=w.epoch
  self.assertTrue(await self.probe([self.reply()]))
  self.assertIs(w.pending,p);self.assertTrue(p['confirmed']);self.assertTrue(w.running);self.assertEqual(w.epoch,epoch);self.assertEqual(w.sent_this_run,before)
  self.assertEqual(w.store.db.execute('select status from sends').fetchone()[0],'sent');self.page.close.assert_awaited_once()
 async def test_absence_preserves_unknown(self):
  self.assertFalse(await self.probe());self.assertFalse(self.w.pending['confirmed'])
  self.assertEqual(self.w.store.db.execute('select status from sends').fetchone()[0],'uncertain')
 async def test_wrong_author_or_parent_never_confirms(self):
  self.assertFalse(await self.probe([self.reply(author='stranger'),self.reply('r2',parent='other')]))
 async def test_ambiguous_owner_replies_never_confirms(self):
  self.assertFalse(await self.probe([self.reply(),self.reply('r2')]))
 async def test_user_stop_during_probe_does_not_resume(self):
  self.assertFalse(await self.probe([self.reply()],stop=True));self.assertFalse(self.w.running)
 async def test_blocked_or_not_dispatched_never_probes(self):
  for k in ('blocked','dispatched'):
   w=self.w;w.pending['blocked']=k=='blocked';w.pending['dispatched']=k!='dispatched'
   w.verify_unknown=AsyncMock();self.assertFalse(await w.reconcile_pending_receipt(w.pending,w.epoch));w.verify_unknown.assert_not_awaited()
 async def test_timeout_retains_unknown(self):
  self.w.verify_unknown=AsyncMock(side_effect=TimeoutError('budget'))
  self.assertFalse(await self.w.reconcile_pending_receipt(self.w.pending,self.w.epoch));self.assertFalse(self.w.pending['confirmed'])



 async def test_click_timeout_reserved_without_dispatch_can_confirm_unique_proof(self):
  self.w.pending['dispatched']=False
  self.assertTrue(await self.probe([self.reply()],click_timed_out=True))
  self.assertTrue(self.w.pending['confirmed']);self.assertEqual(self.w.sent_this_run,0)
 async def test_click_timeout_without_proof_keeps_reservation(self):
  self.w.pending['dispatched']=False
  self.assertFalse(await self.probe(click_timed_out=True))
  self.assertEqual(self.w.store.db.execute('select status from sends').fetchone()[0],'uncertain')
 async def test_click_timeout_without_reservation_does_not_probe(self):
  self.w.pending['dispatched']=False;self.w.store.db.execute('delete from sends')
  self.w.verify_unknown=AsyncMock()
  self.assertFalse(await self.w.reconcile_pending_receipt(self.w.pending,self.w.epoch,click_timed_out=True))
  self.w.verify_unknown.assert_not_awaited()

 async def test_probe_connection_closed_retains_unknown_for_guarded_hold(self):
  from playwright.async_api import Error
  w=self.w;p=w.pending
  w.verify_unknown=AsyncMock(side_effect=Error('Page.goto: net::ERR_CONNECTION_CLOSED'))
  self.assertFalse(await w.reconcile_pending_receipt(p,w.epoch))
  self.assertIs(w.pending,p);self.assertFalse(p['confirmed']);self.assertTrue(w.running)
  self.assertEqual(w.store.db.execute('select status from sends').fetchone()[0],'uncertain')
 async def test_probe_browser_closed_still_stops(self):
  from playwright.async_api import Error
  self.w.verify_unknown=AsyncMock(side_effect=Error('Target page, context or browser has been closed'))
  with self.assertRaises(Error):await self.w.reconcile_pending_receipt(self.w.pending,self.w.epoch)
 async def test_probe_connection_failure_does_not_undo_user_stop(self):
  from playwright.async_api import Error
  async def fail(*a,**kw):
   self.w.running=False
   raise Error('net::ERR_CONNECTION_RESET')
  self.w.verify_unknown=AsyncMock(side_effect=fail)
  self.assertFalse(await self.w.reconcile_pending_receipt(self.w.pending,self.w.epoch))
  self.assertFalse(self.w.running);self.assertIsNotNone(self.w.pending)
