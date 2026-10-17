"""Fail-closed new-comment eligibility. No platform requests in this module."""
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse


def video_id(value):
    value = value.strip()
    if re.fullmatch(r"\d{15,25}", value):
        return value
    parsed = urlparse(value)
    if parsed.scheme == 'https' and parsed.hostname == 'creator.douyin.com':
        detail = re.fullmatch(r'/creator-micro/work-management/work-detail/(\d{15,25})/?', parsed.path)
        if detail:return detail[1]
    if parsed.scheme != 'https' or parsed.hostname not in ('www.douyin.com', 'douyin.com'):
        raise ValueError('请填写完整的 https://www.douyin.com/video/数字 链接，不使用分享短链')
    match = re.fullmatch(r'/video/(\d{15,25})/?', parsed.path)
    if not match:
        raise ValueError('没有识别到视频编号，请复制浏览器中的完整视频链接')
    return match[1]


def extract_comments(payload, video_hint=''):
    """Accept only structured comments with ID, video ID, author and real timestamp.
    Never infer missing fields from UI order, nicknames, relative dates or a URL.
    """
    found = {}
    def walk(obj, depth=0):
        if depth > 16:
            return
        if isinstance(obj, dict):
            cid = obj.get('cid') or obj.get('comment_id')
            vid = obj.get('aweme_id') or obj.get('item_id') or video_hint
            user = obj.get('user') or obj.get('user_info') or {}
            text = obj.get('text') or obj.get('content')
            if not text and (obj.get('image_list') or obj.get('sticker')):
                text = '[图片表情]'
            stamp = obj.get('create_time')
            if isinstance(user, dict) and cid and vid and isinstance(text, str) and isinstance(stamp, (int, float)):
                author = user.get('uid') or user.get('user_id') or user.get('sec_uid')
                name = user.get('nickname')
                ts = float(stamp) / (1000 if stamp > 1e12 else 1)
                if author and isinstance(name, str) and 1e9 < ts < 1e11:
                    root = str(obj.get('reply_id') or '0')
                    target = str(obj.get('reply_to_reply_id') or '0')
                    parent = target if target != '0' else root
                    try:reply_count=max(0,int(obj.get('reply_comment_total') or 0))
                    except (ValueError,TypeError,OverflowError):
                        # Keep walking siblings, but never treat an unknown count
                        # as zero and thereby certify this thread as complete.
                        reply_count=None
                    if reply_count is not None:found[str(cid)] = dict(id=str(cid), video=str(vid), author=str(author), name=name,
                        text=text, created=ts, parent=parent, root=root if root!='0' else str(cid),
                        reply_count=reply_count)
            for val in obj.values():
                walk(val, depth + 1)
        elif isinstance(obj, list):
            for val in obj[:1000]:
                walk(val, depth + 1)
    walk(payload)
    return list(found.values())


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute('CREATE TABLE IF NOT EXISTS items (video TEXT, cid TEXT, status TEXT, detail TEXT, updated REAL, PRIMARY KEY(video,cid))')
        self.db.execute('CREATE TABLE IF NOT EXISTS sends (video TEXT, cid TEXT, part INTEGER, status TEXT, updated REAL, PRIMARY KEY(video,cid,part))')
        self.db.execute('CREATE TABLE IF NOT EXISTS discussions (video TEXT, root TEXT, muted INTEGER DEFAULT 0, mediated INTEGER DEFAULT 0, last_reply REAL DEFAULT 0, PRIMARY KEY(video,root))')
        self.db.execute('CREATE TABLE IF NOT EXISTS own_authors (author TEXT PRIMARY KEY)')
        self.db.execute('CREATE TABLE IF NOT EXISTS conversation (video TEXT, cid TEXT, author TEXT, name TEXT, text TEXT, created REAL, parent TEXT, root TEXT, PRIMARY KEY(video,cid))')
        self.db.execute('CREATE INDEX IF NOT EXISTS conversation_person ON conversation(video,author,created)')
        self.db.execute('CREATE TABLE IF NOT EXISTS replies (video TEXT, cid TEXT, part INTEGER, author TEXT, input TEXT, text TEXT, created REAL, PRIMARY KEY(video,cid,part))')
        # Older versions stored only the verified final reply in the item detail.
        # Recover its wording without inventing the missing participant identity.
        for video,cid,detail,stamp in self.db.execute("SELECT video,cid,detail,updated FROM items i WHERE status='sent' AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.video=i.video AND r.cid=i.cid)").fetchall():
            original,separator,reply=detail.rpartition(' → ')
            if separator:
                self.db.execute('INSERT OR IGNORE INTO replies VALUES(?,?,?,?,?,?,?)',(video,cid,0,'',original,reply,stamp))
        self.db.commit()

    def observe(self,comments):
        self.db.executemany('INSERT OR REPLACE INTO conversation VALUES(?,?,?,?,?,?,?,?)',
            [(c['video'],c['id'],c['author'],c['name'],c['text'],c['created'],c['parent'],c['root']) for c in comments])
        self.db.commit()

    def history(self,video,author=None,root=None,before=None,limit=24):
        where='video=?';args=[video]
        if author is not None:where+=' AND author=?';args.append(author)
        if root is not None:where+=' AND (root=? OR cid=?)';args.extend((root,root))
        if before is not None:where+=' AND created<=?';args.append(before)
        args.append(min(40,limit))
        rows=self.db.execute('SELECT video,cid,author,name,text,created,parent,root FROM conversation WHERE '+where+' ORDER BY created DESC,cid DESC LIMIT ?',args).fetchall()
        return [dict(zip(('video','id','author','name','text','created','parent','root'),r)) for r in reversed(rows)]

    def remember_reply(self,c,part,text):
        self.db.execute('INSERT OR REPLACE INTO replies VALUES(?,?,?,?,?,?,?)',(c['video'],c['id'],part,c['author'],c['text'],text,time.time()))
        self.db.commit()

    def recent_replies(self,video,author=None,limit=32):
        args=[video];where='r.video=?'
        if author is not None:
            where+=' AND (r.author=? OR c.author=?)';args.extend((author,author))
        args.append(min(limit,80))
        rows=self.db.execute('SELECT r.cid,r.author,r.input,r.text,r.created FROM replies r LEFT JOIN conversation c ON c.video=r.video AND c.cid=r.cid WHERE '+where+' ORDER BY r.created DESC LIMIT ?',args).fetchall()
        return [dict(zip(('comment_id','author','input','reply','at'),r)) for r in reversed(rows)]

    def seen(self, c):
        return self.db.execute('SELECT status FROM items WHERE video=? AND cid=?', (c['video'], c['id'])).fetchone() is not None

    def mark(self, c, status, detail=''):
        self.db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?)', (c['video'], c['id'], status, detail, time.time()))
        self.db.commit()

    def last_attempt(self):
        old=self.db.execute("SELECT COALESCE(MAX(updated),0) FROM items WHERE status IN ('sent','uncertain')").fetchone()[0]
        new=self.db.execute('SELECT COALESCE(MAX(updated),0) FROM sends').fetchone()[0]
        return max(old,new)

    def attempts_since(self, stamp):
        old=self.db.execute("SELECT COUNT(*) FROM items i WHERE status IN ('sent','uncertain') AND updated>=? AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.video=i.video AND s.cid=i.cid)",(stamp,)).fetchone()[0]
        return old+self.db.execute('SELECT COUNT(*) FROM sends WHERE updated>=?',(stamp,)).fetchone()[0]

    def reserve_send(self,c,part):
        # Each physical comment consumes one attempt, even the second short sentence.
        self.db.execute('INSERT INTO sends VALUES(?,?,?,?,?)',(c['video'],c['id'],part,'uncertain',time.time()))
        self.db.commit()

    def confirm_send(self,c,part):
        self.db.execute("UPDATE sends SET status='sent' WHERE video=? AND cid=? AND part=?",(c['video'],c['id'],part));self.db.commit()

    def remember_own(self,c):
        self.db.execute('INSERT OR IGNORE INTO own_authors VALUES(?)',(c['author'],))
        if all(k in c for k in ('name','root','created','parent')):self.observe([c])
        self.mark(c,'own','助手已确认发出的评论')

    def own_authors(self):
        return {row[0] for row in self.db.execute('SELECT author FROM own_authors')}

    def discussion(self,video,root):
        row=self.db.execute('SELECT muted,mediated,last_reply FROM discussions WHERE video=? AND root=?',(video,root)).fetchone()
        return dict(zip(('muted','mediated','last_reply'),row or (0,0,0)))

    def update_discussion(self,video,root,**values):
        state=self.discussion(video,root);state.update(values)
        self.db.execute('INSERT OR REPLACE INTO discussions VALUES(?,?,?,?,?)',(video,root,state['muted'],state['mediated'],state['last_reply']));self.db.commit()

    def new_candidate(self,c,selected,started,now):
        # Only explicit owner-approved catch-up markers may cross a later restart cutoff.
        approved=self.db.execute("SELECT 1 FROM items i WHERE video=? AND cid=? AND status IN ('owner_recheck','restart_pending') AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.video=i.video AND s.cid=i.cid)",(c['video'],c['id'])).fetchone()
        return c['video']==selected and 0<c['created']<=now+5 and (bool(approved) or started<c['created'] and not self.seen(c)) and c['author'] not in self.own_authors()

    def recent(self):
        rows=self.db.execute("SELECT i.video,i.cid,i.status,i.detail,i.updated,c.name,c.text FROM items i LEFT JOIN conversation c ON c.video=i.video AND c.cid=i.cid WHERE i.status!='old' ORDER BY i.updated DESC LIMIT 80").fetchall()
        result=[]
        for row in rows:
            entry=dict(zip(('video','id','status','detail','updated','name','original'),row))
            replies=self.db.execute("SELECT r.text,s.status,r.part FROM replies r LEFT JOIN sends s ON s.video=r.video AND s.cid=r.cid AND s.part=r.part WHERE r.video=? AND r.cid=? ORDER BY r.part",row[:2]).fetchall()
            entry['replies']=[dict(text=r[0],status=r[1],part=r[2]) for r in replies]
            result.append(entry)
        return result



def eligible(comment, selected, started, now, store):
    return (comment['video'] == selected and comment['parent'] == '0'
            and started < comment['created'] <= now + 5 and not store.seen(comment))


def parse_reply(content):
    clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip())
    data = json.loads(clean)
    if data.get('action') == 'skip':
        return ''
    text = data.get('reply', '')
    if data.get('action') != 'reply' or not isinstance(text, str) or not 1 <= len(text.strip()) <= 100:
        raise ValueError('模型没有返回有效短回复，本条已搁置')
    if re.search(r'https?://|www\.|@', text, re.I):
        raise ValueError('回复含链接或提及，本次试用不发送')
    return text.strip()
