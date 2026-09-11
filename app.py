#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Loopback-only desktop client. Python 3.7+, standard library only."""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import urllib.request
import urllib.error
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from extract import extract
from protocol import MODEL, DEFAULTS, settings, images, build, sse

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DEEPSEEK_DATA_DIR', str(Path.home() / '.local/share/deepseek-client')))
URL = 'https://api.deepseek.com/chat/completions'
PORT = 8765
CONFIG = {}
SESSIONS = {}
LOCK = threading.Lock()
BUSY = {}
ATTEMPTS = []
SYSTEM = ''
VERSION = '2.0.0'
AGENT = None
AGENT_LOCK = threading.Lock()

def agent_service():
    global AGENT
    from agent.controller import AgentService
    with AGENT_LOCK:
        if AGENT is None or AGENT.store.data.parent != DATA.resolve():
            AGENT = AgentService(DATA, lambda: os.environ.get('DEEPSEEK_API_KEY') or CONFIG.get('api_key'), api_error)
        return AGENT


def token():
    return os.urandom(32).hex()

def db():
    conn = sqlite3.connect(str(DATA / 'history.db'), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(str(DATA), 0o700)
    with db() as c:
        c.executescript('CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY,title TEXT,updated REAL); CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY,conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,role TEXT,content TEXT,created REAL);')
    with db() as c:
        cols={r[1] for r in c.execute('PRAGMA table_info(messages)')}
        if 'metadata' not in cols:
            backup=DATA / 'history-before-v1.1.db'
            if not backup.exists():
                with sqlite3.connect(str(backup)) as dest:
                    c.backup(dest)
                os.chmod(str(backup),0o600)
        for name, default in [('metadata','{}'),('images','[]'),('reasoning','')]:
            if name not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN " + name + " TEXT NOT NULL DEFAULT '"+default+"'")
        if 'settings' not in {r[1] for r in c.execute('PRAGMA table_info(conversations)')}:
            c.execute("ALTER TABLE conversations ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'")
    os.chmod(str(DATA / 'history.db'), 0o600)

def save_config(key,password):
    global CONFIG
    if not isinstance(key,str) or not 1<=len(key.strip())<=512 or any(ord(c)<33 or ord(c)>126 for c in key.strip()):
        raise ValueError('请输入有效 API Key，不能含空格或换行')
    if not isinstance(password,str) or not 8<=len(password)<=1024:
        raise ValueError('本地密码需要 8–1024 个字符')
    salt=token()
    value=dict(api_key=key.strip(),salt=salt,password_hash=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),200000).hex())
    temp=DATA / ('config-'+token()+'.tmp')
    fd=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f)
    os.replace(str(temp),str(DATA/'config.json'))
    CONFIG=value
    SESSIONS.clear()


def setup():
    init()
    key = getpass.getpass('DeepSeek API Key（隐藏输入，留空则运行时读取环境变量）: ').strip()
    password = getpass.getpass('设置本地访问密码（至少 8 位）: ')
    if len(password) < 8 or password != getpass.getpass('再次输入密码: '):
        raise SystemExit('密码太短或两次不一致，配置未保存。')
    salt = token()
    cfg = dict(api_key=key, salt=salt, password_hash=hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200000).hex())
    target = DATA / 'config.json'
    temp = DATA / ('config-' + token() + '.tmp')
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(cfg, f)
    os.replace(str(temp), str(target))
    print('配置完成。运行 bash start.sh 启动。')

