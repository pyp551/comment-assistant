"""Local-only trial. Normal visible Edge, passive response observation, no private API calls."""
import asyncio
import base64
import ctypes
import json
import hashlib
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
import requests
import uvicorn
from reconcile import ReplyLedger, ScanCoverage
from core import Store, extract_comments, eligible, parse_reply, video_id
from persona import SYSTEM, VERSION, allow_two, parse_decision, thread_context, asks_to_stop, video_memory, repeated_reply, image_placeholder, conversational_fallback

from settings import settings, DATA, model_config
from providers import generate
DATA = DATA
DATA.mkdir(parents=True, exist_ok=True)
TOKEN = secrets.token_urlsafe(32)
PORT = int(os.environ.get('COMMENT_WORKBENCH_PORT', '8896'))
CREATOR = 'https://creator.douyin.com/creator-micro/interactive/comment'
CONFIG = Path(os.environ.get('COMMENT_WORKBENCH_HOME', str(Path(os.environ.get('LOCALAPPDATA', '')) / 'CommentWorkbench'))) / 'settings.json'
EDGE = Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'

def request_page(request):
    # Playwright raises on .frame for Service Worker requests. They still need
    # routing and an exact pending-request receipt must still be consumed.
    try:return request.frame.page
    except Exception:return None

def self_account(url, payload):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname not in ('www.douyin.com','creator.douyin.com'):return None
    if parsed.path.rstrip('/')!='/aweme/v1/web/user/profile/self':return None
    if not isinstance(payload,dict) or payload.get('status_code')!=0:return None
    user=payload.get('user')
    if not isinstance(user,dict):return None
    uid=str(user.get('uid') or '')
    if not re.fullmatch(r'\d{5,25}',uid):return None
    return dict(uid=uid,nickname=str(user.get('nickname') or ''),source='登录账号接口')

def comment_search_keyword(text):
    # The creator search is a single-line input. Passing a multi-line original
    # silently removes its line breaks, joining words into a different query.
    # A short literal first line is only a discovery hint: full text, author,
    # thread scope and outgoing IDs are still checked before sending.
    for line in str(text).splitlines():
        line=re.sub(r'\[[^\]\r\n]{1,24}\]','',line).strip()
        if line:return line[:24]
    return ''


def validate_resume_quarantine(db, resume):
    # Only an explicit local maintenance checkpoint may preserve known unknown sends.
    # Never change their status or make them eligible for another send.
    if db.execute("SELECT 1 FROM items WHERE status='drafting' LIMIT 1").fetchone():
        raise ValueError('仍有未完成草稿，不能维修恢复')
    actual={
        'items':[list(r) for r in db.execute("SELECT video,cid,status,updated FROM items WHERE status='uncertain' ORDER BY video,cid")],
        'sends':[list(r) for r in db.execute("SELECT video,cid,part,status,updated FROM sends WHERE status='uncertain' ORDER BY video,cid,part")],
    }
    if actual['items'] or actual['sends']:
        if resume.get('owner_authorized_quarantine') is not True or resume.get('quarantine')!=actual:
            raise ValueError('有未核准隔离的未知发送，不能恢复监听')


def trial_caps_enabled():
    # Owner-only local policy; comments and model output cannot alter this file.
    try:
        policy=json.loads((DATA/'owner-policy.json').read_text('utf-8'))
        return policy.get('trial_caps_enabled') is True
    except (OSError,ValueError,AttributeError):
        return False

def validate_owner_retry(db,resume):
    job=resume.get('owner_retry')
    if job is None:return None
    if job.get('authorized') is not True or job.get('video')!=resume.get('video'):
        raise ValueError('指定补发授权无效')
    key=(job['video'],job['cid'])
    old=db.execute("SELECT updated FROM sends WHERE video=? AND cid=? AND part=1 AND status='uncertain'",key).fetchone()
    detail=db.execute("SELECT detail FROM items WHERE video=? AND cid=? AND status='uncertain'",key).fetchone()
    if not old or old[0]!=job.get('original_stamp') or not detail or detail[0]!='准备发送，若中断不自动重发：'+job.get('reply',''):
        raise ValueError('指定补发与原未知记录不一致')
    if db.execute('SELECT 1 FROM sends WHERE video=? AND cid=? AND part=100',key).fetchone():
        raise ValueError('这次指定补发已尝试，不自动重试')
    return job


class ModelUnavailable(Exception):
    pass


def decide(comment, facts, style, context=(), memory=None, rewrite=None):
    cfg = model_config()
    endpoint = str(cfg.get('agent_url', '')).rstrip('/')
    if not cfg.get('agent_key'):
        raise ValueError('请在 API 配置中填写并保存接口地址、模型和 API Key')
    image_greeting = image_placeholder(comment['text'])
    two = False if image_greeting else allow_two(comment)
    payload = {'作者确认资料': facts, '语气偏好': style, 'allow_two': two,
               '当前评论': comment['text'], 'image_greeting': image_greeting,
               '讨论上下文': [{'作者': c['author'], '原话': c['text'][:500]} for c in context]}
    payload['同一视频记忆']=memory or {}
    late=bool(comment.get('created') and time.time()-comment['created']>=3600)
    if late:
        payload['迟回复说明']=settings.active['late_prompt']
        two=False;payload['allow_two']=False
    previous=[r['reply'] for r in (memory or {}).get('本视频已确认发出的回复',[])]
    for attempt in range(2):
        if rewrite:
            payload['本次重写要求']={'原因':settings.active['rewrite_prompt'],'重复草稿':rewrite}
        try:
            content = generate(settings.active, json.dumps(payload, ensure_ascii=False))
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (429,502,503,504):
                raise ModelUnavailable('模型暂不可用，请等待自动退避') from None
            raise ValueError('模型接口请求失败，请检查 API 配置') from None
        choice={'finish_reason':'stop'};message={'content':content}
        try:
            if choice.get('finish_reason')=='length':raise ValueError('截断')
            decision=parse_decision(message.get('content') or '',two)
            if decision['action']=='skip':return decision
            if decision['action']=='reply' and repeated_reply(decision['replies'],previous):raise ValueError('重复')
            decision=late_apology(decision,comment)
            if decision['action']=='reply' and repeated_reply(decision['replies'],previous):raise ValueError('重复')
            return decision
        except (ValueError,TypeError,KeyError):
            rewrite=['请重新生成一条完整且不同的短回复']
    # Both attempts were language generation only; neither opened the reply editor.
    decision=dict(action='skip',tone='unclear',confidence=0,intent='chat',replies=[])
    if decision['action']=='reply' and not repeated_reply(decision['replies'],previous):return decision
    return dict(action='skip',tone='unclear',confidence=0,intent='chat',replies=[],validation_error='生成暂未得到完整且不重复的短回复，稍后重新生成')


def late_apology(decision,comment):
    if decision['action']!='reply' or not comment.get('created') or time.time()-comment['created']<3600:return decision
    parts=list(decision['replies']);first=parts[0]
    if not any(word in first for word in ('抱歉','对不起','回晚','来晚','久等')):
        prefix=settings.active['late_prefix']
        if len(prefix+first)>40:raise ValueError('迟回复过长，重新生成短句')
        parts[0]=prefix+first
    return dict(decision,replies=parts[:1])



def draft(comment, facts, style):
    # Retain the manual language-only smoke-test entry point.
    c=dict(video='smoke',id='smoke',**comment)
    return ' / '.join(decide(c,facts,style)['replies'])


def platform_host(url):
    host = urlparse(url).hostname or ''
    return host == 'douyin.com' or host.endswith('.douyin.com')


class NonTextRequestBody(ValueError):
    pass


def request_fields(request):
    try:
        raw = request.post_data or ''
    except UnicodeDecodeError as exc:
        raise NonTextRequestBody('Request body is not UTF-8 text') from exc
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return {k: str(v) for k, v in value.items()}
    except ValueError:
        pass
    return {k: v[-1] for k, v in parse_qs(raw).items()}


class PageNotReady(RuntimeError):
    """A read-only page health probe timed out; no publication is retried."""


class ThreadPending(ValueError):
    """Local thread is unreadable; do not spend a full-video search on it."""

