import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from extract import extract
from protocol import settings, images, build, sse

class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app.DATA = Path(cls.tmp.name)
        app.init()
        app.CONFIG = dict(api_key='test-only', salt='salt', password_hash=hashlib.pbkdf2_hmac('sha256', b'password123', b'salt', 200000).hex())
        cls.server = app.Server(('127.0.0.1', 0), app.Handler)
        app.PORT = cls.server.server_address[1]
        cls.base = 'http://127.0.0.1:' + str(app.PORT)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.cookie = ''
        response = cls.call('/api/login', {'password':'password123'})
        cls.cookie = response.headers['Set-Cookie'].split(';')[0]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    @classmethod
    def call(cls, path, body=None, cookie=True, origin=None, host=None):
        headers={'Origin':origin or cls.base, 'X-Requested-With':'DeepSeekClient', 'Content-Type':'application/json'}
        if cookie: headers['Cookie']=cls.cookie
        if host: headers['Host']=host
        req=urllib.request.Request(cls.base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        return urllib.request.urlopen(req, timeout=5)

    def test_auth_origin_host(self):
        for kwargs in [dict(cookie=False), dict(host='attacker.example')]:
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.call('/api/conversations', **kwargs)
            self.assertIn(e.exception.code, (401,403))
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call('/api/new', {}, origin='https://attacker.example')
        self.assertEqual(e.exception.code,403)

    def new(self):
        return json.load(self.call('/api/new',{}))['id']

    def fake(self, complete=True):
        parts=[{'choices':[{'delta':{'reasoning_content':'思考'},'finish_reason':None}]}, {'choices':[{'delta':{'content':'你好 <script>alert(1)</script>'},'finish_reason':None}]}, {'choices':[{'delta':{},'finish_reason':'stop'}]}]
        return io.BytesIO((''.join('data: '+json.dumps(p)+'\n\n' for p in parts)+('data: [DONE]\n\n' if complete else '')).encode())

    def test_stream_history_and_secret(self):
        cid=self.new()
        real=urllib.request.urlopen
        def open_mock(req,*a,**kw):
            if req.full_url==app.URL:
                payload=json.loads(req.data)
                self.assertEqual(payload['model'],'deepseek-flash')
                self.assertEqual(payload['thinking']['type'],'enabled')
                return self.fake()
            return real(req,*a,**kw)
        with patch('app.urllib.request.urlopen', side_effect=open_mock):
            events=[json.loads(l) for l in self.call('/api/chat',dict(id=cid,message='测试',thinking=True)).read().decode().splitlines()]
        self.assertEqual(events[-1]['type'],'done')
        rows=json.load(self.call('/api/messages/'+cid))
        self.assertEqual([r['role'] for r in rows],['user','assistant'])
        self.assertNotIn('test-only',self.call('/api/status').read().decode())
        self.call('/api/delete',dict(id=cid)).close()
        self.assertEqual(json.load(self.call('/api/messages/'+cid)),[])

    def test_incomplete_not_saved_and_lock_released(self):
        cid=self.new()
        real=urllib.request.urlopen
        def open_mock(req,*a,**kw):
            return self.fake(False) if req.full_url==app.URL else real(req,*a,**kw)
        with patch('app.urllib.request.urlopen',side_effect=open_mock):
            events=[json.loads(l) for l in self.call('/api/chat',dict(id=cid,message='测试')).read().decode().splitlines()]
        self.assertEqual(events[-1]['type'],'error')
        self.assertEqual(json.load(self.call('/api/messages/'+cid)),[])
        self.assertNotIn(cid,app.BUSY)

    def test_cancel_not_saved(self):
        cid=self.new()
        real=urllib.request.urlopen
        def open_mock(req,*a,**kw):
            if req.full_url==app.URL:
                app.BUSY[cid].set()
                return self.fake()
            return real(req,*a,**kw)
        with patch('app.urllib.request.urlopen',side_effect=open_mock):
            data=self.call('/api/chat',dict(id=cid,message='停止')).read().decode()
        self.assertIn('cancelled',data)
        self.assertEqual(json.load(self.call('/api/messages/'+cid)),[])

    def test_context_pairs(self):
        rows=[dict(role=role,content='a'*15000) for role in ['user','assistant']*3]
        payload,omitted=build(rows,'你好',[],settings(dict(context_chars=48000,system='测试')))
        ctx=payload['messages']
        self.assertEqual(omitted,2)
        self.assertEqual([r['role'] for r in ctx],['system','user','assistant','user'])

    def test_files(self):
        self.assertEqual(extract('中文.txt','测试'.encode('gb18030')),'测试')
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            z.writestr('word/document.xml','<document><p><t>段落一</t></p><p><t>段落二</t></p></document>')
        self.assertEqual(extract('test.docx',raw.getvalue()),'段落一\n段落二')
        with self.assertRaises(ValueError):extract('x.md',b'a'*100001)
        with self.assertRaises(ValueError):extract('x.doc',b'old')
        with self.assertRaises(ValueError):extract('x.docx',b'bad zip')
        response=json.load(self.call('/api/extract',dict(name='a.txt',data=base64.b64encode(b'hello').decode())))
        self.assertEqual(response['text'],'hello')

    def test_xlsx(self):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            z.writestr('xl/workbook.xml','<workbook xmlns:r="urn:r"><sheets><sheet name="预算" r:id="r1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/worksheets/sheet1.xml','<worksheet><sheetData><row><c r="A1" t="inlineStr"><is><t>金额</t></is></c><c r="B1"><v>42</v></c><c r="C1"><f>B1*2</f></c></row></sheetData></worksheet>')
        text=extract('a.xlsx',raw.getvalue())
        self.assertIn('工作表：预算',text)
        self.assertIn('B1=42',text)
        self.assertIn('公式无缓存值',text)


class FlashTests(unittest.TestCase):
    setUpClass=classmethod(ClientTests.setUpClass.__func__)
    tearDownClass=classmethod(ClientTests.tearDownClass.__func__)
    call=classmethod(ClientTests.call.__func__)
    new=ClientTests.new
    def test_official_defaults_and_max_effort(self):
        p,n=build([], '你好', [], settings({}))
        self.assertEqual(p['model'],'deepseek-flash')
        self.assertEqual(p['reasoning_effort'],'high')
        self.assertNotIn('max_tokens',p)
        self.assertEqual([m['role'] for m in p['messages']],['user'])
        p,n=build([], 'JSON', [],settings(dict(effort='max',max_tokens=131072,json_output=True)))
        self.assertEqual(p['max_tokens'],131072)
        self.assertEqual(p['response_format'],{'type':'json_object'})
        self.assertIn('JSON',p['messages'][0]['content'])
        p,n=build([], '你好',[],settings(dict(effort='none')))
        self.assertNotIn('reasoning_effort',p)
        self.assertNotIn('temperature',p)

    def test_invalid_settings(self):
        for invalid in [dict(effort='ultra'),dict(max_tokens=True),dict(max_tokens=-1),dict(context_chars=1),dict(json_output='yes')]:
            with self.assertRaises(ValueError):settings(invalid)
        with self.assertRaises(ValueError):build([], 'x'*48001,[],settings(dict(context_chars=48000)))

    def test_image_history(self):
        data=base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'test').decode()
        pics=images([dict(name='a.png',data=data)])
        rows=[dict(role='user',content='图片',images=json.dumps(pics)),dict(role='assistant',content='图片说明',images='[]')]
        p,n=build(rows,'继续',[],settings({}))
        self.assertEqual(p['messages'][0]['content'][1]['type'],'image_url')
        self.assertTrue(p['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;'))
        with self.assertRaises(ValueError):images([dict(name='x.png',data=base64.b64encode(b'<script>').decode())])
        with self.assertRaises(ValueError):images([dict(name='x',data=data)]*4)
        rows[0]['content']='a'*100000
        p,n=build(rows,'继续',[],settings({}))
        self.assertEqual(n,1)
        self.assertEqual(len(p['messages']),1)

    def test_sse_keepalive_multiline(self):
        self.assertEqual(list(sse([b': keepalive\r\n',b'\r\n',b'data: {\r\n',b'data: "a":1}\r\n',b'\r\n',b'data: [DONE]\n',b'\n'])),['{\n"a":1}','[DONE]'])

    def test_model_usage_stored(self):
        cid=self.new()
        usage=dict(prompt_tokens=10,completion_tokens=5,total_tokens=15,prompt_cache_hit_tokens=3)
        chunk=dict(id='response-test',model='deepseek-flash',usage=usage,choices=[dict(index=0,delta=dict(content='{"ok":true}',reasoning_content='测试思考'),finish_reason='stop')])
        real=urllib.request.urlopen
        def fake(req,*a,**kw):
            if req.full_url==app.URL:return io.BytesIO(('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode())
            return real(req,*a,**kw)
        with patch('app.urllib.request.urlopen',side_effect=fake):
            events=[json.loads(l) for l in self.call('/api/chat',dict(id=cid,message='JSON',settings=dict(json_output=True))).read().decode().splitlines()]
        meta=events[-1]['metadata']
        self.assertEqual(meta['usage'],usage)
        self.assertEqual(meta['returned_model'],'deepseek-flash')
        self.assertEqual(meta['response_id'],'response-test')
        self.assertTrue(meta['json_valid'])
        r=json.load(self.call('/api/messages/'+cid))[-1]
        self.assertEqual(json.loads(r['metadata']),meta)
        self.assertEqual(r['reasoning'],'测试思考')

    def test_length_preserves_raw_json(self):
        cid=self.new();real=urllib.request.urlopen
        def fake(req,*a,**kw):
            if req.full_url==app.URL:return io.BytesIO(b'data: {"choices":[{"delta":{"content":"{\\"a\\":"},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n')
            return real(req,*a,**kw)
        with patch('app.urllib.request.urlopen',side_effect=fake):
            events=[json.loads(l) for l in self.call('/api/chat',dict(id=cid,message='JSON',settings=dict(json_output=True))).read().decode().splitlines()]
        self.assertEqual(events[-1]['type'],'done')
        self.assertFalse(events[-1]['metadata']['json_valid'])
        self.assertEqual(json.load(self.call('/api/messages/'+cid))[-1]['content'],'{"a":')

    def test_account_requests_and_errors(self):
        real=urllib.request.urlopen;urls=[]
        def fake(req,*a,**kw):
            if req.full_url.startswith('https://api.deepseek.com'):
                urls.append(req.full_url)
                if req.full_url.endswith('/models'):return io.BytesIO(b'{"data":[{"id":"deepseek-flash"}]}')
                raise urllib.error.HTTPError(req.full_url,402,'balance',{},None)
            return real(req,*a,**kw)
        with patch('app.urllib.request.urlopen',side_effect=fake):result=json.load(self.call('/api/account',{}))
        self.assertEqual(len(urls),2)
        self.assertEqual(result['models']['data'][0]['id'],'deepseek-flash')
        self.assertIn('error',result['balance'])

    def test_web_setup_and_credentials(self):
        old=dict(app.CONFIG)
        try:
            app.CONFIG={}
            self.assertTrue(json.load(self.call('/api/bootstrap',cookie=False))['needs_setup'])
            self.call('/api/setup',dict(api_key='sk-test-not-real',password='password123',confirm='password123'),cookie=False).close()
            self.assertFalse(json.load(self.call('/api/bootstrap',cookie=False))['needs_setup'])
            with self.assertRaises(urllib.error.HTTPError) as e:self.call('/api/setup',{},cookie=False)
            self.assertEqual(e.exception.code,409)
            r=self.call('/api/login',dict(password='password123'),cookie=False)
            type(self).cookie=r.headers['Set-Cookie'].split(';')[0]
            self.assertNotIn('sk-test-not-real',self.call('/api/status').read().decode())
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
                self.call('/api/credentials',dict(old_password='password123',api_key='sk-new-test',password='',confirm='')).close()
            self.assertEqual(app.CONFIG['api_key'],'sk-new-test')
            with self.assertRaises(urllib.error.HTTPError):self.call('/api/status')
            self.assertEqual((app.DATA/'config.json').stat().st_mode & 0o777,0o600)
        finally:
            app.CONFIG=old
            r=self.call('/api/login',dict(password='password123'),cookie=False)
            type(self).cookie=r.headers['Set-Cookie'].split(';')[0]

    def test_migrate_v1_history(self):
        import sqlite3
        original=app.DATA
        try:
            with tempfile.TemporaryDirectory() as folder:
                app.DATA=Path(folder)
                with sqlite3.connect(str(app.DATA/'history.db')) as c:
                    c.executescript("CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,updated REAL);CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,role TEXT,content TEXT,created REAL);INSERT INTO conversations VALUES ('old','原对话',1);INSERT INTO messages VALUES (1,'old','user','原始材料',1);")
                app.init()
                with app.db() as c:
                    row=c.execute('SELECT * FROM messages').fetchone()
                    self.assertEqual(row['content'],'原始材料')
                    self.assertEqual(row['metadata'],'{}')
                self.assertTrue((app.DATA/'history-before-v1.1.db').exists())
                app.init()
                with app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0],1)
        finally:app.DATA=original

    def test_settings_and_rename(self):
        cid=self.new()
        self.call('/api/settings',dict(id=cid,settings=dict(effort='max'))).close()
        self.call('/api/rename',dict(id=cid,title='审阅材料')).close()
        c=next(c for c in json.load(self.call('/api/conversations')) if c['id']==cid)
        self.assertEqual(c['title'],'审阅材料')
        self.assertEqual(json.loads(c['settings'])['effort'],'max')

if __name__=='__main__':unittest.main(verbosity=2)