class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log messages, credentials, or query strings.

    def send_headers(self, status, kind, length=None, cookie=None):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if length is not None:
            self.send_header('Content-Length', str(length))
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()

    def out(self, obj, status=200, cookie=None):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_headers(status, 'application/json; charset=utf-8', len(raw), cookie)
        self.wfile.write(raw)

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            value = cookie['ds_session'].value
            with LOCK:
                return SESSIONS.get(value, 0) > time.time()
        except (KeyError, ValueError):
            return False

    def guard(self, mutation=False):
        allowed = ['127.0.0.1:' + str(PORT), 'localhost:' + str(PORT)]
        if self.headers.get('Host') not in allowed:
            self.out({'error': '拒绝非本机地址'}, 403)
            return False
        if mutation and (self.headers.get('Origin') not in ['http://' + a for a in allowed] or self.headers.get('X-Requested-With') != 'DeepSeekClient'):
            self.out({'error': '请求来源校验失败'}, 403)
            return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        assets = {'/agent': ('agent.html', 'text/html'), '/static/agent.js': ('static/agent.js', 'text/javascript'), '/static/agent.css': ('static/agent.css', 'text/css'), '/': ('index.html', 'text/html'), '/static/app.js': ('static/app.js', 'text/javascript'), '/static/style.css': ('static/style.css', 'text/css')}
        if self.path in assets:
            name, kind = assets[self.path]
            raw = (ROOT / name).read_bytes()
            self.send_headers(200, kind + '; charset=utf-8', len(raw))
            self.wfile.write(raw)
            return
        if self.path == '/api/bootstrap':
            return self.out({'needs_setup': not bool(CONFIG), 'version': VERSION})
        if not self.session():
            return self.out({'error': '请先登录'}, 401)
        if self.path == '/api/status':
            return self.out({'configured': bool(os.environ.get('DEEPSEEK_API_KEY') or CONFIG.get('api_key')), 'version': VERSION, 'model': MODEL, 'defaults': DEFAULTS, 'key_source': 'environment' if os.environ.get('DEEPSEEK_API_KEY') else 'local configuration'})
        with db() as c:
            if self.path == '/api/conversations':
                return self.out([dict(r) for r in c.execute('SELECT * FROM conversations ORDER BY updated DESC')])
            if self.path.startswith('/api/messages/'):
                cid = self.path.rsplit('/', 1)[1]
                return self.out([dict(r) for r in c.execute('SELECT role,content,created,metadata,images,reasoning FROM messages WHERE conversation_id=? ORDER BY id', (cid,))])
        self.out({'error': '页面不存在'}, 404)

    def do_POST(self):
        if not self.guard(True):
            return
        if self.path not in ('/api/login','/api/setup') and not self.session():
            return self.out({'error': '请先登录'}, 401)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8500000:
                return self.out({'error': '请求大小不合法，文件限 5 MB'}, 413)
            self.connection.settimeout(30)
            body = json.loads(self.rfile.read(size).decode('utf-8'))
            if not isinstance(body, dict):
                raise ValueError('请求必须是 JSON 对象')
            self.route(body)
        except (ValueError, KeyError, TypeError) as e:
            self.out({'error': str(e)[:160]}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.out({'error': '本地处理失败，请检查磁盘空间、文件格式及运行环境'}, 500)

    def route(self, body):
        if self.path.startswith(('/api/project/', '/api/file/', '/api/agent/')):
            return self.out(agent_service().route(self.path, body))
        if self.path == '/api/setup':
            with LOCK:
                if CONFIG:return self.out({'error':'已完成初次配置，请登录后修改'},409)
                if body.get('password')!=body.get('confirm'):raise ValueError('两次密码不一致')
                save_config(body.get('api_key'),body.get('password'))
            return self.out({'ok':True})
        if self.path == '/api/credentials':
            with LOCK:
                if BUSY or (AGENT is not None and AGENT.active()):return self.out({'error':'请等待生成结束或停止 Agent 任务'},409)
                if os.environ.get('DEEPSEEK_API_KEY'):
                    return self.out({'error':'当前环境变量覆盖了 Key；请停止服务并取消该环境变量后重启，再通过网页修改'},409)
                old=body.get('old_password','')
                if not isinstance(old,str) or len(old)>1024:raise ValueError('密码格式错误')
                ATTEMPTS[:]=[t for t in ATTEMPTS if t>time.time()-60]
                if len(ATTEMPTS)>=6:return self.out({'error':'尝试过多，请 60 秒后重试'},429)
                ATTEMPTS.append(time.time())
                digest=hashlib.pbkdf2_hmac('sha256',old.encode(),CONFIG['salt'].encode(),200000).hex()
                if not hmac.compare_digest(digest,CONFIG['password_hash']):return self.out({'error':'当前密码错误'},403)
                new=body.get('password') or old
                if body.get('password') and new!=body.get('confirm'):raise ValueError('两次新密码不一致')
                save_config(body.get('api_key') or CONFIG.get('api_key'),new)
                ATTEMPTS.clear()
            return self.out({'ok':True})
        if self.path == '/api/login':
            if not CONFIG:return self.out({'error':'请先完成网页初始配置'},400)
            with LOCK:
                ATTEMPTS[:] = [t for t in ATTEMPTS if t > time.time() - 60]
                if len(ATTEMPTS) >= 6:
                    return self.out({'error': '尝试过多，请 60 秒后重试'}, 429)
                ATTEMPTS.append(time.time())
            password = body.get('password', '')
            if not isinstance(password, str) or len(password) > 1024:
                raise ValueError('密码格式错误')
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), CONFIG['salt'].encode(), 200000).hex()
            if not hmac.compare_digest(digest, CONFIG['password_hash']):
                return self.out({'error': '密码错误'}, 401)
            sid = token()
            with LOCK:
                for old in list(SESSIONS):
                    if SESSIONS[old] < time.time():
                        del SESSIONS[old]
                SESSIONS[sid] = time.time() + 43200
                ATTEMPTS.clear()
            return self.out({'ok': True}, cookie='ds_session=' + sid + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
        if self.path == '/api/logout':
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            with LOCK:
                SESSIONS.pop(cookie['ds_session'].value, None)
            return self.out({'ok': True}, cookie='ds_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict')
        if self.path == '/api/account':
            result={}
            for field, endpoint in [('models','/models'),('balance','/user/balance')]:
                try:
                    key=os.environ.get('DEEPSEEK_API_KEY') or CONFIG.get('api_key')
                    if not key:raise ValueError('尚未配置 API Key')
                    req=urllib.request.Request('https://api.deepseek.com'+endpoint,headers={'Authorization':'Bearer '+key})
                    with urllib.request.urlopen(req,timeout=20) as response:
                        raw=response.read(1000001)
                        if len(raw)>1000000:raise ValueError('接口响应过大')
                        result[field]=json.loads(raw.decode('utf-8'))
                except Exception as e:
                    result[field]={'error': api_error(e)}
            return self.out(result)
        if self.path == '/api/extract':
            name = body.get('name', '')
            if not isinstance(name, str) or len(name) > 255:
                raise ValueError('文件名不合法')
            raw = base64.b64decode(body.get('data', ''), validate=True)
            return self.out({'text': extract(name, raw), 'name': name})
        cid = body.get('id', '')
        if not isinstance(cid, str) or len(cid) > 64:
            raise ValueError('会话编号不合法')
        if self.path == '/api/new':
            cid = uuid.uuid4().hex
            with db() as c:
                c.execute('INSERT INTO conversations(id,title,updated) VALUES (?,?,?)', (cid, '新对话', time.time()))
            return self.out({'id': cid})
        if self.path in ('/api/settings','/api/rename'):
            with LOCK:
                if BUSY or (AGENT is not None and AGENT.active()):return self.out({'error':'请等待生成结束或停止 Agent 任务'},409)
                with db() as c:
                    if not c.execute('SELECT id FROM conversations WHERE id=?',(cid,)).fetchone():
                        return self.out({'error':'会话不存在'},404)
                    if self.path=='/api/settings':
                        value=settings(body.get('settings',{}))
                        c.execute('UPDATE conversations SET settings=? WHERE id=?',(json.dumps(value),cid))
                    else:
                        value=body.get('title','')
                        if not isinstance(value,str) or not 1<=len(value.strip())<=80:raise ValueError('标题限 1–80 字符')
                        c.execute('UPDATE conversations SET title=? WHERE id=?',(value.strip(),cid))
            return self.out({'ok':True})
        if self.path == '/api/stop':
            with LOCK:
                event = BUSY.get(cid)
                if event:
                    event.set()
            return self.out({'ok': True})
        if self.path == '/api/delete':
            with LOCK:
                if cid in BUSY:
                    return self.out({'error': '请等待生成结束再删除'}, 409)
                with db() as c:
                    c.execute('DELETE FROM conversations WHERE id=?', (cid,))
            return self.out({'ok': True})
        if self.path == '/api/chat':
            return self.chat(cid, body)
        self.out({'error': '接口不存在'}, 404)

    def chat(self, cid, body):
        text=body.get('message','')
        if not isinstance(text,str) or not text.strip() or len(text)>120000:
            raise ValueError('消息及文件文字合计限 120000 字符')
        if body.get('model',MODEL)!=MODEL:raise ValueError('本版仅使用官方 deepseek-flash')
        pictures=images(body.get('images',[]))
        key=os.environ.get('DEEPSEEK_API_KEY') or CONFIG.get('api_key')
        if not key:return self.out({'error':'请运行 python3 app.py --setup 配置 Key 并重启'},400)
        with LOCK:
            if BUSY:return self.out({'error':'正在生成或结束上一条连接，请稍后重试'},409)
            with db() as c:
                conv=c.execute('SELECT settings FROM conversations WHERE id=?',(cid,)).fetchone()
                if not conv:return self.out({'error':'会话不存在'},404)
                options=settings(body.get('settings',json.loads(conv['settings'])))
                # Compatibility with older pages: explicit false still means non-thinking.
                if 'thinking' in body and 'settings' not in body:options['effort']='high' if body['thinking'] is True else 'none'
                rows=[dict(r) for r in c.execute('SELECT role,content,images FROM messages WHERE conversation_id=? ORDER BY id',(cid,))]
                payload,omitted=build(rows,text,pictures,options)
                c.execute('UPDATE conversations SET settings=? WHERE id=?',(json.dumps(options),cid))
            cancel=threading.Event()
            BUSY[cid]=cancel
        upstream=None
        started=False
        began=time.monotonic()
        meta=dict(requested_model=MODEL,returned_model=None,response_id=None,usage=None,finish_reason=None,omitted_turns=omitted,effort=options['effort'],max_tokens=options['max_tokens'] or 'API default',context_chars=options['context_chars'],endpoint=URL,client_version=VERSION,json_output=options['json_output'])
        def emit(obj):
            self.wfile.write((json.dumps(obj,ensure_ascii=False)+'\n').encode('utf-8'))
            self.wfile.flush()
        try:
            # Send local headers immediately, allowing wait status before upstream headers arrive.
            self.send_headers(200,'application/x-ndjson; charset=utf-8')
            started=True
            emit(dict(type='meta',metadata=meta,omitted_turns=omitted))
            req=urllib.request.Request(URL,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            upstream=urllib.request.urlopen(req,timeout=90)
            content=''
            reasoning=''
            finished=False
            for data in sse(upstream):
                if cancel.is_set():
                    emit({'type':'cancelled'})
                    return
                if time.monotonic()-began>1800:raise ValueError('达到本地 30 分钟时限，本轮未保存')
                if not data.strip():continue
                if data=='[DONE]':
                    finished=True
                    break
                chunk=json.loads(data)
                if chunk.get('error'):raise ValueError('API 返回流内错误，本轮未保存')
                if isinstance(chunk.get('model'),str):meta['returned_model']=chunk['model'][:200]
                if isinstance(chunk.get('id'),str):meta['response_id']=chunk['id'][:200]
                if isinstance(chunk.get('usage'),dict):meta['usage']=chunk['usage']
                for choice in chunk.get('choices',[]):
                    if choice.get('index',0)!=0:continue
                    delta=choice.get('delta') or {}
                    if choice.get('finish_reason'):meta['finish_reason']=choice['finish_reason']
                    for field,kind in [('reasoning_content','reasoning'),('content','content')]:
                        value=delta.get(field) or ''
                        if not isinstance(value,str):raise ValueError('API 返回内容格式异常')
                        if value:
                            if kind=='content':content+=value
                            else:reasoning+=value
                            if len(content)+len(reasoning)>2000000:raise ValueError('达到本地 200 万字符保护上限，本轮未保存')
                            emit(dict(type=kind,text=value))
            if cancel.is_set():
                emit({'type':'cancelled'})
                return
            if not finished or not content or meta['finish_reason'] not in ('stop','length'):
                raise ValueError('回复中断或没有完整正文，本轮未保存；请检查输出上限后重试')
            meta['elapsed_seconds']=round(time.monotonic()-began,2)
            if options['json_output']:
                try:
                    parsed=json.loads(content)
                    meta['json_valid']=isinstance(parsed,dict)
                except ValueError:meta['json_valid']=False
            with db() as c:
                now=time.time()
                c.execute('INSERT INTO messages(conversation_id,role,content,created,images) VALUES (?,?,?,?,?)',(cid,'user',text,now,json.dumps(pictures)))
                c.execute('INSERT INTO messages(conversation_id,role,content,created,metadata,reasoning) VALUES (?,?,?,?,?,?)',(cid,'assistant',content,now,json.dumps(meta),reasoning))
                c.execute('UPDATE conversations SET title=CASE WHEN title=? THEN ? ELSE title END,updated=? WHERE id=?',('新对话',text[:32].replace('\n',' '),now,cid))
            emit(dict(type='done',metadata=meta,truncated=meta['finish_reason']=='length'))
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception as e:
            try:
                if cancel.is_set():emit({'type':'cancelled'})
                elif started:emit(dict(type='error',error=api_error(e)))
                else:self.out({'error':api_error(e)},502)
            except (BrokenPipeError,ConnectionResetError):pass
        finally:
            if upstream is not None:upstream.close()
            with LOCK:BUSY.pop(cid,None)

def api_error(e):
    if isinstance(e,urllib.error.HTTPError):
        return {400:'API 参数被拒绝，请检查设置或缩短上下文',401:'API Key 无效，请重新配置并重启',402:'API 余额不足',422:'API 参数校验失败',429:'请求频繁，请稍后手动重试',500:'DeepSeek 服务内部错误',503:'DeepSeek 服务繁忙'}.get(e.code,'DeepSeek API 错误 '+str(e.code))
    if isinstance(e,ValueError):return str(e)[:180]
    return '连接或处理失败；请检查网络、证书和磁盘空间。未自动重试，请核对历史后重试。'

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='DeepSeek 本地办公客户端')
    parser.add_argument('--setup', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.setup:
        setup()
    else:
        init()
        try:
            CONFIG = json.loads((DATA / 'config.json').read_text())
        except FileNotFoundError:
            CONFIG={}
        except (OSError, ValueError):
            raise SystemExit('配置文件无法读取，请检查权限或恢复备份；未覆盖已有配置')
        agent_service()  # Recover interrupted jobs before serving requests.
        try:
            server = Server(('127.0.0.1', PORT), Handler)
        except OSError:
            raise SystemExit('端口 8765 被占用。检查是否已启动本客户端。')
        print('DeepSeek 客户端：http://127.0.0.1:8765 （Ctrl+C 停止）', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
