"""Restart reconciliation. Pure local evidence, no browser or model calls."""
import time
from collections import Counter

def recoverable_generation(detail):
    return any(word in (detail or '') for word in ('模型判定被截断','短回复格式不符','选择不插话','相同或高度相似','重写后仍重复','生成暂未得到','接话备选与此前回复重复','模型暂时连接失败'))

class ReplyLedger:
    def __init__(self, store):
        self.store=store
        store.db.execute('CREATE TABLE IF NOT EXISTS reply_audits (video TEXT, cid TEXT, run TEXT, status TEXT, reason TEXT, updated REAL, PRIMARY KEY(video,cid))')
        store.db.commit()

    def resolve_observed_unknowns(self, video, only_cid=None):
        # Positive read evidence only. Absence is never proof of a failed send.
        db=self.store.db;own=self.store.own_authors();resolved=[]
        rows=db.execute("SELECT s.cid,s.part,s.updated,i.detail FROM sends s JOIN items i ON i.video=s.video AND i.cid=s.cid WHERE s.video=? AND s.status='uncertain'",(video,)).fetchall()
        prefix='准备发送，若中断不自动重发：'
        for cid,part,stamp,detail in rows:
            if only_cid is not None and cid!=only_cid:continue
            # An owner-requested later attempt may have identical wording. Its
            # observed child cannot retroactively prove the earlier unknown send.
            if db.execute('SELECT 1 FROM sends WHERE video=? AND cid=? AND part!=? AND updated>?',(video,cid,part,stamp)).fetchone():continue
            if not detail or not detail.startswith(prefix):continue
            text=detail[len(prefix):]
            matches=db.execute('SELECT cid,author FROM conversation WHERE video=? AND parent=? AND text=? AND created>=?',(video,cid,text,stamp-5)).fetchall()
            matches=[r for r in matches if r[1] in own]
            if len(matches)!=1:continue
            original=db.execute('SELECT video,cid,author,name,text,created,parent,root FROM conversation WHERE video=? AND cid=?',(video,cid)).fetchone()
            if not original:continue
            c=dict(zip(('video','id','author','name','text','created','parent','root'),original))
            self.store.confirm_send(c,part);self.store.remember_reply(c,part,text)
            self.store.mark(c,'sent','已核实作者原回复 '+matches[0][0]+'，未重新发送：'+text)
            resolved.append(cid)
        return resolved

    def answered(self, c):
        db=self.store.db;key=(c['video'],c['id'])
        if db.execute("SELECT 1 FROM items WHERE video=? AND cid=? AND status IN ('sent','replied_external')",key).fetchone():return '本地已确认回复'
        if db.execute("SELECT 1 FROM sends WHERE video=? AND cid=? AND status='sent'",key).fetchone():return '已有成功发送回执'
        if db.execute('SELECT 1 FROM replies WHERE video=? AND cid=?',key).fetchone():return '已有确认回复记忆'
        own=self.store.own_authors()
        rows=db.execute('SELECT author FROM conversation WHERE video=? AND parent=?',key).fetchall()
        if any(author in own for (author,) in rows):return '已发现作者针对这条评论的回复'
        return ''

    def plan(self, video, run, fresh_comments, complete, verified_ids=None, incremental=False, pending_reasons=None):
        db=self.store.db;own=self.store.own_authors()
        fresh={c['id']:c for c in fresh_comments if c['video']==video}
        rows=db.execute('SELECT video,cid,author,name,text,created,parent,root FROM conversation WHERE video=? ORDER BY created,cid',(video,)).fetchall()
        keys=('video','id','author','name','text','created','parent','root')
        for row in rows:
            c=dict(zip(keys,row));key=(video,c['id'])
            if incremental and c['id'] not in fresh:continue
            old=db.execute('SELECT status,detail,updated FROM items WHERE video=? AND cid=?',key).fetchone()
            # A background discovery must never replace an active send/draft.
            if incremental and old and old[0] in ('drafting','uncertain'):continue
            prior=db.execute('SELECT run FROM reply_audits WHERE video=? AND cid=?',key).fetchone()
            if c['author'] in own:status,reason='self','本账号评论不自回'
            elif self.answered(c):status,reason='answered',self.answered(c)
            elif db.execute('SELECT 1 FROM sends WHERE video=? AND cid=?',key).fetchone() or old and old[0]=='uncertain':status,reason='review','发送结果待核实，禁止重试'
            elif self.store.discussion(video,c['root'])['muted']:status,reason='muted','对方已要求停止插话'
            elif not own:status,reason='review','未验证本账号身份，无法区分手动回复'
            elif old and old[0] in ('drafting','blocked','skipped','cancelled') and prior and prior[0]==run and not (old[0]=='skipped' and recoverable_generation(old[1]) and time.time()-old[2]>=120):status,reason='review','本轮已处理，保留结果等待核查'
            elif old and old[0]=='awaiting_thread' and time.time()-db.execute('SELECT updated FROM items WHERE video=? AND cid=?',key).fetchone()[0]<120:status,reason='review','目标讨论串待核查，稍后继续；其他评论正常处理'
            elif not complete and c['id'] not in (verified_ids or set()):status,reason='review',(pending_reasons or {}).get(c['id']) or '本条讨论串尚未核查完整；不影响其他评论'
            elif c['id'] not in fresh:status,reason='unavailable','本次页面未读到此评论，需检查删除、隐藏或加载情况'
            elif c['parent']!='0' and (c['root'] not in fresh or c['parent'] not in fresh):status,reason='review','回复对象或根评论未读到，不能猜测关系'
            else:status,reason='queued','本次重启核查未回复，等待按发送间隔补回'
            db.execute('INSERT OR REPLACE INTO reply_audits VALUES(?,?,?,?,?,?)',(video,c['id'],run,status,reason,time.time()))
            if status=='queued':
                db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?)',(video,c['id'],'restart_pending',reason,time.time()))
            elif status=='answered' and not (old and old[0]=='sent'):
                db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?)',(video,c['id'],'replied_external',reason,time.time()))
            elif old and old[0] in ('restart_pending','owner_recheck'):
                db.execute('UPDATE items SET status=?,detail=?,updated=? WHERE video=? AND cid=?',('backlog_review',reason,time.time(),video,c['id']))
        db.commit()
        return self.summary(video)

    def pending_comments(self, video, limit=100, started=None):
        # Do not lose old queued comments when the browser cache evicts its first pages.
        where="(i.status IN ('restart_pending','owner_recheck') OR (i.cid IS NULL AND c.created>?))"
        cutoff=started if started is not None else float('inf')
        rows=self.store.db.execute("SELECT c.video,c.cid,c.author,c.name,c.text,c.created,c.parent,c.root FROM conversation c LEFT JOIN items i ON i.video=c.video AND i.cid=c.cid WHERE c.video=? AND "+where+" AND c.author NOT IN (SELECT author FROM own_authors) AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.video=c.video AND s.cid=c.cid) ORDER BY c.created,c.cid LIMIT ?",(video,cutoff,limit)).fetchall()
        return [dict(zip(('video','id','author','name','text','created','parent','root'),r)) for r in rows]

    def queue_state(self,video,started):
        counts={}
        rows=self.store.db.execute("SELECT CASE WHEN i.status IN ('restart_pending','owner_recheck') THEN 'waiting' WHEN i.cid IS NULL AND c.created>? THEN 'waiting' ELSE i.status END, COUNT(*) FROM conversation c LEFT JOIN items i ON i.video=c.video AND i.cid=c.cid WHERE c.video=? AND c.author NOT IN (SELECT author FROM own_authors) GROUP BY 1",(started,video)).fetchall()
        for status,count in rows:
            if status:counts[status]=count
        return dict(waiting=counts.get('waiting',0),drafting=counts.get('drafting',0),review=sum(counts.get(k,0) for k in ('skipped','blocked','uncertain','backlog_review','cancelled','awaiting_thread')))

    def summary(self, video):
        counts={};items=[]
        rows=self.store.db.execute('SELECT a.cid,a.status,a.reason,i.status,i.detail FROM reply_audits a LEFT JOIN items i ON i.video=a.video AND i.cid=a.cid WHERE a.video=? ORDER BY a.updated,a.cid',(video,)).fetchall()
        for cid,status,reason,current,detail in rows:
            if current in ('sent','replied_external'):status='answered';reason=detail or reason
            elif status=='queued' and current in ('skipped','blocked','uncertain','cancelled','drafting','backlog_review','awaiting_thread'):
                status='review' if current!='drafting' else 'drafting';reason=detail or current
            counts[status]=counts.get(status,0)+1
            if status not in ('answered','self'):items.append(dict(id=cid,status=status,reason=reason))
        return dict(counts=counts,items=items)

