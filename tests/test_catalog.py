import unittest,sys,copy
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from settings import DEFAULTS,PRESETS
from providers import list_models,generate
class CatalogTests(unittest.TestCase):
 def test_presets(self):
  self.assertEqual(DEFAULTS['model'],'deepseek-flash')
  self.assertTrue(any('千问' in x['name'] for x in PRESETS));self.assertTrue(any('Kimi' in x['name'] for x in PRESETS))
 def test_editor_key_and_chat_suffix(self):
  c=dict(DEFAULTS,api_key='fake-editor-key',base_url='https://example.test/v1/chat/completions')
  with patch('providers.requests.get',return_value=Mock(status_code=200,json=lambda:{'data':[{'id':'model-a'},{'id':'model-a'}]})) as get:
   self.assertEqual(list_models(c)['models'],['model-a']);self.assertEqual(get.call_args.args[0],'https://example.test/v1/models');self.assertEqual(get.call_args.kwargs['headers']['Authorization'],'Bearer fake-editor-key');self.assertFalse(get.call_args.kwargs['allow_redirects'])
 def test_gemini_text_only(self):
  c=dict(DEFAULTS,protocol='gemini',api_key='fake',base_url='https://generativelanguage.googleapis.com')
  rows=[{'name':'models/text','supportedGenerationMethods':['generateContent']},{'name':'models/embed','supportedGenerationMethods':['embedContent']}]
  with patch('providers.requests.get',return_value=Mock(status_code=200,json=lambda:{'models':rows})):
   self.assertEqual(list_models(c)['models'],['text'])
 def test_reject_redirect_and_denied(self):
  for status in [302,401,403,404]:
   with patch('providers.requests.get',return_value=Mock(status_code=status)):
    with self.assertRaises(ValueError):list_models(dict(DEFAULTS,api_key='fake'))
 def test_kimi_fixed_parameters(self):
  response=Mock(status_code=200,json=lambda:{'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]})
  for model in ['kimi-k2.6','kimi-k3']:
   with patch('providers.requests.post',return_value=response) as post,patch('providers.usage_record'):
    generate(dict(DEFAULTS,api_key='fake',base_url='https://api.moonshot.cn/v1',model=model),'test');body=post.call_args.kwargs['json'];self.assertNotIn('temperature',body)
    if model=='kimi-k3':self.assertNotIn('thinking',body)
    else:self.assertEqual(body['thinking']['type'],'disabled')