class Watcher:
    def __init__(self, store=None):
        self.store = store if store is not None else Store(DATA / 'comments.sqlite3')
        self.coverage = None
        self.root_snapshot = None
        self.history_failed = set()
        self.history_next = 0
        self.history_position = 0
        self.history_steps = 0
        self.history_stalled = 0
        self.history_progress = None
        self.reconcile_status = {'phase':'idle','complete':False}
        self.context = self.page = self.pw = None
        self.running = False
        self.started = 0
        self.selected = ''
        self.message = '尚未启动。先登录创作者中心，选择自己的视频'
        self.comments = {}
        self.observed = 0
        self.task = None
        self.epoch = 0
        self.next_check = 0
        self.last_reload = 0
        self.last_send = self.store.last_attempt()
        self.facts = self.style = ''
        self.events = []
        self.pending = None
        self.lock = asyncio.Lock()
        self.reader_tasks = set()
        self.sent_this_run = 0
        self.response_shapes = []
        self.ui_probe = {}
        self.video_reads = {}
        self.phase = 'idle'
        self.fold_cursor = 0
        self.followups = []
        self.poll_interval = settings.active['poll_interval']
        self.send_interval = settings.active['send_interval']
        self.prepared_jobs = {}
        self.model_slots = asyncio.Semaphore(2)
        self.model_jobs = set()
        self.external_drafts = {}
        self.model_failures = 0
        self.model_retry_at = 0
        self.unknown_streak = 0
        self.draft_limit = 3
        self.timings = []
        self.reply_signatures = {}
        self.locate_positions = {}
        self.root_positions = {}
        self.history_failed_until = {}
        self.locate_budget = 8
        self.locate_backoff = {}
        self.discovery = None
        self.discovery_task = None
        self.history_reader = None
        self.history_task = None
        self.search_text = ''
        self.search_positions = {}
        self.owner_retry = None
        self.priority_turn = 0
        self.loaded_roots = set()
        self.last_discovery_sync = 0

    def verified_comment_ids(self):
        verified=self.coverage.verified_ids() if self.coverage else set()
        d=getattr(self,'discovery',None)
        if d and d.running and d.coverage:
            verified |= d.coverage.verified_ids()
        h=getattr(self,'history_reader',None)
        if h and h.running and h.coverage:
            verified |= h.coverage.verified_ids()
        return verified

    async def close_discovery(self):
        h=getattr(self,'history_reader',None)
        ht=getattr(self,'history_task',None)
        self.history_reader=None;self.history_task=None
        if ht and ht is not asyncio.current_task():
            ht.cancel()
            await asyncio.gather(ht,return_exceptions=True)
        if h:
            h.running=False
            if h.page and not h.page.is_closed():await h.page.close()
        d=getattr(self,'discovery',None)
        self.discovery=None
        task=getattr(self,'discovery_task',None)
        self.discovery_task=None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        if d:
            d.running=False
            if d.page and not d.page.is_closed():await d.page.close()

    @property
    def prepared(self):
        # Compatibility for diagnostics; all cached jobs live in prepared_jobs.
        return next(iter(self.prepared_jobs.values()),None)

    def discard_draft(self,cid):
        job=self.prepared_jobs.pop(cid,None)
        if job and not job['task'].done():job['task'].cancel()
        elif job and not job['task'].cancelled():job['task'].exception()

    def clear_prepared(self):
        for cid in list(getattr(self,'prepared_jobs',{})):self.discard_draft(cid)

    async def model_decision(self,c,context,memory):
        # Cancellation of an asyncio wrapper cannot stop requests in a thread.
        # Keep the actual call tracked and its slot occupied until it finishes.
        epoch=self.epoch;facts=self.facts;style=self.style;started=False
        async def run():
            nonlocal started
            async with self.model_slots:
                if not self.running or self.epoch!=epoch:return dict(action='skip',tone='unclear',replies=[])
                if time.time()<self.model_retry_at:raise ModelUnavailable('模型退避等待中')
                started=True
                try:
                    result=await asyncio.to_thread(decide,dict(c),facts,style,context,memory)
                except (ModelUnavailable,requests.Timeout,requests.ConnectionError):
                    if self.running and self.epoch==epoch and time.time()>=self.model_retry_at:
                        self.model_failures+=1
                        delay=min(600,30*2**min(self.model_failures-1,5))
                        self.model_retry_at=time.time()+delay
                        self.event(f'模型连接异常，{delay}秒后自动重试；评论抓取继续，尚未发送')
                    raise
                if self.running and self.epoch==epoch:
                    self.model_failures=0;self.model_retry_at=0
                return result
        task=asyncio.create_task(run())
        self.model_jobs.add(task)
        def finished(job):
            self.model_jobs.discard(job)
            if not job.cancelled():job.exception()
        task.add_done_callback(finished)
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not started:task.cancel()
            raise

    def prepare_batch(self,candidates,epoch,current=None):
        # Same person/thread are serial lanes. Never generate two speculative
        # turns of their conversation using the same old context.
        authors={current['author']} if current else set()
        roots={current['root']} if current else set()
        chosen=[]
        for c in candidates:
            if c['author'] in authors or c['root'] in roots:continue
            if current and c['id']==current['id']:continue
            chosen.append(c);authors.add(c['author']);roots.add(c['root'])
            if len(chosen)>=self.draft_limit:break
        keep={c['id'] for c in chosen}
        for cid in list(self.prepared_jobs):
            if cid not in keep:self.discard_draft(cid)
        for c in chosen:self.prepare_draft(c,epoch)

    def draft_inputs(self,c):
        history=self.store.history(c['video'],root=c['root'],before=c['created'],limit=24)
        for key in (c['root'],c['parent']):
            parent=self.comments.get((c['video'],key))
            if parent and all(x['id']!=key for x in history):history.append(parent)
        _,context=thread_context(c,history)
        memory=video_memory(self.store,c)
        # Other threads may progress while generating. Invalidate if this person,
        # this thread, the target, or owner settings change; global wording is
        # checked again immediately before accepting the prepared decision.
        person=self.store.history(c['video'],author=c['author'],limit=40)
        thread=self.store.history(c['video'],root=c['root'],limit=40)
        signature=json.dumps([c,self.facts,self.style,person,thread,self.store.recent_replies(c['video'],author=c['author'])],sort_keys=True,ensure_ascii=False)
        return context,memory,signature

    def prepare_draft(self,c,epoch):
        if c['id'] in self.external_drafts:return
        if not self.running or self.epoch!=epoch or c['video']!=self.selected:return
        if ReplyLedger(self.store).answered(c) or self.store.discussion(c['video'],c['root'])['muted']:return
        context,memory,signature=self.draft_inputs(c)
        job=self.prepared_jobs.get(c['id'])
        if job and job['epoch']==epoch and job['signature']==signature:return
        self.discard_draft(c['id'])
        if len(self.prepared_jobs)>=self.draft_limit:return
        self.prepared_jobs[c['id']]=dict(id=c['id'],epoch=epoch,signature=signature,
            task=asyncio.create_task(self.model_decision(c,context,memory)))

    async def take_draft(self,c,epoch):
        external=self.external_drafts.pop(c['id'],None)
        if external:
            _,_,signature=self.draft_inputs(c)
            if external['epoch']!=epoch or external['signature']!=hashlib.sha256(signature.encode()).hexdigest():
                raise ThreadPending('外部草稿上下文已变化，作废后重新核实')
            return parse_decision(json.dumps(external['decision'],ensure_ascii=False),False)
        # Every result, including a fresh call, is checked against newly arrived
        # context. Bounded regeneration prevents a busy thread starving others.
        for attempt in range(2):
            if not self.running or self.epoch!=epoch:return dict(action='skip',tone='unclear',replies=[])
            context,memory,signature=self.draft_inputs(c)
            job=self.prepared_jobs.pop(c['id'],None)
            if job and job['epoch']==epoch and job['signature']==signature:
                decision=await job['task']
            else:
                if job and not job['task'].done():job['task'].cancel()
                elif job and not job['task'].cancelled():job['task'].exception()
                decision=await self.model_decision(c,context,memory)
            if not self.running or self.epoch!=epoch:return dict(action='skip',tone='unclear',replies=[])
            _,_,new_signature=self.draft_inputs(c)
            previous=[r['reply'] for r in self.store.recent_replies(c['video'],limit=80)]
            if signature==new_signature and not (decision.get('action')=='reply' and repeated_reply(decision['replies'],previous)):
                return decision
        raise ThreadPending('对话内容正在变化或备选重复，尚未发送，稍后重新核查')

    def event(self, text):
        self.message = text
        self.events.insert(0, {'at': time.time(), 'text': text})
        self.events = self.events[:60]

    def stop(self, message='已停止。再次启动会先核查所选视频的未回复评论'):
        getattr(self,'external_drafts',{}).clear()
        self.running = False
        if getattr(self,'discovery',None):self.discovery.running=False
        if getattr(self,'discovery_task',None):self.discovery_task.cancel()
        if getattr(self,'history_reader',None):self.history_reader.running=False
        if getattr(self,'history_task',None):self.history_task.cancel()
        self.clear_prepared()
        self.followups = []
        self.phase = 'stopped'
        self.epoch += 1
        self.next_check = 0
        self.event(message)

    async def browser(self):
        if self.running:
            raise ValueError('请先停止监听再切换浏览器')
        if self.page and not self.page.is_closed():
            await self.page.bring_to_front()
            return
        if self.context:
            await self.context.close()
        if not self.pw:
            self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(str(DATA / 'edge-profile'),
            executable_path=str(EDGE), headless=False, chromium_sandbox=True,
            viewport={'width': 1280, 'height': 850})
        self.context.on('response', self.schedule_read)
        await self.context.route('**/*', self.guard_post)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto(CREATOR, wait_until='domcontentloaded', timeout=45000)
        self.event('请在 Edge 手动登录，在评论管理中选择自己的视频；登录验证由你操作')

    def schedule_read(self, response):
        for d in (getattr(self,'discovery',None),getattr(self,'history_reader',None),getattr(self,'verifier',None),getattr(self,'targeted_reader',None)):
            if d and d.page is not None and request_page(response.request) is d.page:return
        task = asyncio.create_task(self.read_response(response))
        self.reader_tasks.add(task)
        task.add_done_callback(self.reader_tasks.discard)

    async def guard_post(self, route):
        req = route.request
        # Binary telemetry/media POSTs are not form data. Never let their
        # decoding error escape into Playwright's shared connection callback.
        fields = {}
        if platform_host(req.url) and req.method == 'POST':
            try:
                fields = request_fields(req)
            except NonTextRequestBody:
                path = urlparse(req.url).path.lower()
                if 'comment' in path and any(x in path for x in ('reply', 'publish', 'create')):
                    if self.pending:
                        self.pending['blocked'] = True
                    self.stop('评论请求正文无法核实，已拦截并停止；不会重试')
                    await route.abort()
                else:
                    await route.continue_()
                return
        readonly=any(d and d.page is not None and request_page(req) is d.page for d in (getattr(self,'discovery',None),getattr(self,'history_reader',None),getattr(self,'verifier',None),getattr(self,'targeted_reader',None)))
        if readonly and platform_host(req.url) and req.method=='POST':
            path=urlparse(req.url).path.lower()
            if 'comment' in path and any(x in path for x in ('reply','publish','create')):
                if '/list/' in path and not any(k in fields for k in ('text','content')):
                    await route.continue_();return
                self.stop('只读发现页出现发送请求，已拦截并停止')
                await route.abort();return
        if self.pending and platform_host(req.url) and req.method == 'POST':
            p = self.pending
            possible_send = ('comment' in req.url.lower() and any(x in req.url.lower() for x in ('reply', 'publish', 'create')))
            possible_send = possible_send or fields.get('text') == p['reply'] or fields.get('content') == p['reply']
            if possible_send:
                nested = fields.get('reply_to_reply_id')
                cid = nested if nested and nested != '0' else (fields.get('reply_id') or fields.get('comment_id') or fields.get('cid'))
                creator_target=fields.get('reply_to_comment_ids')
                if creator_target is not None:
                    # Creator UI carries one literal ID, despite the plural field name.
                    # Never accept a list/batch or conflicting target aliases.
                    cid=creator_target if re.fullmatch(r'\d{15,25}',creator_target) and (not cid or cid==creator_target) else None
                vid = fields.get('aweme_id') or fields.get('item_id')
                text = fields.get('text') or fields.get('content')
                muted = hasattr(self,'store') and self.store.discussion(p['comment']['video'],p['comment'].get('root',p['comment']['id']))['muted']
                if muted or not self.running or p['epoch'] != self.epoch or cid != p['comment']['id'] or vid != self.selected or text != p['reply'] or p['dispatched']:
                    p['blocked'] = True
                    self.stop('发送请求的目标无法准确验证，已拦截并停止；不会重试')
                    await route.abort()
                    return
                p['dispatched'] = True
                p['request'] = req
        await route.continue_()

    async def read_response(self, response):
        if not platform_host(response.url):
            return
        try:
            if response.status in (403, 429) and 'comment' in response.url.lower():
                self.stop('评论页面返回访问限制，已停止，请到 Edge 查看；不会绕过或重试')
                return
            if 'json' not in response.headers.get('content-type', ''):
                return
            body = await response.body()
            if len(body) > 3000000:
                return
            payload = json.loads(body)
            identity=self_account(response.url,payload)
            if identity:
                owner=getattr(self,'owner',self)
                previous=getattr(owner,'current_identity',None)
                if previous and previous['uid']!=identity['uid'] and owner.running:
                    owner.stop('登录账号发生变化，请停止后重新核对账号与作品')
                    return
                if not owner.running and not owner.pending:
                    owner.current_identity=identity
                    owner.store.db.execute('INSERT OR IGNORE INTO own_authors VALUES(?)',(identity['uid'],))
                    owner.store.db.commit()

            # A video id explicitly carried by this comment request scopes its response.
            # Never borrow the selected id merely because the UI currently shows it.
            query = parse_qs(urlparse(response.url).query)
            post = request_fields(response.request)
            hint = ''
            if 'comment' in urlparse(response.url).path.lower():
                ids = {str(v) for k in ('aweme_id','item_id') for v in query.get(k,[]) if re.fullmatch(r'\d{15,25}',str(v))}
                ids.update(str(post[k]) for k in ('aweme_id','item_id') if re.fullmatch(r'\d{15,25}',str(post.get(k,''))))
                if len(ids)==1:hint=ids.pop()
                if not hint:
                    # Reply-list requests identify a known root, not necessarily a video.
                    parents={str(v) for k in ('comment_id','cid','reply_id') for v in query.get(k,[])}
                    parents.update(post[k] for k in ('comment_id','cid','reply_id') if k in post)
                    videos={c['video'] for c in self.comments.values() if c['id'] in parents and c['parent']=='0'}
                    if len(videos)==1:hint=videos.pop()
                self.response_shapes.insert(0,dict(path=urlparse(response.url).path,status=response.status,keys=list(payload)[:16] if isinstance(payload,dict) else [],video_hint=hint))
                self.response_shapes=self.response_shapes[:8]
            comments = extract_comments(payload,hint)
            if hint and '/comment/list/' in response.url and '/reply/' not in response.url and isinstance(payload,dict):
                raw=payload.get('comments')
                valid=(payload.get('status_code')==0 and (isinstance(raw,list) or raw is None and payload.get('total')==0))
                if valid and (not raw or comments):
                    self.video_reads[hint]={'at':time.time(),'total':payload.get('total'),'count':len(comments)}
                    self.observed=time.time()
                    self.root_snapshot=dict(video=hint,at=time.time(),comments=comments,has_more=payload.get('has_more'),total=payload.get('total'),expected_ids=[str(r.get('cid') or r.get('comment_id')) if isinstance(r,dict) and (r.get('cid') or r.get('comment_id')) else None for r in raw or []])
            if self.coverage and hint==self.coverage.video and request_page(response.request) is self.page:
                valid_list=isinstance(payload,dict) and payload.get('status_code')==0 and (isinstance(payload.get('comments'),list) or payload.get('comments') is None and payload.get('total')==0)
                if valid_list:
                    expected=[str(r.get('cid') or r.get('comment_id')) if isinstance(r,dict) and (r.get('cid') or r.get('comment_id')) else None for r in payload.get('comments') or []]
                    roots={str(v) for k in ('comment_id','cid','reply_id') for v in query.get(k,[])}
                    roots.update(str(post[k]) for k in ('comment_id','cid','reply_id') if k in post)
                    known_roots={c['root'] for c in self.comments.values() if c['id'] in roots and c['video']==hint}
                    reply_root=next(iter(known_roots)) if '/reply/' in response.url and len(known_roots)==1 else None
                    cursor_values=query.get('cursor',[])
                    request_cursor=post.get('cursor') if 'cursor' in post else (cursor_values[0] if len(cursor_values)==1 else None)
                    self.coverage.observe(comments,root_list='/comment/list/' in response.url and '/reply/' not in response.url and not self.search_text,has_more=payload.get('has_more'),total=payload.get('total'),expected_ids=expected,reply_root=reply_root,request_cursor=request_cursor,next_cursor=payload.get('cursor'))
            if comments:
                self.store.observe(comments)
                if request_page(response.request) is self.page:
                    self.loaded_roots.update(c['id'] for c in comments if c['parent']=='0')
                for d in (getattr(self,'discovery',None),getattr(self,'history_reader',None)):
                    if d and d.coverage:
                        d.coverage.observe([c for c in comments if c['video']==self.selected and c['parent']=='0' and c['id'] in d.coverage.comments])
                self.observed = time.time()
                for c in comments:
                    self.comments[(c['video'], c['id'])] = c
                    if self.running and c['video']==self.selected and c['created']>self.started and c['author'] not in self.store.own_authors() and asks_to_stop(c['text']):
                        self.store.update_discussion(c['video'],c.get('root',c['id']),muted=1)
                if len(self.comments) > 3000:
                    self.comments = dict(list(self.comments.items())[-2000:])
            p = self.pending
            if p and p.get('request') == response.request:
                p['received'] = True
                p['business_status']=payload.get('status_code') if isinstance(payload,dict) else None
                p['business_message']=str(payload.get('status_msg',''))[:240] if isinstance(payload,dict) else ''
                # A status message alone is not proof of a reply to the intended parent.
                confirmed = [c for c in comments if c['video'] == self.selected and c['parent'] == p['comment']['id'] and c['text'] == p['reply']]
                p['confirmed'] = len(confirmed) == 1
                self.record_send_receipt(p,response.status,comments)
                if p['confirmed']:
                    self.store.remember_own(confirmed[0])
            return comments
        except Exception:
            # Missing/changed structures never become sendable comments.
            return

    def record_send_receipt(self,p,http_status,comments):
        # Local diagnostic evidence only; a 200/status=0 is never send proof.
        # Keep scoped parsed comments and business status, never cookies/headers.
        db=self.store.db
        db.execute('CREATE TABLE IF NOT EXISTS send_receipts(video TEXT,cid TEXT,part INTEGER,created REAL,http_status INTEGER,business_status TEXT,message TEXT,comments TEXT,confirmed INTEGER)')
        rows=[{k:c.get(k) for k in ('id','video','parent','root','author','text','created')} for c in comments if c['video']==self.selected]
        db.execute('INSERT INTO send_receipts VALUES(?,?,?,?,?,?,?,?,?)',(self.selected,p['comment']['id'],p.get('part',1),time.time(),http_status,str(p.get('business_status')),p.get('business_message',''),json.dumps(rows,ensure_ascii=False),int(p['confirmed'])))
        db.commit()

    async def healthy(self):
        was_running=self.running;epoch=self.epoch
        for attempt in range(3):
            if was_running and (not self.running or self.epoch!=epoch):
                raise ValueError('已停止页面检查')
            if not self.page or self.page.is_closed():
                raise ValueError('Edge 窗口已关闭，请重新连接')
            if urlparse(self.page.url).hostname != 'creator.douyin.com':
                raise ValueError('请保持在抖音创作者中心的评论管理页')
            selectors = '[class*="captcha"]:visible, [id*="captcha"]:visible, iframe[src*="captcha"]:visible'
            if await self.page.locator(selectors).count():
                raise ValueError('发现验证界面，服务停止，请手动处理后重新启动')
            try:
                text = await self.page.locator('body').inner_text(timeout=5000)
            except PlaywrightTimeoutError:
                if attempt==2:
                    raise PageNotReady('页面正文连续3次读取超时，已暂停；未自动重发，请检查 Edge 页面') from None
                self.event('页面暂时未加载，重查页面状态（%s/3）；不重发评论' % (attempt+1))
                await asyncio.sleep(.5)
                continue
            for warning in ('操作频繁', '访问频繁', '请求频繁', '行为异常', '安全验证', '扫码登录', '登录已过期'):
                if warning in text:
                    raise ValueError('页面提示“%s”，服务已停止' % warning)
            return

    async def inspect_video(self, selected):
        if not self.context:raise ValueError('请先点击登录抖音，使用工具打开的 Edge')
        await self.close_discovery()
        matches=[]
        for page in self.context.pages:
            if page.is_closed():continue
            try:
                if video_id(page.url)==selected:matches.append(page)
            except ValueError:pass
        if len(matches)==1:self.page=matches[0]
        elif len(matches)>1:raise ValueError('同一作品打开了多个标签页，请只保留一个再试')
        else:
            self.page=await self.context.new_page()
            await self.page.goto('https://creator.douyin.com/creator-micro/work-management/work-detail/'+selected+'?tabKey=commentManagement',wait_until='domcontentloaded',timeout=30000)
        await self.healthy()
        before=time.time()
        await self.page.reload(wait_until='domcontentloaded',timeout=30000)
        # Await passive comment responses triggered by that ordinary page refresh.
        if not await self.wait_video_read(selected,before):
            raise ValueError('未收到该视频的有效评论列表，不能确认是空评论还是加载失败，请检查 Edge 页面')
        count=sum(c['video']==selected for c in self.comments.values())
        if count:await self.probe_ui()
        else:self.ui_probe={'comment_rows':0,'first_reply_buttons':0}
        self.event('已识别作品详情标签页，读到 %s 条评论；尚未启动监听' % count)
        return count

    async def wait_video_read(self,selected,since,epoch=None,timeout=10,local_only=False):
        # Await the response from this ordinary refresh, without another request.
        deadline=time.monotonic()+timeout
        while True:
            if epoch is not None and (not self.running or self.epoch!=epoch):return False
            stamp=(self.root_snapshot or {}).get('at',0) if local_only else self.video_reads.get(selected,{}).get('at',0)
            if stamp>=since:return True
            remaining=deadline-time.monotonic()
            if remaining<=0:return False
            await asyncio.sleep(min(.25,remaining))

    async def verify_unknown(self, selected, cid, active_epoch=None):
        # Read-only, stopped-service repair: never infer failure from absence.
        if active_epoch is None:
            if self.running:raise ValueError('运行中不执行暂停发送核查')
        elif not self.running or self.epoch!=active_epoch or not self.pending or self.pending['comment']['id']!=cid:
            return False
        if not self.context:raise ValueError('请先连接浏览器')
        if self.selected and self.selected!=selected:raise ValueError('不能更换核查视频')
        db=self.store.db
        if not db.execute("SELECT 1 FROM sends WHERE video=? AND cid=? AND status='uncertain'",(selected,cid)).fetchone():
            raise ValueError('不是待核实发送')
        rows=db.execute('SELECT video,cid,author,name,text,created,parent,root FROM conversation WHERE video=? AND (cid=? OR cid=(SELECT root FROM conversation WHERE video=? AND cid=?))',(selected,cid,selected,cid)).fetchall()
        probe=Watcher(store=self.store);probe.selected=selected;probe.running=True
        probe.context=self.context;probe.coverage=ScanCoverage(selected)
        probe.comments={(r[0],r[1]):dict(zip(('video','id','author','name','text','created','parent','root'),r)) for r in rows}
        c=probe.comments[(selected,cid)];root=probe.comments[(selected,c['root'])]
        self.verifier=probe
        try:
            probe.page=await self.context.new_page()
            probe.page.on('response',probe.schedule_read)
            since=time.time()
            await probe.page.goto('https://creator.douyin.com/creator-micro/work-management/work-detail/'+selected+'?tabKey=commentManagement',wait_until='domcontentloaded',timeout=30000)
            await probe.wait_video_read(selected,since,timeout=8,local_only=True)
            await probe.search_root(root)
            row=await probe.find_comment_row(root)
            await asyncio.wait_for(probe.load_visible_thread(row,probe.epoch),15)
            if probe.reader_tasks:await asyncio.wait(list(probe.reader_tasks),timeout=2)
            if active_epoch is not None:
                if not self.running or self.epoch!=active_epoch or not probe.running:return False
                await self.healthy()
                await probe.healthy()
                if not self.running or self.epoch!=active_epoch:return False
                if self.pending and self.pending.get('confirmed'):return True
                if not self.pending or self.pending.get('blocked'):return False
            resolved=ReplyLedger(self.store).resolve_observed_unknowns(selected,only_cid=cid)
            if cid in resolved:
                if self.pending and self.pending['comment']['id']==cid:
                    if active_epoch is None:
                        self.pending=None;self.sent_this_run+=1
                    else:self.pending['confirmed']=True
                self.event('已核实原作者回复，未重新发送：'+cid)
            else:self.event('仍未找到唯一对应的作者回复，保留未知，禁止重发：'+cid)
            return cid in resolved
        finally:
            probe.running=False
            for task in list(probe.reader_tasks):task.cancel()
            try:
                if probe.page:await asyncio.wait_for(probe.page.close(),3)
            finally:self.verifier=None

    async def reconcile_pending_receipt(self, p, epoch, click_timed_out=False):
        # One bounded read-only attempt; no new publication or global restart.
        if p.get('confirmed'):return True
        reserved=self.store.db.execute("SELECT 1 FROM sends WHERE video=? AND cid=? AND part=? AND status='uncertain'",(self.selected,p['comment']['id'],p['part'])).fetchone() if click_timed_out else None
        if not self.running or self.epoch!=epoch or self.pending is not p or not (p.get('dispatched') or click_timed_out and reserved) or p.get('blocked'):
            return False
        self.event('回执暂未确认，正在只读核实原回复；不会重新发送')
        try:
            await asyncio.wait_for(self.verify_unknown(self.selected,p['comment']['id'],active_epoch=epoch),35)
        except (TimeoutError,ValueError,PlaywrightTimeoutError) as exc:
            self.event('只读回执复核未完成，保留未知：'+str(exc)[:120])
        except PlaywrightError as exc:
            # Only transport failures of the read-only probe may reach the
            # existing guarded hold path. Browser closure and other errors stop.
            if not any(code in str(exc) for code in ('net::ERR_CONNECTION_CLOSED','net::ERR_CONNECTION_RESET','net::ERR_TIMED_OUT')):raise
            self.event('只读核实连接中断，保留未知并进入原安全检查：'+str(exc)[:120])
        return bool(self.running and self.epoch==epoch and self.pending is p and p.get('confirmed') and not p.get('blocked'))

    def comment_rows(self):
        # CSS-module prefix observed in the creator work-detail comment page.
        return self.page.locator('[class^="comment-item-"], [class*=" comment-item-"]')

    async def probe_ui(self):
        rows=self.comment_rows()
        try:await rows.first.wait_for(state='visible',timeout=5000)
        except Exception:pass
        self.ui_probe={'comment_rows':await rows.count()}
        self.ui_probe['first_reply_buttons']=await rows.first.get_by_role('button',name='回复',exact=True).count() if await rows.count() else 0

    async def find_comment_row(self,c):
        matched=await self.find_comment_rows([c])
        if c['id'] not in matched:raise ValueError('无法唯一定位目标评论，需核查，未猜测发送')
        return matched[c['id']]

    async def find_comment_rows(self,comments,within=None):
        if not comments:return {}
        rows=(within.locator('[class^="comment-item-"], [class*=" comment-item-"]') if within is not None else self.comment_rows())
        await rows.first.wait_for(state='visible',timeout=1000)
        expected=[]
        for c in comments:
            parent=getattr(self,'comments',{}).get((c.get('video'),c.get('parent')))
            expected.append(dict(id=c['id'],name=c['name'].strip(),text=c['text'].strip(),prefix=('回复 '+parent['name']+'：') if parent else None))
        # One browser round trip for the entire batch, not one per historical
        # root. Normalize line-break whitespace but never weaken unique matching.
        matches=await rows.evaluate_all(r"""(rows,expected)=>{
         const norm=s=>s.replace(/\s+/g,' ').trim(),lookup=new Map();
         rows.forEach((row,index)=>{
          if(!row.getClientRects().length||getComputedStyle(row).visibility==='hidden')return [];
          const bodies=row.querySelectorAll(':scope > [class^="comment-main-"]');if(bodies.length!==1)return [];
          const texts=bodies[0].querySelectorAll(':scope > [class^="text-"]'),names=bodies[0].querySelectorAll(':scope > [class^="username-"]');
          if(texts.length!==1||names.length!==1)return [];
          const node=texts[0].cloneNode(true),prefixes=[...node.querySelectorAll(':scope > [class^="reply-to-"]')].map(e=>e.textContent);
          node.querySelectorAll(':scope > [class^="reply-to-"]').forEach(e=>e.remove());
          node.querySelectorAll('img.text-emoji-pCH4HE, img[class^="text-emoji-"]').forEach(e=>e.replaceWith(e.alt));
          node.querySelectorAll('br').forEach(e=>e.replaceWith('\n'));
          const key=JSON.stringify([norm(names[0].innerText.split(/\r?\n/)[0]),norm(node.textContent)]);
          if(!lookup.has(key))lookup.set(key,[]);lookup.get(key).push({index,prefixes});
         });
         return expected.map(e=>{
          const indices=(lookup.get(JSON.stringify([norm(e.name),norm(e.text)]))||[])
           .filter(r=>!r.prefixes.length||(r.prefixes.length===1&&r.prefixes[0]===e.prefix)).map(r=>r.index);
          let position=null;
          if(indices.length===1){const el=rows[indices[0]];let p=el.parentElement;
           while(p&&p!==document.body){if(/auto|scroll/.test(getComputedStyle(p).overflowY)&&p.scrollHeight>p.clientHeight+2)break;p=p.parentElement;}
           p=p&&p!==document.body?p:document.scrollingElement;
           position=Math.max(0,p.scrollTop+el.getBoundingClientRect().top-(p===document.scrollingElement?0:p.getBoundingClientRect().top)-40);}
          return {id:e.id,indices,position};
         });
        }""",expected)
        roots={c['id'] for c in comments if c.get('parent')=='0'}
        for m in matches:
            if m['id'] in roots and len(m['indices'])==1 and m.get('position') is not None:
                self.root_positions[(comments[0]['video'],m['id'])]=m['position']
        return {m['id']:rows.nth(m['indices'][0]) for m in matches if len(m['indices'])==1}

    async def expand_replies(self,selected):
        # Normal visible UI expansion only. Bound work per cycle; rotate threads fairly.
        roots=[c for c in self.comments.values() if c['video']==selected and c['parent']=='0' and c.get('reply_count',0)>0]
        roots.sort(key=lambda c:c['created'])
        if not roots:return
        start=self.fold_cursor%len(roots)
        batch=(roots[start:]+roots[:start])[:5]
        self.fold_cursor=(start+len(batch))%len(roots)
        for c in batch:
            await self.healthy()
            try:row=await self.find_comment_row(c)
            except (ValueError,TimeoutError,PlaywrightTimeoutError):continue
            control=row.locator(':scope > [class^="comment-main-"]')
            expand=control.get_by_role('button',name=re.compile(r'^查看\s*\d+\s*条回复(?:\s+chevron_down)?$'))
            if await expand.count()==1:
                await expand.click(timeout=5000)
                await asyncio.sleep(1)
        self.ui_probe['folded_threads_checked']=len(batch)

    async def reveal_visible_comment(self,c):
        root=c if c['parent']=='0' else self.comments.get((c['video'],c['root']))
        if not root:raise ValueError('楼中楼根评论尚未核实，未发送')
        # Refresh all visible reply pages even for a root target: a manual author
        # reply may have arrived after the startup audit or while drafting.
        row=await self.find_comment_row(root)
        await self.load_visible_thread(row,self.epoch)
        if self.reader_tasks:await asyncio.wait(list(self.reader_tasks),timeout=1)
        if self.coverage and c['id'] not in self.verified_comment_ids():
            raise ThreadPending(self.coverage.reason(c) or '讨论串数据仍待核查')
        if c['parent']!='0':
            scoped=await self.find_comment_rows([c],within=row)
            if c['id'] in scoped:return scoped[c['id']]
        return await self.find_comment_row(c)

    async def load_visible_thread(self,row,epoch):
        for _ in range(100):
            if not self.running or self.epoch!=epoch:raise ValueError('已停止读取折叠回复')
            controls=row.get_by_role('button',name=re.compile(r'^(?:查看\s*\d+\s*条回复|展开更多回复|查看更多回复)(?:\s+chevron_down)?$'))
            count=await controls.count()
            if count==0:return
            if count!=1:raise ValueError('折叠回复入口不唯一，需核查')
            await self.healthy()
            await controls.click(timeout=5000)
            if not await self.pause_scan(epoch):raise ValueError('已停止读取折叠回复')
        raise ValueError('折叠回复尚未读完，暂停等待核查，不标记全部完成')

    async def pause_scan(self,epoch,seconds=1):
        for _ in range(int(seconds*4)):
            if not self.running or self.epoch!=epoch:return False
            await asyncio.sleep(.25)
        if self.reader_tasks:await asyncio.wait(list(self.reader_tasks),timeout=1)
        return self.running and self.epoch==epoch

    async def scroll_comments(self,top=False):
        rows=self.comment_rows()
        if not await rows.count():return False
        # Scroll only the actual ancestor containing comment rows; do not operate unrelated panels.
        return await rows.first.evaluate("""(el,top)=>{let p=el.parentElement;while(p&&p!==document.body){if(/auto|scroll/.test(getComputedStyle(p).overflowY)&&p.scrollHeight>p.clientHeight+2)break;p=p.parentElement;}p=p&&p!==document.body?p:document.scrollingElement;const old=p.scrollTop;p.scrollTop=top?0:old+Math.max(200,p.clientHeight*.85);return p.scrollTop!==old;}""",top)

    async def expand_history_threads(self,epoch):
        now=time.time()
        for key in list(self.history_failed):
            if now>=self.history_failed_until.get(key,0):self.history_failed.discard(key)
        roots=[r for r in self.coverage.missing_threads() if r['id'] not in self.history_failed]
        try:matched=await self.find_comment_rows(roots)
        except (ValueError,TimeoutError,PlaywrightTimeoutError):return False
        checked=0
        for root in roots:
            if not self.running or self.epoch!=epoch:return
            row=matched.get(root['id'])
            if row is None:continue
            checked+=1
            if checked>8:return False
            for _ in range(50):
                if root['id'] not in {c['id'] for c in self.coverage.missing_threads()}:break
                controls=row.get_by_role('button',name=re.compile(r'^(?:查看\s*\d+\s*条回复|展开更多回复|查看更多回复)(?:\s+chevron_down)?$'))
                if await controls.count()!=1:
                    self.history_failed.add(root['id'])
                    self.history_failed_until[root['id']]=time.time()+120
                    break
                await self.healthy()
                try:await controls.click(timeout=2000)
                except PlaywrightTimeoutError:
                    await self.healthy()
                    self.history_failed.add(root['id'])
                    self.history_failed_until[root['id']]=time.time()+120
                    self.event('一处折叠回复暂时打不开，保留待核查，继续其他评论')
                    break
                if not await self.pause_scan(epoch):return
                return True  # One expansion per slice; yield to discovery/sending.

    async def history_scroll(self,position=None):
        rows=self.comment_rows()
        if not await rows.count():return 0
        return await rows.first.evaluate("""(el,pos)=>{let p=el.parentElement;while(p&&p!==document.body){if(/auto|scroll/.test(getComputedStyle(p).overflowY)&&p.scrollHeight>p.clientHeight+2)break;p=p.parentElement;}p=p&&p!==document.body?p:document.scrollingElement;if(pos!==null)p.scrollTop=pos;return p.scrollTop;}""",position)

    async def scan_history(self,epoch):
        # Start a cooperative scan, never a blocking startup prerequisite.
        if not self.running or self.epoch!=epoch:return False
        self.coverage=ScanCoverage(self.selected)
        self.history_failed=set()
        snap=self.root_snapshot
        if snap and snap['video']==self.selected and time.time()-snap['at']<15:
            self.coverage.observe(snap['comments'],True,snap['has_more'],snap['total'],snap['expected_ids'])
        self.reconcile_status={'phase':'scanning','complete':False}
        self.history_next=0;self.history_position=0;self.history_steps=0
        self.history_stalled=0;self.history_progress=None
        self.phase='running'
        return True

    async def history_step(self,epoch):
        if not self.running or self.epoch!=epoch or not self.coverage:return
        await self.healthy()
        if video_id(self.page.url)!=self.selected:raise ValueError('核查期间目标视频改变，停止核查')
        await self.history_scroll(self.history_position)
        if not await self.pause_scan(epoch,.5):return
        if self.history_steps%3==0:
            roots=[c for c in self.comments.values() if c['video']==self.selected and c['parent']=='0']
            try:await self.find_comment_rows(roots)
            except (ValueError,TimeoutError,PlaywrightTimeoutError):pass
        expanded=await self.expand_history_threads(epoch)
        if not self.running or self.epoch!=epoch:return
        moved=True if expanded else await self.scroll_comments()
        if not await self.pause_scan(epoch,1):return
        self.history_position=await self.history_scroll()
        self.history_steps+=1
        complete=self.coverage.complete()
        fresh=dict(self.coverage.comments)
        if self.discovery and self.discovery.coverage:
            fresh.update(self.discovery.coverage.comments)
        fresh=list(fresh.values())
        for c in fresh:
            if c['author'] not in self.store.own_authors() and asks_to_stop(c['text']):
                self.store.update_discussion(self.selected,c['root'],muted=1)
        if not self.pending and not getattr(getattr(self,'owner',None),'pending',None):
            resolved=ReplyLedger(self.store).resolve_observed_unknowns(self.selected)
            if resolved:self.event('已从作者原回复核实 %s 条旧未知发送，未重复发送' % len(resolved))
        # The independent reader audits each received thread immediately. Avoid
        # rewriting the entire historical ledger on every small scroll tick.
        if not isinstance(self,HistoryWatcher) or self.history_steps%30==0:
            ReplyLedger(self.store).plan(self.selected,str(self.started)+':'+str(epoch),fresh,False,self.verified_comment_ids(),incremental=isinstance(self,HistoryWatcher),pending_reasons={c['id']:self.coverage.reason(c) for c in fresh})
        progress=(len(self.coverage.comments),len(self.coverage.verified_ids()))
        self.history_stalled=self.history_stalled+1 if progress==self.history_progress and not moved else 0
        self.history_progress=progress
        self.reconcile_status.update(phase='complete' if complete else 'scanning',complete=complete,observed=len(fresh),pages=self.history_steps,**self.coverage.diagnostics())
        if not self.store.own_authors():
            raise ValueError('尚未验证本账号身份，无法核对手动回复；请先完成账号身份确认')
        self.history_next=time.time()+2
        if complete or self.history_stalled>=5:
            self.reconcile_status['phase']='complete' if complete else 'partial'
            self.history_position=0;self.history_stalled=0;self.history_steps=0
            self.history_next=time.time()+60
            self.event(('历史本轮核查完成' if complete else '部分历史尚待核查')+'；新评论监听继续，已核实未回复的评论进入队列')

    async def search_root(self,c,retain_coverage=False):
        # Use only the ordinary keyword field observed in the creator page.
        # Search narrows the DOM; unique row and exact outgoing cid guards remain.
        root=c if c['parent']=='0' else self.comments.get((c['video'],c['root']))
        wanted=comment_search_keyword(root['text']) if root else ''
        if not wanted and not self.search_text:return False
        if self.pending:raise ThreadPending('正在确认发送，不能改变搜索')
        field=self.page.get_by_placeholder('搜索评论关键词',exact=True)
        if await field.count()!=1:return False
        await self.healthy()
        if video_id(self.page.url)!=self.selected:raise ValueError('搜索前目标视频改变')
        if self.search_text!=wanted:
            before=time.time()
            self.search_positions[(self.selected,self.search_text)]=dict(self.locate_positions)
            self.search_text=wanted
            self.loaded_roots.clear();self.locate_positions=dict(self.search_positions.get((self.selected,wanted),{}));self.root_positions.clear()
            # No old full-list evidence is allowed to stand in for the search result.
            if not retain_coverage:self.coverage=ScanCoverage(self.selected)
            await field.fill(wanted,timeout=2000)
            await field.press('Enter',timeout=2000)
            if not await self.wait_video_read(self.selected,before,self.epoch,timeout=5,local_only=True):
                raise ThreadPending('关键词查找尚未收到有效列表，稍后核查')
        return bool(wanted)

    async def reveal_comment(self,c):
        try:
            if c['root'] in self.loaded_roots and (c['video'],c['root']) in self.root_positions:
                await self.healthy()
                if video_id(self.page.url)!=c['video']:raise ValueError('目标视频改变，未发送')
                await self.history_scroll(self.root_positions[(c['video'],c['root'])])
                try:
                    return await asyncio.wait_for(self.reveal_visible_comment(c),timeout=2)
                except ThreadPending:raise
                except (ValueError,TimeoutError,PlaywrightTimeoutError):pass
            # Old roots otherwise get lost every time a new-comment reload clears
            # the virtual list. Search their exact root text before bounded locate.
            if getattr(self,'history_reader',None) and (time.time()-c['created']>300 or self.search_text):
                if await self.search_root(c):
                    return await asyncio.wait_for(self.locate_comment(c),timeout=self.locate_budget)
            # A read-only discovery can see a root absent from the sender DOM.
            # Refresh only before typing/reserving a send, and at most every 30s.
            if self.discovery and c['root'] not in self.loaded_roots and time.time()-self.last_discovery_sync>=30:
                if self.pending:raise ThreadPending('正在确认发送，不同步回复页面')
                await self.healthy()
                if video_id(self.page.url)!=self.selected:raise ValueError('目标视频改变，未同步页面')
                self.last_discovery_sync=time.time()
                self.loaded_roots.clear()
                self.locate_positions.clear()
                self.root_positions.clear()
                await self.page.reload(wait_until='domcontentloaded',timeout=10000)
                await self.wait_video_read(self.selected,self.last_discovery_sync,self.epoch,timeout=5)
            return await asyncio.wait_for(self.locate_comment(c),timeout=self.locate_budget)
        except asyncio.TimeoutError:
            raise ThreadPending('单条定位已用满8秒预算，保留检查位置后再查') from None

    async def locate_comment(self,c):
        epoch=self.epoch
        key=(c['video'],c['id'])
        saved=self.locate_positions.get(key,self.root_positions.get((c['video'],c['root'])))
        if saved is not None:await self.history_scroll(saved)
        for step in range(6):
            if not self.running or self.epoch!=epoch:raise ValueError('已停止定位评论')
            await self.healthy()
            if video_id(self.page.url)!=c['video']:raise ValueError('目标视频改变，未发送')
            try:
                row=await self.reveal_visible_comment(c)
                self.locate_positions.pop(key,None)
                return row
            except ThreadPending:raise
            except (ValueError,TimeoutError,PlaywrightTimeoutError):pass
            moved=await self.scroll_comments(top=step==0 and saved is None)
            self.locate_positions[key]=await self.history_scroll()
            if step and not moved:
                self.locate_positions.pop(key,None)
                break
            if not await self.pause_scan(epoch):break
        # The last scroll can load the target; inspect it before reporting failure.
        if self.running and self.epoch==epoch:
            await self.healthy()
            if video_id(self.page.url)!=c['video']:raise ValueError('目标视频改变，未发送')
            try:
                row=await self.reveal_visible_comment(c)
                self.locate_positions.pop(key,None)
                return row
            except ThreadPending:raise
            except (ValueError,TimeoutError,PlaywrightTimeoutError):pass
        raise ThreadPending('本次有限定位未找到唯一目标，保留检查位置后再查')

    async def detect_identity(self):
        await self.browser()
        self.current_identity=None
        page=await self.context.new_page()
        try:
            await page.goto('https://www.douyin.com/user/self',wait_until='domcontentloaded',timeout=30000)
            for _ in range(20):
                if getattr(self,'current_identity',None):return dict(identity=self.current_identity)
                await asyncio.sleep(.5)
            raise ValueError('尚未读取到登录账号，请先在专用浏览器登录抖音，再点自动识别；也可使用手动身份确认')
        finally:await page.close()

    async def start(self, values):
        if self.running:
            raise ValueError('服务已经在运行')
        if self.pending:
            raise ValueError('上一次发送仍待核实，不能带着旧发送状态直接重启；请先核实或按账号主人明确授权处理')
        if self.task and not self.task.done():
            raise ValueError('上一轮正在收尾，请稍后再启动')
        if values.get('authorized') is not True:
            raise ValueError('请确认这是自己的视频，并授权补回本视频未回复的历史评论并回复新评论')
        selected = video_id(values.get('video', ''))
        resume=None
        checkpoint=DATA/'maintenance-resume.json'
        if values.get('resume_maintenance') is True:
            resume=json.loads(checkpoint.read_text('utf-8'))
            now=time.time()
            if resume.get('video')!=selected or resume.get('reason')!='empty-list-upgrade' or not 0<=now-resume.get('created',0)<3600 or not 0<resume.get('started',0)<=now or not 0<=resume.get('sent',-1) or trial_caps_enabled() and resume.get('sent',0)>=10:
                raise ValueError('维修恢复记录无效，请手动开始新一轮监听')
            validate_resume_quarantine(self.store.db,resume)
            self.owner_retry=validate_owner_retry(self.store.db,resume)
        self.send_interval=settings.active['send_interval'];self.poll_interval=settings.active['poll_interval']
        self.phase='starting'
        self.event('正在核对视频并建立监听起点…')
        if not self.store.own_authors():
            await self.detect_identity()
        await self.inspect_video(selected)
        await self.healthy()
        cfg = model_config()
        if not cfg.get('agent_key'):
            raise ValueError('未找到 评论助手 模型配置，请在电脑客户端保存模型服务')
        await self.expand_replies(selected)
        if not self.store.own_authors():
            raise ValueError('首次使用请先点击账号身份确认，选择一条你本人已发布的评论并确认；不需要测试发评论')
        for c in self.store.history(selected,limit=40):
            self.comments.setdefault((c['video'],c['id']),c)
        self.selected = selected
        # Platform times have second precision: skip the ambiguous current second.
        self.started = resume['started'] if resume else time.time()
        self.facts = str(values.get('facts', ''))[:6000]
        self.style = str(values.get('style', '自然、友好、简短'))[:300]
        self.sent_this_run = resume['sent'] if resume else 0
        self.followups.clear()
        self.clear_prepared()
        self.locate_positions.clear()
        self.search_positions.clear()
        self.running = True
        self.phase='reconciling'
        self.reconcile_status={'phase':'scanning','complete':False}
        self.epoch += 1
        self.priority_turn=0
        self.event('监听已启动：独立检查新增评论，历史分段补漏；按配置间隔发送，不重复已回复评论')
        self.task = asyncio.create_task(self.loop(self.epoch))
        if resume:
            checkpoint.unlink()
            self.event('修复更新完成，已恢复原起点继续监听新评论')

    def defer_locate(self,c):
        key=(c['video'],c['id'])
        failures=self.locate_backoff.get(key,(0,0))[0]+1
        delay=min(600,60*2**min(failures-1,4))
        self.locate_backoff[key]=(failures,time.time()+delay)
        return delay

    def locate_ready(self,c):
        return time.time()>=self.locate_backoff.get((c['video'],c['id']),(0,0))[1]

    def candidates(self,ignore_model_backoff=False):
        cooling=time.time()<self.model_retry_at
        if cooling and not ignore_model_backoff and not self.external_drafts:return []
        queued=ReplyLedger(self.store).pending_comments(self.selected,limit=10000,started=self.started)
        for c in queued:
            self.comments[(c['video'],c['id'])]=c
            for parent_id in (c['root'],c['parent']):
                rows=self.store.db.execute('SELECT video,cid,author,name,text,created,parent,root FROM conversation WHERE video=? AND cid=?',(self.selected,parent_id)).fetchall()
                for row in rows:
                    parent=dict(zip(('video','id','author','name','text','created','parent','root'),row))
                    self.comments[(parent['video'],parent['id'])]=parent
        verified=self.verified_comment_ids() if self.coverage or self.discovery else None
        queued=[c for c in queued if self.locate_ready(c) and (verified is None or c['id'] in verified) and self.store.new_candidate(c,self.selected,self.started,time.time())]
        held={r[0] for r in self.store.db.execute("SELECT DISTINCT c.root FROM conversation c JOIN sends s ON s.video=c.video AND s.cid=c.cid WHERE c.video=? AND s.status='uncertain'",(self.selected,))}
        queued=[c for c in queued if c['root'] not in held]
        if cooling and not ignore_model_backoff:queued=[c for c in queued if c['id'] in self.external_drafts]
        # Three recent comments, then one oldest: neither new arrivals nor backlog starve.
        if self.priority_turn%4!=3:
            cutoff=time.time()-300
            queued.sort(key=lambda c:(c['created']<cutoff,c['created'],c['id']))
        return queued

    async def refresh_comments(self,epoch):
        await self.healthy()
        before=time.time()
        await self.page.reload(wait_until='domcontentloaded',timeout=30000)
        self.last_reload=time.time()
        fresh=await self.wait_video_read(self.selected,before,epoch)
        if not self.running or self.epoch!=epoch:return False
        await self.healthy()
        if not fresh:
            self.event('本次刷新未收到有效列表，30秒后再检查；不依据过期页面发帖')
            return False
        await self.expand_replies(self.selected)
        return self.running and self.epoch==epoch

    async def loop(self, epoch):
        try:
            if self.context:
                self.discovery=DiscoveryWatcher(self)
                self.discovery_task=asyncio.create_task(self.discovery.run(epoch))
                self.history_reader=HistoryWatcher(self)
                self.history_task=asyncio.create_task(self.history_reader.run(epoch))
                await self.send_queue_loop(epoch)
                return
            first=True
            resume_scan=False
            while self.running and self.epoch==epoch:
                if not resume_scan:
                    if not await self.scan_history(epoch):return
                    snap=self.root_snapshot
                    fresh_start=first and snap and snap['video']==self.selected and time.time()-snap['at']<15
                    first=False
                    if not fresh_start and not await self.refresh_comments(epoch):
                        self.next_check=time.time()+self.poll_interval
                        while self.running and self.epoch==epoch and time.time()<self.next_check:
                            if not await self.pause_scan(epoch,1):return
                        continue
                resume_scan=False
                self.next_check=0
                # Keep the pass cursor and coverage across send batches. Once a
                # few scan slices produce verified work, drain it immediately;
                # resume scanning from the saved position without another reload.
                slices=0
                while self.running and self.epoch==epoch and self.reconcile_status['phase']=='scanning':
                    try:await self.history_step(epoch)
                    except PlaywrightTimeoutError:
                        await self.healthy()
                        self.reconcile_status['phase']='partial'
                        self.event('本轮一处历史页面读取超时，先处理已核实的待回复队列')
                    slices+=1
                    if slices>=3 and (self.candidates() or self.history_steps and self.history_steps%100==0):
                        resume_scan=self.reconcile_status['phase']=='scanning'
                        break
                self.next_check=0
                while self.running and self.epoch==epoch:
                    candidates=self.candidates()
                    if not candidates and not self.followups:break
                    if candidates:self.prepare_batch(candidates,epoch)
                    due=self.last_send+self.send_interval
                    if time.time()<due:
                        if not await self.pause_scan(epoch,min(1,due-time.time())):return
                        continue
                    if trial_caps_enabled() and (self.sent_this_run>=10 or self.store.attempts_since(time.time()-3600)>=10):
                        raise ValueError('已达到配置的试用发送上限，服务停止')
                    if candidates:
                        await self.process_comment(candidates[0],epoch)
                        self.priority_turn+=1
                    else:await self.send_followup(epoch)
                if not self.running or self.epoch!=epoch:return
                if resume_scan:
                    self.event('继续处理剩余历史：保留扫描位置，不从头重复遍历')
                    continue
                self.next_check=time.time()+self.poll_interval
                self.event('本轮可回复队列已处理完，30秒后刷新检查；待核查项不会阻塞监听')
                while self.running and self.epoch==epoch and time.time()<self.next_check:
                    # Passive arrivals can be handled promptly without a forced reload.
                    if self.candidates():break
                    if not await self.pause_scan(epoch,min(1,self.next_check-time.time())):return
        except asyncio.CancelledError:
            self.stop('服务已退出')
        except Exception as exc:
            self.stop(str(exc)[:240])
        finally:
            self.clear_prepared()
            await self.close_discovery()

    async def send_queue_loop(self,epoch):
        # History owns a separate persistent DOM. Neither new arrivals nor a
        # constantly nonempty send queue can reset or starve its scan cursor.
        self.phase='running'
        job=self.owner_retry;self.owner_retry=None
        if job:
            row=self.store.db.execute('SELECT video,cid,author,name,text,created,parent,root FROM conversation WHERE video=? AND cid=?',(job['video'],job['cid'])).fetchone()
            if not row:raise ValueError('指定补发原评论不存在，未猜测发送')
            c=dict(zip(('video','id','author','name','text','created','parent','root'),row))
            while self.running and self.epoch==epoch and time.time()<self.last_send+self.send_interval:await asyncio.sleep(.25)
            if self.running and self.epoch==epoch:
                self.event('处理账号主人明确指定的一次补发，先核对原评论；原未知记录保留')
                await self.send(c,job['reply'],epoch,100,owner_requested=True)
        while self.running and self.epoch==epoch:
            candidates=self.candidates()
            if candidates:self.prepare_batch(candidates,epoch)
            if time.time()<self.last_send+self.send_interval or not (candidates or self.followups):
                await asyncio.sleep(.25)
                continue
            if trial_caps_enabled() and (self.sent_this_run>=10 or self.store.attempts_since(time.time()-3600)>=10):
                raise ValueError('已达到配置的试用发送上限，服务停止')
            if candidates:
                await self.process_comment(candidates[0],epoch)
                self.priority_turn+=1
            else:await self.send_followup(epoch)

    async def send_followup(self,epoch):
        if not self.followups:return
        job=self.followups.pop(0);c=job['comment']
        if not self.running or self.epoch!=epoch or job['epoch']!=epoch:return
        # Optional second parts expire rather than answering a stale conversation.
        if time.time()-job['created']>300:return
        if self.store.discussion(c['video'],c['root'])['muted']:return
        if self.store.db.execute('SELECT 1 FROM sends WHERE video=? AND cid=? AND part=2',(c['video'],c['id'])).fetchone():return
        if await self.send(c,job['reply'],epoch,2):
            self.store.update_discussion(c['video'],c['root'],last_reply=time.time())

    async def process_comment(self,c,epoch):
        try:
            return await self._process_comment(c,epoch)
        finally:
            self.reply_signatures.pop(c['id'],None)
            key=(c['video'],c['id'])
            row=self.store.db.execute('SELECT status FROM items WHERE video=? AND cid=?',key).fetchone()
            if (row and row[0]=='drafting' and not self.pending and
                not self.store.db.execute('SELECT 1 FROM sends WHERE video=? AND cid=?',key).fetchone()):
                self.store.mark(c,'awaiting_thread','生成或页面检查中断，尚未进入发送；待重新核查')

    async def _process_comment(self,c,epoch):
        answered=ReplyLedger(self.store).answered(c)
        if answered:
            self.store.mark(c,'replied_external',answered);return
        history=self.store.history(c['video'],root=c['root'],before=c['created'],limit=24)
        for key in (c['root'],c['parent']):
            parent=self.comments.get((c['video'],key))
            if parent and all(x['id']!=key for x in history):history.append(parent)
        root, context = thread_context(c, history)
        discussion = self.store.discussion(c['video'],root)
        if discussion['muted']:
            self.store.mark(c,'skipped','该讨论串已要求停止插话');return
        if c['parent']!='0' and (not context or not self.store.own_authors()):
            self.store.mark(c,'skipped','楼中楼上下文或本账号身份尚未验证，不猜测回复');return
        draft_started=time.monotonic()
        self.store.mark(c, 'drafting', c['name'] + '：' + c['text'])
        self.event('处理待回复评论，正在组织接话：' + c['text'][:60])
        try:
            d = await self.take_draft(c,epoch)
            self.last_draft_seconds=round(time.monotonic()-draft_started,3)
            self.reply_signatures[c['id']]=self.draft_inputs(c)[2]
        except ThreadPending as exc:
            self.store.mark(c,'awaiting_thread',str(exc));return
        except (ModelUnavailable,requests.Timeout,requests.ConnectionError):
            self.store.mark(c,'skipped','模型暂时连接失败，尚未发送，稍后重新生成')
            self.event('模型暂不可用，该条保留补回；退避结束自动继续，评论抓取不停止')
            return
        if not self.running or self.epoch != epoch:
            self.store.mark(c, 'cancelled', '已停止，未发送');return
        if d['action']=='withdraw':
            self.store.update_discussion(c['video'],root,muted=1)
        if d['action']!='reply':
            reason=d.get('validation_error') or ('对方要求停止回复，本讨论串不再自动介入' if d['action']=='withdraw' else '模型未能明确理解评论含义，主动跳过，未发送' if d['tone']=='unclear' else '模型判断本条不适合回复，主动跳过，未发送')
            reason+='（判断：'+str(d['tone'])+'；把握程度：'+str(d['confidence'])+'）'
            self.store.mark(c,'skipped',reason);self.event(reason);return
        if d['intent']=='mediate':
            people={x['author'] for x in context if x['author'] not in self.store.own_authors()}
            if len(people)<2 or discussion['mediated']:
                self.store.mark(c,'skipped','不把单句当争吵，或本串已劝过一次');return
            # Reserve before sending; uncertain outcomes never trigger repeated mediation.
            self.store.update_discussion(c['video'],root,mediated=1)
        # Each verified unanswered comment stays eligible; a previous
        # reply elsewhere in the thread must not silently drop it.
        if not self.running or self.epoch!=epoch:return
        if self.store.discussion(c['video'],root)['muted']:return
        self.prepare_batch(self.candidates(),epoch,current=c)
        if await self.send(c,d['replies'][0],epoch,1):
            self.store.update_discussion(c['video'],root,last_reply=time.time())
            if len(d['replies'])>1 and self.running and self.epoch==epoch:
                self.followups.append(dict(comment=c,reply=d['replies'][1],epoch=epoch,created=time.time()))
                self.event('第一句已回复；先回复其他评论，空闲时再接第二句')

    async def send(self, c, reply, epoch, part=1,owner_requested=False):
        if self.pending:raise ValueError('上条发送尚未核实，禁止并发发送')
        if not owner_requested and self.store.db.execute("SELECT 1 FROM sends s JOIN conversation t ON t.video=s.video AND t.cid=s.cid WHERE t.video=? AND t.root=? AND s.status='uncertain'",(c['video'],c.get('root',c['id']))).fetchone():
            self.event('同串存在未知发送，冻结该串；其他评论继续')
            return False
        await self.healthy()
        if repeated_reply([reply],[r['reply'] for r in self.store.recent_replies(c['video'],limit=80)]):
            if part==1:self.store.mark(c,'skipped','发送前发现本视频已说过相同或高度相似的话')
            self.event('这句之前已经说过，本次不重复发送')
            return False
        # Both author and exact comment text must exist in exactly one comment row.
        try:
            locate_started=time.monotonic()
            row=await self.reveal_comment(c)
            locate_seconds=round(time.monotonic()-locate_started,3)
        except (ValueError,TimeoutError,PlaywrightTimeoutError) as exc:
            # Platform/captcha failures still stop globally; a local unreadable
            # thread must not block unrelated comments. No send was reserved.
            await self.healthy()
            detail=(str(exc)[:180] if isinstance(exc,ThreadPending) else '页面未能唯一定位该评论或展开入口，等待下轮核查')
            delay=self.defer_locate(c)
            detail+=f'；{delay}秒后再定位'
            if part==1:self.store.mark(c, 'awaiting_thread', detail+'；尚未尝试发送')
            self.event(detail+'；其他评论继续处理')
            return False
        self.locate_backoff.pop((c['video'],c['id']),None)
        if part==1 or owner_requested:
            answered=ReplyLedger(self.store).answered(c)
            if answered:
                self.store.mark(c,'replied_external',answered)
                self.event('发现该评论已有作者回复，不重复发送')
                return False
        signature=self.reply_signatures.get(c['id']) if part==1 else None
        if signature and signature!=self.draft_inputs(c)[2]:
            self.store.mark(c,'awaiting_thread','定位期间收到新对话，尚未发送；重新核实上下文后再回复')
            return False
        surface=row.locator(':scope > [class^="comment-main-"] > [class^="comment-operation-wrap-"]')
        button = surface.get_by_role('button', name='回复', exact=True)
        if await button.count() != 1:
            raise ValueError('无法唯一定位该评论的回复按钮')
        p = self.pending = dict(comment=c, reply=reply, part=part,epoch=epoch, dispatched=False, blocked=False, confirmed=False)
        try:
            await button.click(timeout=5000)
            inputs = surface.locator('[contenteditable="true"]:visible, textarea:visible')
            if await inputs.count() != 1:
                raise ValueError('回复输入框不唯一，已停止，不会输入到其他位置')
            await inputs.fill(reply, timeout=5000)
            send = surface.get_by_role('button', name='发送', exact=True)
            if await send.count() != 1:
                # Do not choose an arbitrary global Send button.
                raise ValueError('未找到评论内唯一发送按钮，草稿留在 Edge，未发送')
            if not self.running or self.epoch != epoch:
                return
            root = c.get('root', c['id'])
            if self.store.discussion(c['video'],root)['muted']:return
            self.store.reserve_send(c,part)
            self.store.mark(c, 'uncertain', '准备发送，若中断不自动重发：' + reply)
            self.last_send = time.time()
            receipt_started=time.monotonic()
            click_timed_out=False
            try:
                await send.click(timeout=5000)
            except PlaywrightTimeoutError:
                click_timed_out=True
                self.event('发送点击超时，保留预留并只读核实；不会再次点击发送')
            for _ in range(20):
                if p['confirmed'] or p['blocked']:
                    break
                await asyncio.sleep(0.5)
            if not p['confirmed']:
                await self.reconcile_pending_receipt(p,epoch,click_timed_out=click_timed_out)
            if not p['confirmed']:
                if await self.hold_unknown_and_continue(p,epoch):return False
                if not p['confirmed']:raise ValueError('发送结果尚未验证，已暂停；请到 Edge 检查，不会自动重发')
            self.store.confirm_send(c,part)
            self.store.remember_reply(c,part,reply)
            self.store.mark(c, 'sent', c['text'] + ' → ' + reply)
            self.sent_this_run += 1
            self.unknown_streak = 0
            self.timings.append(dict(cid=c['id'],at=time.time(),draft_seconds=getattr(self,'last_draft_seconds',None) if part==1 else None,locate_seconds=locate_seconds,receipt_seconds=round(time.monotonic()-receipt_started,3)))
            self.timings=self.timings[-40:]
            self.event('已验证回复成功：' + reply)
            return True
        except (ValueError,PlaywrightTimeoutError):
            # A failure opening/filling the composer is not an uncertain send.
            # The durable reservation always precedes the Send click. Only when
            # both reservation and dispatch are absent may we clear the draft.
            reserved=self.store.db.execute('SELECT 1 FROM sends WHERE video=? AND cid=? AND part=?',(c['video'],c['id'],part)).fetchone()
            if reserved or p['dispatched'] or p['blocked']:raise
            await self.healthy()
            # Keep the guard armed against any unexpected late publish while
            # an ordinary refresh discards the unsent composer contents.
            p['epoch']=-1
            # Discarding a composer requires a committed new document, not all
            # page scripts reaching DOMContentLoaded (which can stall). Keep
            # the publish guard armed throughout these read-only retries.
            for attempt in range(3):
                if not self.running or self.epoch!=epoch:
                    raise ValueError('已停止，不继续清理未发送草稿')
                if p['dispatched'] or p['blocked'] or self.store.db.execute('SELECT 1 FROM sends WHERE video=? AND cid=? AND part=?',(c['video'],c['id'],part)).fetchone():
                    raise ValueError('清理期间出现发送证据，保留待核实，不重试')
                try:
                    await self.page.reload(wait_until='commit',timeout=10000)
                    break
                except PlaywrightTimeoutError:
                    await self.healthy()
                    if video_id(self.page.url)!=self.selected:
                        raise ValueError('清理未发送草稿时视频改变，已停止')
                    if attempt==2:raise
                    await asyncio.sleep(.5)
            await self.healthy()
            if video_id(self.page.url)!=self.selected:raise ValueError('清理未发送草稿时视频改变，已停止')
            if not self.running or self.epoch!=epoch:raise ValueError('已停止，保留未发送草稿核查状态')
            if p['dispatched'] or p['blocked'] or self.store.db.execute('SELECT 1 FROM sends WHERE video=? AND cid=? AND part=?',(c['video'],c['id'],part)).fetchone():
                raise ValueError('清理期间出现发送证据，保留待核实，不重试')
            self.pending=None;self.search_text='';self.loaded_roots.clear()
            self.coverage=ScanCoverage(self.selected)
            self.store.mark(c,'awaiting_thread','回复输入入口暂时失效，未点击发送；已清理未发送草稿，稍后重新定位')
            self.event('一条评论回复入口暂时失效，尚未发送；稍后重查，其他评论继续')
            return False
        finally:
            # Keep the guard armed after any uncertain send, to block late retries.
            if p['confirmed']:
                self.pending = None

    async def hold_unknown_and_continue(self,p,epoch):
        # Explicit owner policy: freeze the durable reservation; never retry it.
        try:enabled=json.loads((DATA/'owner-policy.json').read_text('utf-8')).get('auto_hold_unknown') is True
        except (OSError,ValueError):enabled=False
        if not enabled or p.get('blocked') or p.get('business_status') not in (None,0,'0'):return False
        if self.unknown_streak>=2:return False
        c=p['comment'];part=p.get('part',1)
        if not self.store.db.execute("SELECT 1 FROM sends WHERE video=? AND cid=? AND part=? AND status='uncertain'",(c['video'],c['id'],part)).fetchone():return False
        await self.healthy()
        if not self.running or self.epoch!=epoch or self.pending is not p or video_id(self.page.url)!=c['video']:return False
        p['epoch']=-1  # Block every late publish while the composer is retired.
        for attempt in range(3):
            try:
                await self.page.reload(wait_until='commit',timeout=10000)
                break
            except PlaywrightTimeoutError:
                await self.healthy()
                if not self.running or self.epoch!=epoch or p.get('blocked') or video_id(self.page.url)!=c['video']:return False
                if attempt==2:return False
        await self.healthy()
        if not self.running or self.epoch!=epoch or self.pending is not p or p.get('blocked') or video_id(self.page.url)!=c['video']:return False
        if p.get('confirmed'):return False  # Late positive receipt uses the normal once-only finalizer.
        if p.get('business_status') not in (None,0,'0'):return False
        self.store.mark(c,'uncertain','回执未确认，自动冻结本条及讨论串，禁止重发：'+p['reply'])
        self.clear_prepared()
        self.followups=[j for j in self.followups if j['comment']['root']!=c['root']]
        self.pending=None;self.search_text='';self.loaded_roots.clear();self.root_positions.clear();self.locate_positions.clear()
        self.coverage=ScanCoverage(self.selected)
        self.unknown_streak+=1
        self.event('回执仍未知，已冻结 '+c['id']+' 及同串，禁止重发；其他评论继续。连续第三条未知将停止发送')
        return True

    def state(self):
        cfg = model_config()
        videos = {}
        for c in self.comments.values():
            videos[c['video']] = videos.get(c['video'], 0) + 1
        return dict(account_identity=getattr(self,"current_identity",None),model_retry_at=self.model_retry_at,model_failures=self.model_failures,unknown_streak=self.unknown_streak,running=self.running, started=self.started, video=self.selected, message=self.message,
                    next_check=self.next_check, observed=self.observed, videos=videos,
                    model_ready=bool(cfg.get('agent_key')), model=cfg.get('agent_model', ''),
                    events=self.events, records=self.store.recent(), sent=self.sent_this_run, persona_version=VERSION,
                    pages=[p.url for p in self.context.pages if not p.is_closed()] if self.context else [],
                    response_shapes=self.response_shapes, ui_probe=self.ui_probe, phase=self.phase,
                    loop_task={'done':self.task.done(),'stack':[f.f_code.co_name for f in self.task.get_stack(limit=3)]} if self.task else None,
                    video_reads=self.video_reads, worker_version='1.1.0',trial_caps_enabled=trial_caps_enabled(),
                    reconciliation=dict(self.history_reader.reconcile_status if self.history_reader else self.reconcile_status,**ReplyLedger(self.store).summary(self.selected)),
                    reply_settings={'facts':self.facts,'style':self.style},pending_send=bool(self.pending),
                    pending_send_detail=({k:self.pending.get(k) for k in ('dispatched','received','confirmed','blocked','business_status','business_message')}|{'cid':self.pending['comment']['id']}) if self.pending else None,
                    queue=ReplyLedger(self.store).queue_state(self.selected,self.started),
                    prepared_drafts=len(self.prepared_jobs),model_calls=len(self.model_jobs),stage_timings=self.timings,
                    optional_followups=len(self.followups),poll_interval=self.poll_interval,send_interval=self.send_interval,
                    discovery=self.discovery.status if self.discovery else {'phase':'idle'},
                    history_reader=self.history_reader.status if self.history_reader else {'phase':'idle'})