class ScanCoverage:
    """Only fresh, video-scoped root/list and reply evidence counts toward coverage."""
    def __init__(self, video):
        self.video=video;self.comments={};self.root_receipts=0;self.terminal=False;self.invalid=False
        self.invalid_roots=set();self.unparsed_ids=set()
        self.reply_pages={}
    def observe(self, comments, root_list=False, has_more=None, total=None, expected_ids=None, reply_root=None, request_cursor=None, next_cursor=None):
        if expected_ids is not None and (None in expected_ids or not set(expected_ids).issubset({c['id'] for c in comments if c['video']==self.video})):
            self.invalid=True
            self.unparsed_ids.update(str(x) for x in set(expected_ids)-{c['id'] for c in comments if c['video']==self.video})
            if reply_root:self.invalid_roots.add(reply_root)
        for c in comments:
            if c['video']==self.video:
                old=self.comments.get(c['id'])
                if old and c['parent']=='0' and old.get('reply_count')!=c.get('reply_count'):
                    self.reply_pages.pop(c['id'],None)
                self.comments[c['id']]=c
        # A terminal reply page is evidence only when the entire cursor chain
        # starts at zero and every expected comment was parsed in this video/thread.
        if reply_root and request_cursor is not None:
            valid=(expected_ids is not None and None not in expected_ids and
                   set(expected_ids).issubset({c['id'] for c in comments if c['video']==self.video and c['root']==reply_root and c['parent']!='0'}))
            self.reply_pages.setdefault(reply_root,{})[str(request_cursor)]=(str(next_cursor) if next_cursor is not None else None,has_more,valid)
        if root_list:
            self.root_receipts+=1
            # Unknown/missing has_more cannot prove the end of a paginated list.
            self.terminal=self.terminal or (has_more in (False,0) if has_more is not None else total==0)
    def thread_terminal(self,root):
        pages=self.reply_pages.get(root,{})
        cursor='0';seen=set()
        while cursor in pages and cursor not in seen:
            seen.add(cursor);nxt,more,valid=pages[cursor]
            if not valid:return False
            if more in (False,0):return True
            if more not in (True,1) or nxt is None:return False
            cursor=nxt
        return False

    def missing_threads(self):
        counts=Counter(c.get('root') for c in self.comments.values() if c['parent']!='0')
        return [c for c in self.comments.values() if c['parent']=='0' and not self.thread_terminal(c['id']) and c.get('reply_count',0)>counts[c['id']]]

    def reason(self,c):
        root=c.get('root',c['id'])
        if root in self.invalid_roots:return '该讨论串含无法解析的评论记录，待核查'
        if c['id'] in self.unparsed_ids:return '这条评论的结构未能完整解析，待核查'
        if c['id'] not in self.comments:return '本轮尚未取得这条评论的有效列表数据'
        if root not in self.comments:return '根评论尚未读到，无法核对回复关系'
        for r in self.missing_threads():
            if r['id']==root:
                n=sum(x['parent']!='0' and x.get('root')==root for x in self.comments.values())
                return '折叠回复已读%s/%s条，尚未取得连续分页的末页数据' % (n,r.get('reply_count',0))
        if c['parent']!='0' and c['parent'] not in self.comments:return '被回复的上级评论尚未读到'
        return ''
    def complete(self):return bool(self.root_receipts and self.terminal and not self.invalid and not self.missing_threads())

    def verified_ids(self):
        # Global pagination need not finish before a fully observed thread is safe.
        missing={c['id'] for c in self.missing_threads()}
        roots={c['id'] for c in self.comments.values() if c['parent']=='0' and c['id'] not in missing|self.invalid_roots|self.unparsed_ids}
        return {c['id'] for c in self.comments.values() if c['root'] in roots and (c['parent']=='0' or c['parent'] in self.comments)}

    def diagnostics(self):
        return dict(root_receipts=self.root_receipts,root_end_seen=self.terminal,parse_incomplete=self.invalid,unparsed_ids=sorted(self.unparsed_ids),invalid_roots=sorted(self.invalid_roots),
                    missing_threads=[dict(id=c['id'],expected=c.get('reply_count',0),observed=sum(x['parent']!='0' and x.get('root')==c['id'] for x in self.comments.values())) for c in self.missing_threads()])
