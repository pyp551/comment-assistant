import sys,os,tempfile,unittest,json,copy
from pathlib import Path
from unittest.mock import patch,Mock
os.environ['COMMENT_WORKBENCH_DATA']=tempfile.mkdtemp(prefix='comment-assistant-tests-',dir=str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from settings import Settings,DEFAULTS,validate,settings
from providers import generate,balance,usage_summary
import threading,time,requests
import server
class IdentityParserTests(unittest.TestCase):
 def test_self_only(self):
  payload={'status_code':0,'user':{'uid':'12345678','nickname':'test'}}
  self.assertEqual(server.self_account('https://www.douyin.com/aweme/v1/web/user/profile/self/',payload)['uid'],'12345678')
  for url in ('https://www.douyin.com/aweme/v1/web/user/profile/other/','https://evil.example/aweme/v1/web/user/profile/self/'):
   self.assertIsNone(server.self_account(url,payload))
 def test_invalid_response(self):
  for payload in ({'status_code':1,'user':{'uid':'12345678'}},{'status_code':0,'user':{'nickname':'name'}},[]):
   self.assertIsNone(server.self_account('https://www.douyin.com/aweme/v1/web/user/profile/self/',payload))
class SettingsTests(unittest.TestCase):
 def test_default_has_no_key_and_neutral_identity(self):
  self.assertEqual(DEFAULTS['api_key'],'');self.assertEqual(DEFAULTS['provider'],'DeepSeek')
 def test_save_does_not_apply_and_session_key_not_on_disk(self):
  s=Settings();c=copy.deepcopy(DEFAULTS);c.update(api_key='test-session-secret',remember_key=False)
  s.save(c);self.assertNotEqual(s.active,c);self.assertNotIn('test-session-secret',s.path.read_text('utf-8'));s.apply();self.assertEqual(s.active,c)
  fresh=Settings();self.assertEqual(fresh.saved['api_key'],'')
 def test_encrypted_key_roundtrip(self):
  s=Settings();c=copy.deepcopy(DEFAULTS);c['api_key']='test-encryption-secret';s.save(c)
  self.assertNotIn('test-encryption-secret',s.path.read_text('utf-8'));self.assertEqual(Settings().saved['api_key'],c['api_key'])
 def test_endpoint_and_rate_validation(self):
  for patcher in (dict(base_url='http://example.com'),dict(send_interval=0),dict(protocol='unknown')):
   with self.assertRaises(ValueError):validate(dict(DEFAULTS,**patcher))
class AdapterTests(unittest.TestCase):
 def test_three_protocols_and_usage(self):
  cases=[('openai',{'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],'usage':{'prompt_tokens':3,'completion_tokens':2}},'/chat/completions'),('anthropic',{'content':[{'type':'text','text':'ok'}],'usage':{'input_tokens':3,'output_tokens':2}},'/v1/messages'),('gemini',{'candidates':[{'content':{'parts':[{'text':'ok'}]},'finishReason':'STOP'}],'usageMetadata':{'promptTokenCount':3,'candidatesTokenCount':2}},':generateContent')]
  for protocol,payload,end in cases:
   c=dict(DEFAULTS,protocol=protocol,api_key='adapter-'+protocol,base_url='https://test.example',model='test')
   response=Mock(status_code=200);response.json.return_value=payload
   with patch('providers.requests.post',return_value=response) as post:
    self.assertEqual(generate(c,'hello'),'ok');self.assertTrue(post.call_args.args[0].endswith(end));self.assertFalse(post.call_args.kwargs['allow_redirects'])
   self.assertEqual(usage_summary(c)['input'],3)
 def test_tool_call_rejected(self):
  r=Mock(status_code=200);r.json.return_value={'choices':[{'message':{'tool_calls':[{}]}}]}
  with patch('providers.requests.post',return_value=r),self.assertRaises(ValueError):generate(dict(DEFAULTS,api_key='test'),'hello')
 def test_balance_official_host_only(self):
  with patch('providers.requests.get') as get:
   self.assertFalse(balance(dict(DEFAULTS,base_url='https://other.example'))['supported']);get.assert_not_called()
class RoutesTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  server.PORT=18998
  cls.runner=server.uvicorn.Server(server.uvicorn.Config(server.app,host='127.0.0.1',port=server.PORT,log_config=None,access_log=False))
  cls.thread=threading.Thread(target=cls.runner.run,daemon=True);cls.thread.start()
  for _ in range(50):
   if cls.runner.started:break
   time.sleep(.1)
 @classmethod
 def tearDownClass(cls):cls.runner.should_exit=True;cls.thread.join(5)
 def setUp(self):
  class Client:
   def get(self,path,**kw):return requests.get('http://127.0.0.1:18998'+path,timeout=5,**kw)
   def post(self,path,**kw):return requests.post('http://127.0.0.1:18998'+path,timeout=5,**kw)
  self.c=Client();self.h={'x-comment-token':server.TOKEN}
 def test_identity_requires_explicit_confirmation(self):
  r=self.c.post('/api/identity-confirm',headers=self.h,json={})
  self.assertEqual(r.status_code,400)
 def test_identity_rejects_running(self):
  with patch.object(server.watcher,'running',True):
   self.assertEqual(self.c.post('/api/identity-confirm',headers=self.h,json={'confirmed':True}).status_code,400)
 def test_identity_rejects_pending(self):
  with patch.object(server.watcher,'pending',{'comment':{}}):
   self.assertEqual(self.c.post('/api/identity-options',headers=self.h,json={}).status_code,400)
 def test_token_required(self):self.assertEqual(self.c.get('/api/settings').status_code,403)
 def test_ui_and_source(self):
  self.assertEqual(self.c.get('/').status_code,200)
  r=self.c.get('/api/settings',headers=self.h);self.assertEqual(r.status_code,200);self.assertIn('active',r.json())
  self.assertEqual(self.c.get('/api/source/settings.py',headers=self.h).status_code,200)
 def test_apply_rejected_while_running(self):
  with patch.object(server.watcher,'running',True):self.assertEqual(self.c.post('/api/config/apply',headers=self.h,json={}).status_code,400)
 def test_save_preserves_active(self):
  old=copy.deepcopy(settings.active);c=dict(DEFAULTS,model='changed')
  self.assertEqual(self.c.post('/api/config/save',headers=self.h,json=c).status_code,200);self.assertEqual(settings.active,old)
if __name__=='__main__':unittest.main()