class DiscoveryWatcher(Watcher):
    """Ordinary read-only tab. No model calls, private API requests, or sends."""
    def __init__(self,owner):
        super().__init__(store=owner.store)
        self.owner=owner
        self.context=owner.context
        self.selected=owner.selected
        self.started=owner.started
        self.epoch=owner.epoch
        self.running=True
        self.comments=owner.comments
        self.status=dict(phase='starting',last_read=0,next_check=0,cycles=0,changed=0)
        self.signatures={}

    def event(self,text):
        self.status['message']=text

    def stop(self,message='新增检查已停止'):
        self.running=False
        self.owner.stop(message)

    async def send(self,*args,**kwargs):
        raise RuntimeError('只读发现页禁止发送')

    async def navigate_readonly(self,url,first,epoch):
        # Read-only navigation failures are local only after health/scope checks. Never send.
        for attempt in range(3):
            if not self.running or not self.owner.running or self.owner.epoch!=epoch:return False
            try:
                if first:
                    await self.page.goto(url,wait_until='domcontentloaded',timeout=30000)
                else:
                    await self.healthy()
                    if video_id(self.page.url)!=self.selected:raise ValueError('只读检查页目标改变')
                    await self.page.reload(wait_until='domcontentloaded',timeout=30000)
                await self.healthy()
                if video_id(self.page.url)!=self.selected:raise ValueError('只读检查页目标改变')
                self.status['navigation_retries']=0
                return True
            except PlaywrightError as exc:
                if not isinstance(exc,PlaywrightTimeoutError) and not any(code in str(exc) for code in ('net::ERR_CONNECTION_CLOSED','net::ERR_CONNECTION_RESET','net::ERR_TIMED_OUT')):raise
                if not self.running or not self.owner.running or self.owner.epoch!=epoch:return False
                await self.healthy()
                if video_id(self.page.url)!=self.selected:raise ValueError('只读检查页超时且目标未核实')
                self.status['navigation_retries']=attempt+1
                if not self.running or not self.owner.running or self.owner.epoch!=epoch:return False
                if attempt==2:
                    self.status.update(phase='retrying',message='只读检查页本轮连接失败，稍后补查；其他已核实评论继续处理')
                    return None  # Unverified page: skip traversal, keep owner alive.
                self.status.update(phase='retrying',message='只读页面连接失败，30秒后重试；已核实评论继续处理')
                for _ in range(60):
                    if not self.running or not self.owner.running or self.owner.epoch!=epoch:return False
                    await asyncio.sleep(.5)
        return False

    async def read_response(self,response):
        if request_page(response.request) is not self.page:return
        if not self.running or not self.owner.running or self.epoch!=self.owner.epoch:return
        incoming=await super().read_response(response)
        if not self.running or not self.owner.running:return
        if incoming is None:return
        # Shared observer data remains available even after either cache is trimmed.
        self.owner.comments.update(self.comments)
        self.owner.observed=max(self.owner.observed,self.observed)
        self.owner.video_reads.update(self.video_reads)
        if self.coverage:
            # Changed reply totals invalidate stale terminal proof in the sender.
            if self.owner.coverage:
                roots=[c for c in incoming or [] if c['parent']=='0' and c['video']==self.selected]
                self.owner.coverage.observe(roots)
            other=self.owner.history_reader if self is self.owner.discovery else self.owner.discovery
            if other and other.coverage:
                other.coverage.observe([c for c in incoming or [] if c['parent']=='0' and c['video']==self.selected and c['id'] in other.coverage.comments])
            touched={c['root'] for c in incoming if c['video']==self.selected}
            rows=[c for c in self.coverage.comments.values() if c['root'] in touched]
            changed=0
            for c in rows:
                signature=(c['text'],c.get('reply_count',0),c['parent'],c['root'])
                if self.signatures.get(c['id'])!=signature:
                    changed+=1;self.signatures[c['id']]=signature
            self.status['changed']+=changed
            self.status['last_read']=self.observed
            # Only this view's verified subset is audited. Never demote unrelated queues.
            verified=self.coverage.verified_ids()
            safe=[c for c in rows if c['id'] in verified]
            ReplyLedger(self.store).plan(self.selected,str(self.started)+':'+str(self.epoch),safe,False,verified,incremental=True)

    async def run(self,epoch):
        try:
            self.page=await self.context.new_page()
            self.page.on('response',self.schedule_read)
            url='https://creator.douyin.com/creator-micro/work-management/work-detail/'+self.selected+'?tabKey=commentManagement'
            first=True
            while self.running and self.owner.running and self.owner.epoch==epoch:
                self.coverage=ScanCoverage(self.selected)
                self.history_failed=set()
                self.status['phase']='checking'
                before=time.time()
                navigated=await self.navigate_readonly(url,first,epoch)
                if navigated is False:break
                if navigated is None:
                    for _ in range(60):
                        if not self.running or not self.owner.running or self.owner.epoch!=epoch:return
                        await asyncio.sleep(.5)
                    continue
                first=False
                if await self.wait_video_read(self.selected,before,epoch):
                    # Bounded folded-thread work; never monopolize the sender page.
                    try:
                        async def expand():
                            for _ in range(3):
                                if not await self.expand_history_threads(epoch):break
                        await asyncio.wait_for(expand(),8)
                    except (asyncio.TimeoutError,PlaywrightTimeoutError):
                        self.status['message']='局部折叠回复未读完，保留历史补查'
                    self.status['cycles']+=1
                else:self.status['message']='本轮未收到有效列表，未假定没有新评论'
                self.status.update(phase='waiting',next_check=time.time()+30)
                while self.running and self.owner.running and self.owner.epoch==epoch and time.time()<self.status['next_check']:
                    await asyncio.sleep(.5)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.stop('新增检查页异常：'+str(exc)[:180])
        finally:
            self.running=False
            self.status['phase']='stopped'


