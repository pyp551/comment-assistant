import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch,Mock
from server import DiscoveryWatcher,HistoryWatcher,PlaywrightTimeoutError,PlaywrightError

class NavigationRecovery(unittest.IsolatedAsyncioTestCase):
 def make(self):
  h=object.__new__(DiscoveryWatcher);h.running=True;h.owner=SimpleNamespace(running=True,epoch=7);h.selected='7000000000000000001';h.status={}
  h.page=SimpleNamespace(url='https://creator.douyin.com/creator-micro/work-management/work-detail/'+h.selected,reload=AsyncMock(),goto=AsyncMock())
  h.healthy=AsyncMock();return h
 async def test_timeout_retries_readonly_without_stopping_owner(self):
  h=self.make();h.page.reload.side_effect=[PlaywrightTimeoutError('slow'),None]
  with patch('server.asyncio.sleep',new=AsyncMock()):self.assertTrue(await h.navigate_readonly(h.page.url,False,7))
  self.assertEqual(h.page.reload.await_count,2);self.assertTrue(h.owner.running);self.assertEqual(h.status['navigation_retries'],0)
 async def test_three_timeouts_defer_unverified_page(self):
  h=self.make();h.page.reload.side_effect=PlaywrightTimeoutError('slow')
  with patch('server.asyncio.sleep',new=AsyncMock()):
   self.assertIsNone(await h.navigate_readonly(h.page.url,False,7))
  self.assertTrue(h.owner.running)
  self.assertEqual(h.page.reload.await_count,3)
 async def test_platform_health_failure_never_retries(self):
  h=self.make();h.page.reload.side_effect=PlaywrightTimeoutError('slow');h.healthy.side_effect=[None,ValueError('captcha')]
  with self.assertRaisesRegex(ValueError,'captcha'):await h.navigate_readonly(h.page.url,False,7)
  self.assertEqual(h.page.reload.await_count,1)
 async def test_scope_change_never_retries(self):
  h=self.make();h.page.url='https://creator.douyin.com/login'
  with self.assertRaises(ValueError):await h.navigate_readonly(h.page.url,False,7)
  h.page.reload.assert_not_awaited()
 async def test_stop_during_backoff_prevents_retry(self):
  h=self.make();h.page.reload.side_effect=PlaywrightTimeoutError('slow')
  async def stop(_):h.owner.running=False
  with patch('server.asyncio.sleep',side_effect=stop):self.assertFalse(await h.navigate_readonly(h.page.url,False,7))
  self.assertEqual(h.page.reload.await_count,1)
 async def test_history_uses_same_protected_helper(self):
  self.assertIs(HistoryWatcher.navigate_readonly,DiscoveryWatcher.navigate_readonly)
  h=self.make();self.assertTrue(await h.navigate_readonly(h.page.url,True,7));h.page.goto.assert_awaited_once();h.page.reload.assert_not_awaited()

 async def test_transport_retry_keeps_pending_sender_intact(self):
  for code in ('net::ERR_CONNECTION_CLOSED','net::ERR_CONNECTION_RESET','net::ERR_TIMED_OUT'):
   h=self.make();pending=object();h.owner.pending=pending
   h.page.reload.side_effect=[PlaywrightError(code),None]
   with patch('server.asyncio.sleep',new=AsyncMock()):self.assertTrue(await h.navigate_readonly(h.page.url,False,7))
   self.assertIs(h.owner.pending,pending);self.assertTrue(h.owner.running)
 async def test_transport_exhaustion_does_not_stop_or_clear_pending(self):
  h=self.make();pending=object();h.owner.pending=pending
  h.page.reload.side_effect=PlaywrightError('net::ERR_CONNECTION_CLOSED')
  with patch('server.asyncio.sleep',new=AsyncMock()):self.assertIsNone(await h.navigate_readonly(h.page.url,False,7))
  self.assertEqual(h.page.reload.await_count,3);self.assertIs(h.owner.pending,pending);self.assertTrue(h.owner.running)
 async def test_browser_closed_is_not_recoverable(self):
  h=self.make();h.page.reload.side_effect=PlaywrightError('Target page, context or browser has been closed')
  with self.assertRaises(PlaywrightError):await h.navigate_readonly(h.page.url,False,7)
  self.assertEqual(h.page.reload.await_count,1)
 async def test_transport_health_failure_is_fatal(self):
  h=self.make();h.page.reload.side_effect=PlaywrightError('net::ERR_CONNECTION_CLOSED');h.healthy.side_effect=[None,ValueError('captcha')]
  with self.assertRaisesRegex(ValueError,'captcha'):await h.navigate_readonly(h.page.url,False,7)
 async def test_epoch_change_during_health_does_not_retry(self):
  h=self.make();h.page.reload.side_effect=PlaywrightError('net::ERR_CONNECTION_CLOSED')
  async def health():h.owner.epoch+=1
  h.healthy.side_effect=health
  self.assertFalse(await h.navigate_readonly(h.page.url,False,7))

 async def test_deferred_navigation_skips_traversal_and_retries_both_readers(self):
  for cls in (DiscoveryWatcher,HistoryWatcher):
   h=self.make();h.context=SimpleNamespace(new_page=AsyncMock(return_value=h.page))
   h.page.on=Mock();h.schedule_read=Mock();h.stop=Mock()
   h.navigate_readonly=AsyncMock(side_effect=[None,False]);h.wait_video_read=AsyncMock()
   h.scan_history=AsyncMock()
   with patch('server.asyncio.sleep',new=AsyncMock()):await cls.run(h,7)
   self.assertEqual(h.navigate_readonly.await_count,2)
   h.wait_video_read.assert_not_awaited();h.stop.assert_not_called();self.assertTrue(h.owner.running)

 async def test_history_body_timeout_rebuild_keeps_pending(self):
  from server import PageNotReady
  h=self.make();h.owner.healthy=AsyncMock();h.owner.page=h.page;h.owner.event=Mock();h.page.close=AsyncMock();h.stop=Mock();pending=object();h.owner.pending=pending
  with patch.object(HistoryWatcher,'run_once',new=AsyncMock(side_effect=[PageNotReady('slow'),None])) as run,patch('server.asyncio.sleep',new=AsyncMock()):
   await HistoryWatcher.run(h,7)
  self.assertEqual(run.await_count,2);self.assertIs(h.owner.pending,pending);h.stop.assert_not_called()
 async def test_history_body_timeout_captcha_stops(self):
  from server import PageNotReady
  h=self.make();h.owner.healthy=AsyncMock(side_effect=ValueError('captcha'));h.stop=Mock()
  with patch.object(HistoryWatcher,'run_once',new=AsyncMock(side_effect=PageNotReady('slow'))) as run:
   await HistoryWatcher.run(h,7)
  self.assertEqual(run.await_count,1);h.stop.assert_called_once()
