import sys,unittest,tempfile,time,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import Store
from reconcile import ReplyLedger,ScanCoverage
from server import Watcher
VID='7000000000000000001'
def comment(cid='c',author='alice',parent='0',root=None,video=VID,count=0,text='你好'):
 return dict(id=cid,video=video,author=author,name=author,text=text,created=1720000000,parent=parent,root=root or cid,reply_count=count)
class Ledger(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'db';self.s=Store(self.db);self.ledger=ReplyLedger(self.s)
  self.s.remember_own(comment('proof',author='owner',parent='other'))
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def plan(self,rows,complete=True,run='one'):
  self.s.observe(rows);return self.ledger.plan(VID,run,rows,complete)
 def test_old_and_skipped_are_queued_without_resetting_clock(self):
  rows=[comment('a'),comment('b')]
  self.s.mark(rows[0],'old');self.s.mark(rows[1],'skipped','语气 unclear')
  a=self.plan(rows);self.assertEqual(a['counts']['queued'],2)
  for c in rows:self.assertTrue(self.s.new_candidate(c,VID,time.time(),time.time()))
 def test_failed_before_send_can_recheck_but_not_uncertain(self):
  a,b=comment('a'),comment('b');self.s.mark(a,'blocked');self.s.mark(b,'uncertain')
  result=self.plan([a,b]);self.assertEqual(result['counts']['queued'],1);self.assertEqual(result['counts']['review'],1)
 def test_any_attempt_cannot_be_requeued_even_if_item_status_wrong(self):
  c=comment();self.s.reserve_send(c,1);self.s.mark(c,'skipped')
  self.assertEqual(self.plan([c])['counts']['review'],1)
  self.assertFalse(self.s.new_candidate(c,VID,time.time(),time.time()))
 def test_confirmed_first_of_two_is_already_answered(self):
  c=comment();self.s.reserve_send(c,1);self.s.confirm_send(c,1)
  self.assertEqual(self.plan([c])['counts']['answered'],1)
 def test_memory_alone_preserves_success(self):
  c=comment();self.s.remember_reply(c,1,'你好呀')
  self.assertEqual(self.plan([c])['counts']['answered'],1)
 def test_manual_author_reply_targets_exact_parent(self):
  a,b=comment('a'),comment('b');r=comment('r',author='owner',parent='a',root='a')
  result=self.plan([a,b,r]);self.assertEqual(result['counts']['answered'],1);self.assertEqual(result['counts']['queued'],1)
  self.assertFalse(self.s.new_candidate(a,VID,0,time.time()));self.assertTrue(self.s.new_candidate(b,VID,time.time(),time.time()))
 def test_same_nickname_does_not_establish_owner(self):
  c=comment();fake=dict(comment('r',author='someone',parent='c',root='c'),name='owner')
  result=self.plan([c,fake]);self.assertEqual(result['counts']['queued'],2)
 def test_other_video_does_not_supply_answer(self):
  c=comment();self.s.observe([comment('r',author='owner',parent='c',video='other')])
  self.assertEqual(self.plan([c])['counts']['queued'],1)
 def test_nested_author_reply_does_not_answer_new_sibling(self):
  root=comment('root');reply=comment('r',author='owner',parent='root',root='root');new=comment('new',parent='r',root='root')
  result=self.plan([root,reply,new]);self.assertEqual(result['counts']['queued'],1)
  self.assertTrue(self.s.new_candidate(new,VID,time.time(),time.time()))
 def test_missing_parent_or_unknown_identity_goes_to_review(self):
  c=comment(parent='missing',root='missing')
  self.assertEqual(self.plan([c])['counts']['review'],1)
  self.s.db.execute('DELETE FROM own_authors');self.s.db.commit()
  self.assertTrue(any(r['id']=='c' and r['status']=='review' for r in self.plan([comment()])['items']))
 def test_partial_scan_never_enqueues_history(self):
  self.assertEqual(self.plan([comment()],False)['counts']['review'],1)
  self.assertFalse(self.s.new_candidate(comment(),VID,time.time(),time.time()))
 def test_no_unbounded_recent_40_cutoff(self):
  rows=[comment(str(i)) for i in range(85)]
  self.assertEqual(self.plan(rows)['counts']['queued'],85)
 def test_muted_thread_never_requeued(self):
  c=comment();self.s.update_discussion(VID,'c',muted=1)
  self.assertEqual(self.plan([c])['counts']['muted'],1)
 def test_restart_queue_survives_and_sent_never_reappears(self):
  c=comment();self.plan([c]);self.s.db.close();self.s=Store(self.db);self.ledger=ReplyLedger(self.s)
  self.assertEqual(self.plan([c],run='two')['counts']['queued'],1)
  self.s.mark(c,'sent','你好 → 你好呀')
  self.assertEqual(self.plan([c],run='three')['counts']['answered'],1)
 def test_missing_historic_comment_is_not_silently_dropped(self):
  c=comment();self.plan([c]);result=self.plan([])
  self.assertEqual(result['counts']['unavailable'],1);self.assertFalse(self.s.new_candidate(c,VID,time.time(),time.time()))
 def test_policy_skip_remains_visible_after_queue(self):
  c=comment();self.plan([c]);self.s.mark(c,'skipped','模型返回不完整')
  self.assertEqual(self.ledger.summary(VID)['counts']['review'],1)
 def test_queue_is_loaded_from_database_without_browser_cache(self):
  c=comment();self.plan([c]);self.s.db.close();self.s=Store(self.db);self.ledger=ReplyLedger(self.s)
  self.assertEqual(self.ledger.pending_comments(VID)[0]['id'],'c')
  self.assertEqual(self.ledger.pending_comments('other'),[])
class Coverage(unittest.TestCase):
 def test_unparsed_comment_cannot_be_claimed_complete(self):
  c=ScanCoverage(VID);c.observe([comment()],True,has_more=0,expected_ids=['c','missing'])
  self.assertFalse(c.complete())
 def test_malformed_comment_cannot_be_claimed_complete(self):
  c=ScanCoverage(VID);c.observe([],True,has_more=0,expected_ids=[None])
  self.assertFalse(c.complete())
 def test_end_missing_is_not_complete(self):
  c=ScanCoverage(VID);c.observe([comment()],True,total=1);self.assertFalse(c.complete())
 def test_pagination_and_collapsed_replies_must_complete(self):
  c=ScanCoverage(VID);c.observe([comment(count=2)],True,has_more=1,total=2);self.assertFalse(c.complete())
  c.observe([comment('two')],True,has_more=0,total=2);self.assertFalse(c.complete())
  c.observe([comment('r',parent='c',root='c')]);self.assertFalse(c.complete())
  c.observe([comment('s',parent='r',root='c')]);self.assertTrue(c.complete())
 def test_reply_endpoint_cannot_claim_root_list_end(self):
  c=ScanCoverage(VID);c.observe([comment()],False,has_more=0,total=0);self.assertFalse(c.complete())
 def test_verified_empty_is_complete(self):
  c=ScanCoverage(VID);c.observe([],True,total=0);self.assertTrue(c.complete())
 def test_other_video_and_duplicate_replies_do_not_satisfy_count(self):
  c=ScanCoverage(VID);c.observe([comment(count=2)],True,has_more=0)
  r=comment('r',parent='c',root='c');c.observe([r,r,comment('other',parent='c',root='c',video='elsewhere')])
  self.assertFalse(c.complete())
class ScanFlow(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.w=Watcher();self.w.store.close() if hasattr(self.w.store,'close') else self.w.store.db.close()
  self.w.store=Store(Path(self.tmp.name)/'db');self.w.store.remember_own(comment('proof',author='owner',parent='other'))
  w=self.w;w.running=True;w.selected=VID;w.started=time.time();w.healthy=AsyncMock();w.scroll_comments=AsyncMock(return_value=False);w.expand_history_threads=AsyncMock()
  w.history_scroll=AsyncMock(return_value=0)
  w.page=SimpleNamespace(url='https://creator.douyin.com/creator-micro/work-management/work-detail/'+VID,reload=AsyncMock())
 def tearDown(self):self.w.store.db.close();self.tmp.cleanup()
 async def test_scan_complete_queues_old_and_preserves_start(self):
  w=self.w;c=comment();start=w.started
  async def pause(epoch,seconds=1):
   w.coverage.observe([c],True,has_more=0);w.store.observe([c]);return True
  w.pause_scan=pause
  self.assertTrue(await w.scan_history(w.epoch));await w.history_step(w.epoch);self.assertEqual(w.started,start);self.assertEqual(w.phase,'running')
  self.assertTrue(w.store.new_candidate(c,VID,start,time.time()))
 async def test_stop_during_scan_prevents_queue_and_send(self):
  w=self.w
  async def pause(epoch,seconds=1):w.stop();return False
  w.pause_scan=pause
  await w.scan_history(w.epoch);await w.history_step(w.epoch);self.assertFalse(w.running)
  self.assertFalse(w.store.db.execute("SELECT 1 FROM items WHERE status='restart_pending'").fetchone())
 async def test_incomplete_scan_keeps_listening_with_visible_reason(self):
  w=self.w;c=comment(count=1)
  async def pause(epoch,seconds=1):
   w.coverage.observe([c],True,has_more=0);w.store.observe([c]);return True
  w.pause_scan=pause
  await w.scan_history(w.epoch)
  for _ in range(7):await w.history_step(w.epoch)
  self.assertTrue(w.running);self.assertFalse(w.reconcile_status['complete'])
  self.assertEqual(w.reconcile_status['missing_threads'][0]['id'],'c')
 async def test_old_stop_request_is_honored(self):
  w=self.w;c=comment(text='别回复我了')
  async def pause(epoch,seconds=1):
   w.coverage.observe([c],True,has_more=0);w.store.observe([c]);return True
  w.pause_scan=pause
  self.assertTrue(await w.scan_history(w.epoch));await w.history_step(w.epoch);self.assertTrue(w.store.discussion(VID,'c')['muted'])

class ScrollProgress(ScanFlow):
 async def test_traversing_known_rows_does_not_end_pass_early(self):
  w=self.w;c=comment(count=1)
  async def pause(epoch,seconds=1):
   w.coverage.observe([c],True,has_more=1);w.store.observe([c]);return True
  w.pause_scan=pause;w.scroll_comments=AsyncMock(return_value=True);w.expand_history_threads=AsyncMock(return_value=False)
  await w.scan_history(w.epoch)
  for _ in range(8):await w.history_step(w.epoch)
  self.assertEqual(w.reconcile_status['phase'],'scanning');self.assertTrue(w.running)