class HistoryWatcher(DiscoveryWatcher):
    """Persistent, read-only historical traversal, independent of sender reloads."""
    def backlog_roots(self,limit=24):
        # Durable queued rows may lack fresh coverage after a restart. Include
        # them in oldest-first read-only verification, not just failed rows.
        # Do not retry sends, muted discussions, or previously answered comments.
        ReplyLedger(self.store)
        rows=self.store.db.execute("""SELECT r.video,r.cid,r.author,r.name,r.text,r.created,r.parent,r.root
            FROM conversation c JOIN conversation r ON r.video=c.video AND r.cid=c.root
            LEFT JOIN items i ON i.video=c.video AND i.cid=c.cid
            LEFT JOIN reply_audits a ON a.video=c.video AND a.cid=c.cid
            WHERE c.video=? AND (i.status IN ('awaiting_thread','backlog_review','skipped','restart_pending','owner_recheck') OR a.status IN ('review','unavailable'))
            AND COALESCE(i.status,'') NOT IN ('sent','replied_external','drafting','uncertain','blocked')
            AND c.author NOT IN (SELECT author FROM own_authors)
            AND NOT EXISTS(SELECT 1 FROM sends s WHERE s.video=c.video AND s.cid=c.cid)
            AND NOT EXISTS(SELECT 1 FROM discussions d WHERE d.video=c.video AND d.root=c.root AND d.muted=1)
            ORDER BY c.created,c.cid""",(self.selected,)).fetchall()
        roots={}
        for row in rows:roots.setdefault(row[1],dict(zip(('video','id','author','name','text','created','parent','root'),row)))
        ordered=list(roots.values())
        cursor=getattr(self,'backlog_cursor',0)%max(1,len(ordered))
        chosen=(ordered[cursor:]+ordered[:cursor])[:limit]
        self.backlog_cursor=cursor+len(chosen)
        return chosen

    async def recheck_backlog(self,epoch,limit=24):
        for root in self.backlog_roots(limit):
            if not self.running or not self.owner.running or self.owner.epoch!=epoch:return
            self.status.update(phase='targeted',target=root['id'])
            try:
                if not await self.search_root(root,retain_coverage=True):continue
                # Search result alone does not establish no prior author reply.
                # Read the uniquely matched root's folded replies before enqueue.
                async def inspect():
                    # Search returns only the first page. Read further visible pages
                    # within the existing budget; never certify a missing row.
                    row=await self.locate_comment(root)
                    if self.reader_tasks:await asyncio.wait(list(self.reader_tasks),timeout=1)
                await asyncio.wait_for(inspect(),8)
            except (ThreadPending,PlaywrightTimeoutError,asyncio.TimeoutError) as exc:
                self.status['message']='旧楼层局部读取未完成，保留核查：'+str(exc)[:90]
            except ValueError as exc:
                # Duplicate/missing row is local; health and scope failures are not.
                await self.healthy()
                if video_id(self.page.url)!=self.selected:raise
                self.status['message']='旧楼层暂未唯一定位，保留核查：'+str(exc)[:90]
            self.status['targeted_checked']=self.status.get('targeted_checked',0)+1
        if self.search_text:
            # Restore the unfiltered list without discarding the verified roots
            # already queued by this pass. Search terminal pages never prove the
            # end of the full history list.
            try:
                await self.search_root(dict(text='',parent='0'),retain_coverage=True)
            except (ThreadPending,PlaywrightTimeoutError,asyncio.TimeoutError) as exc:
                # Clearing a search is still a local read. Never let a missing
                # response stop the sender or certify a filtered list complete.
                await self.healthy()
                if video_id(self.page.url)!=self.selected:raise ValueError('历史目标视频改变')
                if not self.running or not self.owner.running or self.owner.epoch!=epoch:return False
                self.status.update(phase='waiting',target='',message='历史搜索复位未核实，保留待查：'+str(exc)[:90])
                return False
        self.status.update(phase='scanning',target='')
        return True

    async def history_step(self,epoch):
        await super().history_step(epoch)
        self.status.update(steps=self.history_steps,position=self.history_position,
                           observed=len(self.coverage.comments),last_step=time.time())

    async def interleave_backlog(self,epoch):
        if not self.running or not self.owner.running or self.owner.epoch!=epoch:return
        if time.monotonic()<getattr(self,'next_targeted_check',0):return
        await self.isolated_backlog(epoch)
        self.next_targeted_check=time.monotonic()+120

    async def isolated_backlog(self,epoch):
        # A search resets the virtual list; restoring scrollTop alone cannot
        # restore its unloaded rows. Keep the traversal DOM completely untouched.
        probe=HistoryWatcher(self.owner)
        probe.coverage=self.coverage
        probe.backlog_cursor=getattr(self,'backlog_cursor',0)
        self.owner.targeted_reader=probe
        try:
            probe.page=await self.context.new_page()
            probe.page.on('response',probe.schedule_read)
            before=time.time()
            if not await probe.navigate_readonly(self.page.url,True,epoch):return
            if await probe.wait_video_read(self.selected,before,epoch):
                await probe.recheck_backlog(epoch,limit=3)
                self.backlog_cursor=probe.backlog_cursor
                self.status['targeted_checked']=self.status.get('targeted_checked',0)+probe.status.get('targeted_checked',0)
        finally:
            probe.running=False
            try:
                if probe.page is not None:await probe.page.close()
            finally:
                tasks=list(probe.reader_tasks)
                for task in tasks:task.cancel()
                if tasks:await asyncio.gather(*tasks,return_exceptions=True)
                self.owner.targeted_reader=None

    async def run(self,epoch):
        failures=0
        while self.owner.running and self.owner.epoch==epoch:
            self.running=True
            try:
                await HistoryWatcher.run_once(self,epoch)
                return
            except PageNotReady:
                # Only the isolated reader may be rebuilt. Sender state is untouched.
                try:
                    await self.owner.healthy()
                    if video_id(self.owner.page.url)!=self.selected:raise ValueError('目标作品改变')
                except Exception as exc:
                    self.stop('主页面无法验证健康，停止自动恢复：'+str(exc));return
                if not self.owner.running or self.owner.epoch!=epoch:return
                failures+=1;delay=30 if failures<=3 else 300
                self.status.update(phase='waiting',message='历史页正文加载超时，'+str(delay)+'秒后自动重建独立检查页；其他已核实评论继续',next_check=time.time()+delay)
                self.owner.event(self.status['message'])
                if self.page is not None:
                    try:await self.page.close()
                    except Exception:pass
                for _ in range(delay*2):
                    if not self.owner.running or self.owner.epoch!=epoch:return
                    await asyncio.sleep(.5)

    async def run_once(self,epoch):
        try:
            self.page=await self.context.new_page()
            self.page.on('response',self.schedule_read)
            url='https://creator.douyin.com/creator-micro/work-management/work-detail/'+self.selected+'?tabKey=commentManagement'
            first=True
            while self.running and self.owner.running and self.owner.epoch==epoch:
                self.search_text=''
                await self.scan_history(epoch)
                before=time.time();self.status['phase']='scanning'
                navigated=await self.navigate_readonly(url,first,epoch)
                if navigated is False:break
                if navigated is None:
                    for _ in range(60):
                        if not self.running or not self.owner.running or self.owner.epoch!=epoch:return
                        await asyncio.sleep(.5)
                    continue
                first=False
                if await self.wait_video_read(self.selected,before,epoch):
                    restored=await self.recheck_backlog(epoch)
                    self.next_targeted_check=time.monotonic()+120
                    while restored is not False and self.running and self.owner.running and self.owner.epoch==epoch and self.reconcile_status['phase']=='scanning':
                        await self.history_step(epoch)
                        if self.reconcile_status['phase']=='scanning':await self.interleave_backlog(epoch)
                    self.status['cycles']+=1
                else:self.status['message']='历史页本轮未收到有效列表，未判定为空'
                self.status.update(phase='waiting',next_check=time.time()+60)
                while self.running and self.owner.running and self.owner.epoch==epoch and time.time()<self.status['next_check']:
                    await asyncio.sleep(.5)
        except asyncio.CancelledError:pass
        except PageNotReady:raise
        except Exception as exc:self.stop('历史检查页异常：'+str(exc)[:180])
        finally:
            self.running=False;self.status['phase']='stopped'


