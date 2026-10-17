import json,time,sqlite3,hashlib
from contextlib import contextmanager
from urllib.parse import quote,urlparse
import requests
from settings import DATA

@contextmanager
def usage_db():
 d=sqlite3.connect(DATA/'usage.sqlite3')
 try:
  yield d
  d.commit()
 finally:d.close()

def usage_record(c,usage,ok):
 with usage_db() as d:
  d.execute('CREATE TABLE IF NOT EXISTS usage(at REAL,provider TEXT,model TEXT,key_id TEXT,input INTEGER,output INTEGER,ok INTEGER)')
  inp=usage.get('prompt_tokens',usage.get('input_tokens',usage.get('promptTokenCount',0))) or 0
  out=usage.get('completion_tokens',usage.get('output_tokens',usage.get('candidatesTokenCount',0))) or 0
  d.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?)',(time.time(),c['provider'],c['model'],hashlib.sha256(c['api_key'].encode()).hexdigest()[:16],inp,out,int(ok)))
def usage_summary(c):
 if not (DATA/'usage.sqlite3').exists():return dict(calls=0,failures=0,input=0,output=0)
 with usage_db() as d:r=d.execute('SELECT COUNT(*),SUM(1-ok),SUM(input),SUM(output) FROM usage WHERE key_id=?',(hashlib.sha256(c['api_key'].encode()).hexdigest()[:16],)).fetchone()
 return dict(zip(('calls','failures','input','output'),[x or 0 for x in r]))
def generate(c,text,test=False):
 if not c['api_key'] or not c['model'] or not c['base_url']:raise ValueError('请填写接口地址、模型名和 API Key')
 base=c['base_url'].rstrip('/');p=c['protocol'];sys=c['system_prompt']+'\n\n'+c['format_prompt'];key=c['api_key']
 if test:sys='你是连接测试助手。请简短回复用户。'
 if p=='openai':
  url=base if base.endswith('/chat/completions') else base+'/chat/completions'
  headers={'Authorization':'Bearer '+key};body=dict(model=c['model'],messages=[dict(role='system',content=sys),dict(role='user',content=text)],stream=False,max_tokens=int(c['max_tokens']),temperature=c['temperature'])
  host=urlparse(base).hostname or ''
  if host=='api.deepseek.com':
   body['thinking']={'type':'enabled' if c.get('thinking',False) else 'disabled'}
  elif host in ('api.moonshot.cn','api.moonshot.ai'):
   if c['model'] in ('kimi-k2.5','kimi-k2.6'):
    body['thinking']={'type':'enabled' if c.get('thinking',False) else 'disabled'}
    body.pop('temperature',None)
   elif c['model'].startswith(('kimi-k3','kimi-k2.7')):body.pop('temperature',None)
  elif host.endswith('aliyuncs.com'):body['enable_thinking']=c.get('thinking',False)
 elif p=='anthropic':
  url=base if base.endswith('/messages') else base+('/messages' if base.endswith('/v1') else '/v1/messages')
  headers={'x-api-key':key,'anthropic-version':'2023-06-01'};body=dict(model=c['model'],system=sys,messages=[dict(role='user',content=text)],max_tokens=int(c['max_tokens']),temperature=min(1,c['temperature']))
 else:
  url=base+('' if base.endswith('/v1beta') else '/v1beta')+'/models/'+quote(c['model'],safe='')+':generateContent'
  headers={'x-goog-api-key':key};body=dict(systemInstruction=dict(parts=[dict(text=sys)]),contents=[dict(role='user',parts=[dict(text=text)])],generationConfig=dict(temperature=c['temperature'],maxOutputTokens=int(c['max_tokens'])))
 try:
  response=requests.post(url,headers=headers,json=body,timeout=(8,c['timeout']),allow_redirects=False)
  if 300<=response.status_code<400:raise ValueError('接口重定向已拒绝，请填写最终接口地址')
  response.raise_for_status();r=response.json()
  if p=='openai':
   m=r['choices'][0]['message']
   if m.get('tool_calls') or m.get('function_call') or r['choices'][0].get('finish_reason')=='length':raise ValueError('模型返回工具调用或截断内容，未发送')
   out=m.get('content') or '';usage=r.get('usage',{})
  elif p=='anthropic':
   if any(x['type']=='tool_use' for x in r['content']) or r.get('stop_reason')=='max_tokens':raise ValueError('模型输出无效')
   out=''.join(x.get('text','') for x in r['content']);usage=r.get('usage',{})
  else:
   candidate=r['candidates'][0];parts=candidate['content']['parts']
   if any('functionCall' in x for x in parts) or candidate.get('finishReason') not in (None,'STOP'):raise ValueError('模型输出无效')
   out=''.join(x.get('text','') for x in parts if not x.get('thought'));usage=r.get('usageMetadata',{})
  if not out.strip():raise ValueError('模型没有返回文字')
  usage_record(c,usage,True);return out
 except Exception:
  usage_record(c,{},False);raise

def balance(c):
 if urlparse(c['base_url']).hostname!='api.deepseek.com':return dict(supported=False,message='当前厂商未接入统一余额查询，请到厂商控制台查看；本地用量仍可统计')
 if not c['api_key']:raise ValueError('尚未配置生效密钥')
 r=requests.get('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+c['api_key']},timeout=(8,20),allow_redirects=False);r.raise_for_status()
 return dict(supported=True,checked_at=time.time(),**r.json())


def list_models(c):
 """Explicit read-only catalog request using editor settings, not active secrets."""
 if not c['api_key'] or not c['base_url']:raise ValueError('请先填写 API Key 与接口地址')
 base=c['base_url'].rstrip('/');p=c['protocol'];headers={}
 if p=='openai':
  if base.endswith('/chat/completions'):base=base[:-len('/chat/completions')]
  url=base+'/models';headers={'Authorization':'Bearer '+c['api_key']}
 elif p=='anthropic':
  if base.endswith('/messages'):base=base[:-len('/messages')]
  url=base+('/models' if base.endswith('/v1') else '/v1/models');headers={'x-api-key':c['api_key'],'anthropic-version':'2023-06-01'}
 else:
  url=base+('' if base.endswith('/v1beta') else '/v1beta')+'/models';headers={'x-goog-api-key':c['api_key']}
 r=requests.get(url,headers=headers,timeout=(8,20),allow_redirects=False)
 if 300<=r.status_code<400:raise ValueError('模型列表接口发生重定向，请使用最终接口地址')
 if r.status_code in (401,403):raise ValueError('模型列表鉴权失败，请核对厂商、地域、Key 与权限')
 if r.status_code==404:raise ValueError('此地址未提供模型列表；可选择预设或手动填写厂商模型 ID')
 r.raise_for_status();data=r.json();rows=data.get('models',[]) if p=='gemini' else data.get('data',[])
 ids=[]
 for row in rows:
  if p=='gemini' and 'generateContent' not in row.get('supportedGenerationMethods',[]):continue
  name=row.get('name','').removeprefix('models/') if p=='gemini' else row.get('id','')
  if isinstance(name,str) and name and name not in ids:ids.append(name)
 return dict(models=ids[:500],checked_at=time.time(),message='来自厂商接口；仅当前返回页。是否支持文字回复、账户调用权限仍需测试连接确认。')