app = FastAPI(docs_url=None, redoc_url=None)
watcher = Watcher()


@app.middleware('http')
async def local_only(request: Request, call_next):
    expected = 'http://127.0.0.1:%s' % PORT
    if request.headers.get('host') != '127.0.0.1:%s' % PORT:
        return JSONResponse({'error': '仅支持本机地址'}, status_code=403)
    if request.url.path.startswith('/api/'):
        if request.headers.get('x-comment-token') != TOKEN or request.headers.get('origin', expected) != expected:
            return JSONResponse({'error': '请重新打开本地页面'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.get('/health')
async def health_identity():return dict(application='comment-assistant',version='1.0.0')

@app.get('/')
async def index():
    return HTMLResponse((ROOT / 'web/index.html').read_text('utf-8').replace('__TOKEN__', TOKEN))


@app.get('/api/state')
async def state():
    return watcher.state()


@app.get('/icon.png')
async def icon():return FileResponse(ROOT/'web/icon.png')

@app.get('/manual')
async def manual():return FileResponse(ROOT/'web/manual.html')

@app.get('/api/resources')
async def resources():
    import psutil
    proc=psutil.Process();backend=proc.memory_info().rss;browser=0;partial=False
    for child in proc.children(recursive=True):
        try:
            if 'msedge' in child.name().lower():browser+=child.memory_info().rss
        except (psutil.NoSuchProcess,psutil.AccessDenied):partial=True
    return dict(backend_bytes=backend,browser_bytes=browser,total_bytes=backend+browser,partial=partial,data_dir=str(DATA))

@app.post('/api/data-location')
async def data_location(request:Request):
    from storage import schedule
    if watcher.running or watcher.pending or watcher.model_jobs or (watcher.task and not watcher.task.done()):
        return JSONResponse({'error':'请先停止监听，等待发送收尾；有待核实发送时不可迁移'},status_code=409)
    try:
        target=schedule(str((await request.json()).get('path','')))
        return dict(message='目录已预约：'+target+'。点击退出软件后重新打开，自动复制记录；原目录保留备份。新目录需重新登录抖音。')
    except ValueError as exc:return JSONResponse({'error':str(exc)},status_code=400)

@app.get('/api/settings')
async def get_settings():
    from providers import usage_summary
    return dict(**settings.view(),usage=usage_summary(settings.active))

@app.get('/api/source/{name}')
async def source(name:str):
    if name not in ('server.py','persona.py','core.py','reconcile.py','settings.py','providers.py','storage.py'):raise HTTPException(404)
    return dict(name=name,content=(ROOT/name).read_text('utf-8'))

@app.post('/api/exit-app')
async def exit_app():
    watcher.stop('用户退出软件')
    async def finish():
        if watcher.task:await asyncio.gather(watcher.task,return_exceptions=True)
        while watcher.model_jobs:await asyncio.sleep(.2)
        await asyncio.sleep(.5)
        hook=globals().get('desktop_exit_hook')
        if hook:hook()
        os._exit(0)
    asyncio.create_task(finish())
    return dict(message='已停止监听，等待发送收尾后退出；未知记录保留')

@app.post('/api/config/{operation}')
async def config_operation(operation:str,request:Request):
    from settings import validate
    from providers import balance,usage_summary
    try:
        if operation=='save':settings.save(await request.json())
        elif operation=='apply':
            if watcher.running or watcher.pending or watcher.model_jobs or (watcher.task and not watcher.task.done()):raise ValueError('请先停止监听并等待发送、模型请求收尾后再应用；保存草稿不影响当前运行')
            settings.apply()
        elif operation=='test':
            if watcher.running or watcher.model_jobs:raise ValueError('请先停止监听再测试，避免与回复争用模型')
            config=validate(await request.json())
            reply=await asyncio.to_thread(generate,config,config['test_prompt'],True)
            return dict(reply=reply[:1000],message='测试完成，仅生成文字，未发布评论、未保存配置')
        elif operation=='models':
            from providers import list_models
            return await asyncio.to_thread(list_models,validate(await request.json()))
        elif operation=='balance':return await asyncio.to_thread(balance,settings.active)
        else:raise ValueError('未知配置操作')
        return dict(**settings.view(),usage=usage_summary(settings.active))
    except Exception as exc:
        # Never return provider response bodies or exception URLs containing keys.
        msg=str(exc) if isinstance(exc,ValueError) else '请求失败，请检查网络、密钥权限与接口配置'
        return JSONResponse({'error':msg[:200]},status_code=400)

@app.post('/api/{action}')
async def action(action: str, request: Request):
    if action == 'stop':
        watcher.stop()
        return watcher.state()
    if watcher.lock.locked():
        raise HTTPException(409, '正在处理上一次操作')
    async with watcher.lock:
        try:
            if action == 'browser':
                await watcher.browser()
            elif action == 'identity-detect':
                if watcher.running or watcher.pending:raise ValueError('请先停止监听并等待发送核实完成')
                return await watcher.detect_identity()
            elif action == 'identity-options':
                if watcher.running or watcher.pending or watcher.model_jobs or (watcher.task and not watcher.task.done()):raise ValueError('请先停止监听并等待发送核实完成')
                selected=video_id(settings.active['video'])
                await watcher.inspect_video(selected)
                await watcher.healthy()
                await watcher.expand_replies(selected)
                rows=watcher.store.history(selected,limit=500)
                return dict(comments=[dict(cid=c['id'],name=c['name'],text=c['text'],author=c['author']) for c in rows],verified=bool(watcher.store.own_authors()))
            elif action == 'identity-confirm':
                if watcher.running or watcher.pending or watcher.model_jobs or (watcher.task and not watcher.task.done()):raise ValueError('请先停止监听并等待发送核实完成')
                values=await request.json()
                if values.get('confirmed') is not True:raise ValueError('必须明确确认所选评论是你本人发布')
                selected=video_id(settings.active['video'])
                await watcher.healthy()
                if video_id(watcher.page.url)!=selected:raise ValueError('当前作品已改变，请重新读取')
                rows=watcher.store.history(selected,limit=500)
                found=[c for c in rows if c['id']==str(values.get('cid')) and c['author']==str(values.get('author'))]
                if len(found)!=1:raise ValueError('评论记录变化，请重新读取')
                watcher.store.db.execute('INSERT OR IGNORE INTO own_authors VALUES(?)',(found[0]['author'],))
                watcher.store.db.commit()
                watcher.event('账号身份已由本机用户确认；历史核查将识别该账号既有回复')
                return dict(message='身份已确认，可以开始监听；未发布任何测试评论')
            elif action == 'start':
                values=await request.json()
                values.update({k:settings.active[k] for k in ('video','facts','style')})
                await watcher.start(values)
            elif action == 'inspect':
                if watcher.running:raise ValueError('请先停止监听再检查页面')
                await watcher.inspect_video(video_id((await request.json()).get('video','')))
            elif action == 'verify-unknown':
                values=await request.json()
                await watcher.verify_unknown(video_id(values.get('video','')),str(values.get('cid','')))
            elif action == 'locate-diagnostic':
                values=await request.json()
                cid=str(values.get('cid',''))
                c=watcher.comments.get((watcher.selected,cid))
                if c is None:
                    row=watcher.store.db.execute('SELECT video,cid,author,name,text,created,parent,root FROM conversation WHERE video=? AND cid=?',(watcher.selected,cid)).fetchone()
                    if row:c=dict(zip(('video','id','author','name','text','created','parent','root'),row))
                if not c or not watcher.running:raise ValueError('当前评论不存在或监听未运行')
                epoch=watcher.epoch
                probe=HistoryWatcher(watcher)
                try:
                    probe.page=await watcher.context.new_page()
                    probe.page.on('response',probe.schedule_read)
                    before=time.time()
                    if not await probe.navigate_readonly(watcher.page.url,True,epoch):raise ValueError('诊断页导航失败')
                    await probe.wait_video_read(watcher.selected,before,epoch)
                    searched=await probe.search_root(c,retain_coverage=True)
                    scan_error=''
                    if searched and values.get('scan') is True:
                        try:await asyncio.wait_for(probe.locate_comment(c),8)
                        except (ThreadPending,PlaywrightTimeoutError,asyncio.TimeoutError) as exc:scan_error=str(exc)
                    await asyncio.sleep(.5)
                    await probe.healthy()
                    if not watcher.running or watcher.epoch!=epoch:raise ValueError('会话已变化')
                    rows=probe.comment_rows()
                    dom=await rows.evaluate_all("rows=>rows.slice(0,30).map(e=>({text:e.innerText,html:e.outerHTML.slice(0,14000)}))")
                    matched=await probe.find_comment_rows([c])
                    return dict(cid=cid,keyword=probe.search_text,searched=searched,matched=cid in matched,scan_error=scan_error,rows=dom,url=probe.page.url)
                finally:
                    probe.running=False
                    if probe.page is not None:await probe.page.close()
                    tasks=list(probe.reader_tasks)
                    for task in tasks:task.cancel()
                    if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            elif action == 'external-queue':
                if not watcher.running:raise ValueError('监听未运行，不提交外部草稿')
                rows=[]
                for c in watcher.candidates(ignore_model_backoff=True)[:12]:
                    context,memory,signature=watcher.draft_inputs(c)
                    rows.append(dict(comment=c,context=context,memory=memory,signature=hashlib.sha256(signature.encode()).hexdigest()))
                return dict(video=watcher.selected,started=watcher.started,epoch=watcher.epoch,rows=rows)
            elif action == 'external-drafts':
                values=await request.json()
                if not watcher.running or values.get('epoch')!=watcher.epoch or values.get('video')!=watcher.selected:raise ValueError('会话已变化')
                rows=values.get('rows',[])
                if not isinstance(rows,list) or len(rows)>12:raise ValueError('每批最多12条')
                eligible_rows={c['id']:c for c in watcher.candidates(ignore_model_backoff=True)}
                accepted=[]
                for row in rows:
                    c=eligible_rows.get(str(row.get('cid')))
                    if not c:continue
                    _,_,signature=watcher.draft_inputs(c)
                    digest=hashlib.sha256(signature.encode()).hexdigest()
                    if row.get('signature')!=digest:continue
                    decision=parse_decision(json.dumps(row.get('decision'),ensure_ascii=False),False)
                    watcher.discard_draft(c['id'])
                    watcher.external_drafts[c['id']]=dict(epoch=watcher.epoch,signature=digest,decision=decision)
                    accepted.append(c['id'])
                return dict(accepted=accepted)
            else:
                raise ValueError('未知操作')
        except Exception as exc:
            if action=='start' and not watcher.running:
                watcher.phase='error'
                watcher.event('启动失败：'+str(exc)[:200])
            return JSONResponse({'error': str(exc)[:240]}, status_code=400)
    return watcher.state()


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning', log_config=None, access_log=False, loop='asyncio')
