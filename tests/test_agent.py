import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.controller import AgentService, URL, TERMINAL_STATES
from project_manager import Store, digest
from tools import filesystem, terminal, git


def stream(message, done=True, finish=None):
    calls = message.pop('tool_calls', [])
    chunks = [dict(id='test-response', model='deepseek-flash', choices=[dict(index=0, delta=message, finish_reason=None)])]
    # Fragment arguments as real streaming providers do, interleaving calls by index.
    for i, c in enumerate(calls):
        raw = json.dumps(c['args'])
        chunks.append(dict(choices=[dict(index=0,delta={'tool_calls':[dict(index=i,id=c.get('id','call-'+str(i)),function=dict(name=c['name'],arguments=raw[:2]))]})]))
        chunks.append(dict(choices=[dict(index=0,delta={'tool_calls':[dict(index=i,function=dict(arguments=raw[2:]))]})]))
    chunks.append(dict(usage=dict(prompt_tokens=11,completion_tokens=7,total_tokens=18),choices=[dict(index=0,delta={},finish_reason=finish or ('tool_calls' if calls else 'stop'))]))
    return io.BytesIO((''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+('data: [DONE]\n\n' if done else '')).encode())


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.root = self.folder/'project'
        self.root.mkdir()
        (self.root/'app.py').write_text('value = 1\n')
        self.service = AgentService(self.folder/'data', lambda:'test-api-key', lambda e:str(e))
        self.store = self.service.store
        self.project = self.store.open(str(self.root))['id']

    def tearDown(self):
        for r in self.store.list('run'):
            if r['state'] not in TERMINAL_STATES:
                self.service.stop(r['id'])
        end = time.monotonic()+3
        while self.service.cancel_events and time.monotonic()<end:
            time.sleep(.01)
        self.tmp.cleanup()

    def wait(self, ident, states):
        end = time.monotonic()+5
        while time.monotonic()<end:
            run = self.store.get('run',ident)
            if run['state'] in states:
                return run
            time.sleep(.01)
        self.fail('task stuck: '+str(run))

    def start(self, **extra):
        return self.service.start(dict(project=self.project,message='修复 value，执行测试',**extra))['id']

    def test_paths_secrets_links_and_binary(self):
        (self.root/'.env').write_text('SECRET=private')
        (self.root/'bin').write_bytes(b'\x00\xff')
        (self.root/'jump').symlink_to(self.folder/'data',target_is_directory=True)
        os.link(str(self.root/'app.py'),str(self.root/'hard.py'))
        for path in ('../other','/etc/passwd','.env','jump/config.json','hard.py'):
            with self.assertRaises(ValueError):self.store.path(self.project,path)
        with self.assertRaises(ValueError):self.store.read(self.project,'bin')
        with self.assertRaises(ValueError):self.store.open(str(self.folder))
        index=self.store.scan(self.project)
        self.assertNotIn('.env',[f['path'] for f in index['files']])

    def test_search_pages_and_stale_files(self):
        (self.root/'large.py').write_text('find me\n'*600)
        self.store.scan(self.project)
        result=self.store.read(self.project,'large.py',1,100)
        self.assertEqual(result['next_start'],101)
        self.assertEqual(result['sha256'],digest((self.root/'large.py').read_bytes()))
        result=self.store.search(self.project,'find me')
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['matches']),60)
        (self.root/'large.py').unlink()
        self.assertEqual(self.store.search(self.project,'find me')['matches'],[])

    def test_patch_conflict_accept_replay_and_rollback(self):
        sha=digest((self.root/'app.py').read_bytes())
        c=filesystem.propose(self.store,self.project,'app.py',old='1',new='2',expected_sha=sha)
        self.assertEqual((self.root/'app.py').read_text(),'value = 1\n')
        (self.root/'app.py').write_text('user edit\n')
        with self.assertRaises(ValueError):filesystem.decide(self.store,c['id'],True)
        (self.root/'app.py').write_text('value = 1\n')
        filesystem.decide(self.store,c['id'],True)
        self.assertEqual((self.root/'app.py').read_text(),'value = 2\n')
        with self.assertRaises(ValueError):filesystem.decide(self.store,c['id'],True)
        (self.root/'app.py').write_text('later edit\n')
        with self.assertRaises(ValueError):filesystem.rollback(self.store,c['id'])
        (self.root/'app.py').write_text('value = 2\n')
        filesystem.rollback(self.store,c['id'])
        self.assertEqual((self.root/'app.py').read_text(),'value = 1\n')
        c=filesystem.propose(self.store,self.project,'new/n.txt',content='new',expected_sha=None)
        filesystem.decide(self.store,c['id'],True)
        filesystem.rollback(self.store,c['id'])
        self.assertFalse((self.root/'new/n.txt').exists())

    def test_restart_recovers_write_ahead_backup(self):
        change=filesystem.propose(self.store,self.project,'app.py',content='value = 2\n',expected_sha=digest(b'value = 1\n'))
        change['state']='applying'
        self.store.put('change',change)
        (self.root/'app.py').write_text('value = 2\n')
        self.service=AgentService(self.folder/'data',lambda:'test-api-key',lambda e:str(e))
        self.assertEqual(self.store.get('change',change['id'])['state'],'accepted')
        filesystem.rollback(self.store,change['id'])
        self.assertEqual((self.root/'app.py').read_text(),'value = 1\n')

    def test_terminal_limits_env_and_cancellation(self):
        for argv in (['sh','-c','echo hello'],['sudo','true'],['/usr/bin/python3','-V'],'python3 -V'):
            with self.assertRaises(ValueError):terminal.validate(argv)
        with patch.dict(os.environ,DEEPSEEK_API_KEY='never-inherit',PYTHONPATH='/secret'):
            r=terminal.execute(['python3','-c','import os; print(os.getenv("DEEPSEEK_API_KEY")); print(os.getenv("PYTHONPATH"))'],self.root)
        self.assertEqual(r['returncode'],0)
        self.assertEqual(r['output'],'None\nNone\n')
        r=terminal.execute(['python3','-c','import time; time.sleep(5)'],self.root,timeout=1)
        self.assertEqual(r['stopped'],'timeout')
        r=terminal.execute(['python3','-c','print("x"*100000)'],self.root)
        self.assertEqual(r['stopped'],'output_limit')
        self.assertLessEqual(len(r['output']),65536)
        cancel=threading.Event();cancel.set()
        r=terminal.execute(['python3','-c','import time; time.sleep(5)'],self.root,cancel)
        self.assertEqual(r['stopped'],'cancelled')

    def test_full_loop_approval_reasoning_usage_and_testing(self):
        seen=[]
        responses=[
            dict(content='先读取实现',reasoning_content='reason-1',tool_calls=[dict(name='read_file',args=dict(path='app.py'))]),
            dict(content='修正值',reasoning_content='reason-2',tool_calls=[dict(name='patch_file',args=dict(path='app.py',old='1',new='2',expected_sha=digest(b'value = 1\n')))]),
            dict(content='测试',reasoning_content='reason-3',tool_calls=[dict(name='execute_command',args=dict(argv=['python3','-c','from app import value; assert value == 2; print("PASS")'],purpose='验证修复'))]),
            dict(content='已修复，测试通过',reasoning_content='reason-4')]
        def fake(req,**kw):
            payload=json.loads(req.data);seen.append(payload)
            self.assertEqual(req.full_url,URL)
            return stream(responses.pop(0))
        with patch('agent.controller.urllib.request.urlopen',side_effect=fake):
            ident=self.start()
            run=self.wait(ident,{'waiting_approval','failed'})
            self.assertEqual(run['pending']['kind'],'file',run)
            self.assertEqual((self.root/'app.py').read_text(),'value = 1\n')
            self.service.approve(dict(run=ident,approval=run['pending']['id'],accept=True))
            run=self.wait(ident,{'waiting_approval','failed'})
            self.assertEqual(run['pending']['kind'],'command',run)
            old=run['pending']['id']
            self.service.approve(dict(run=ident,approval=old,accept=True))
            with self.assertRaises(ValueError):self.service.approve(dict(run=ident,approval=old,accept=True))
            run=self.wait(ident,{'finished','failed'})
        self.assertEqual(run['state'],'finished',run)
        self.assertEqual(len(seen),4)
        self.assertEqual(seen[1]['messages'][2]['reasoning_content'],'reason-1')
        self.assertEqual(seen[1]['messages'][3]['role'],'tool')
        self.assertEqual(run['calls'][0]['usage']['total_tokens'],18)
        self.assertIn('PASS',json.dumps(run['events']))
        self.assertNotIn('test-api-key',json.dumps(run))
        self.assertNotIn('messages',self.service.public(run))

    def test_reject_command_never_executes(self):
        count=[0]
        def fake(req,**kw):
            count[0]+=1
            if count[0]==1:return stream(dict(content='',reasoning_content='r',tool_calls=[dict(name='execute_command',args=dict(argv=['python3','-V'],purpose='检查版本'))]))
            self.assertIn('rejected',json.loads(req.data)['messages'][-1]['content'])
            return stream(dict(content='已取消命令'))
        with patch('agent.controller.urllib.request.urlopen',side_effect=fake),patch('tools.terminal.execute',side_effect=AssertionError('must not run')):
            ident=self.start();run=self.wait(ident,{'waiting_approval'})
            self.service.approve(dict(run=ident,approval=run['pending']['id'],accept=False))
            run=self.wait(ident,{'finished','failed'})
            self.assertEqual(run['state'],'finished',run)

    def test_incomplete_stream_never_executes(self):
        response=dict(content='',tool_calls=[dict(name='execute_command',args=dict(argv=['python3','-V'],purpose='版本'))])
        with patch('agent.controller.urllib.request.urlopen',return_value=stream(response,done=False)):
            ident=self.start();run=self.wait(ident,{'failed'})
        self.assertIsNone(run['pending'])
        self.assertEqual(run['step'],1)

    def test_stop_waiting_and_restart_no_replay(self):
        def fake(*a,**kw):return stream(dict(content='',tool_calls=[dict(name='write_file',args=dict(path='new.txt',content='new',expected_sha=None))]))
        with patch('agent.controller.urllib.request.urlopen',side_effect=fake):
            ident=self.start();run=self.wait(ident,{'waiting_approval'})
            approval=run['pending']['id'];change=run['pending']['change_id']
            self.service.stop(ident)
            self.assertEqual(self.store.get('change',change)['state'],'rejected')
            with self.assertRaises(ValueError):self.service.approve(dict(run=ident,approval=approval,accept=True))
            ident=self.start();self.wait(ident,{'waiting_approval'})
        self.service=AgentService(self.folder/'data',lambda:'test-api-key',lambda e:str(e))
        self.assertEqual(self.store.get('run',ident)['state'],'interrupted')
        self.assertFalse((self.root/'new.txt').exists())

    def test_compact_archives_only_complete_groups(self):
        run=dict(id='test-run',state='finished',project=self.project,task='修复',step=4,context_chars=48000,compactions=0,events=[],
                 messages=[dict(role='user',content='x'*60000)])
        self.service.compact(run)
        self.assertEqual(run['compactions'],1)
        self.assertEqual([m['role'] for m in run['messages']],['system','user'])
        self.assertTrue(self.store.get('transcript','test-run-4'))

    def test_multiple_tools_unknown_tool_and_step_limit(self):
        def fake(req,**kw):
            return stream(dict(content='',tool_calls=[dict(name='read_file',args=dict(path='app.py')),dict(name='invented',args={})]))
        with patch('agent.controller.urllib.request.urlopen',side_effect=fake):
            ident=self.start(max_steps=1);run=self.wait(ident,{'limit','failed'})
        self.assertEqual(run['state'],'limit',run)
        results=[m for m in run['messages'] if m['role']=='tool']
        self.assertEqual(len(results),2)
        self.assertIn('error',results[1]['content'])

    def test_git_filters_tracked_secrets_and_checkpoint_keeps_index(self):
        def g(*args):return subprocess.run(['git']+list(args),cwd=str(self.root),check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        g('init');g('config','user.email','test@example.invalid');g('config','user.name','Test')
        (self.root/'.env').write_text('SECRET=old')
        g('add','.');g('commit','-m','baseline')
        (self.root/'.env').write_text('SECRET=do-not-send')
        (self.root/'app.py').write_text('value = 2\n')
        index_before=(self.root/'.git/index').read_bytes()
        r=git.inspect(self.store,self.project,'diff')
        self.assertIn('+value = 2',r['output'])
        self.assertNotIn('SECRET',r['output'])
        self.assertNotIn('.env',r['output'])
        checkpoint=git.checkpoint(self.store,self.project)
        self.assertTrue(checkpoint['id'])
        # git status may refresh index stat data, but staged content must remain unchanged.
        self.assertEqual(g('diff','--cached','--name-only').stdout,b'')
        self.assertEqual(g('rev-list','--count','HEAD').stdout.strip(),b'1')



class AgentHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        import urllib.request
        cls.app=app
        cls.previous_agent=app.AGENT
        cls.temp=tempfile.TemporaryDirectory()
        cls.project_temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.project_temp.name)/'demo'
        cls.root.mkdir()
        (cls.root/'sample.py').write_text('print("test")\n')
        app.DATA=Path(cls.temp.name)
        app.init()
        app.CONFIG=dict(api_key='test-only',salt='salt',password_hash=hashlib.pbkdf2_hmac('sha256',b'password123',b'salt',200000).hex())
        cls.server=app.Server(('127.0.0.1',0),app.Handler)
        app.PORT=cls.server.server_address[1]
        cls.base='http://127.0.0.1:'+str(app.PORT)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        req=urllib.request.Request(cls.base+'/api/login',data=b'{"password":"password123"}',headers={'Origin':cls.base,'Content-Type':'application/json','X-Requested-With':'DeepSeekClient'})
        with urllib.request.urlopen(req) as r:cls.cookie=r.headers['Set-Cookie'].split(';')[0]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close()
        cls.app.AGENT=cls.previous_agent
        cls.temp.cleanup();cls.project_temp.cleanup()

    def call(self,path,body=None,auth=True,origin=True):
        import urllib.request
        headers={'Content-Type':'application/json','X-Requested-With':'DeepSeekClient'}
        if auth:headers['Cookie']=self.cookie
        if origin:headers['Origin']=self.base
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        return urllib.request.urlopen(req,timeout=5)

    def test_manual_file_proposal_accept_and_rollback(self):
        project=json.load(self.call('/api/project/open',dict(path=str(self.root))))
        body=dict(project=project['id'],path='manual.py',content='print(123)\n',expected_sha=None)
        change=json.load(self.call('/api/file/write',body))
        self.assertFalse((self.root/'manual.py').exists())
        self.assertNotIn('before',change)
        json.load(self.call('/api/file/decide',dict(change=change['id'],accept=True)))
        self.assertEqual((self.root/'manual.py').read_text(),'print(123)\n')
        json.load(self.call('/api/file/rollback',dict(change=change['id'])))
        self.assertFalse((self.root/'manual.py').exists())

    def test_authenticated_routes_and_project_reads(self):
        import urllib.error
        for auth,origin in ((False,True),(True,False)):
            with self.assertRaises(urllib.error.HTTPError) as e:self.call('/api/project/open',dict(path=str(self.root)),auth,origin)
            self.assertIn(e.exception.code,(401,403))
        project=json.load(self.call('/api/project/open',dict(path=str(self.root))))
        files=json.load(self.call('/api/project/tree',dict(project=project['id'])))
        self.assertEqual(files['files'][0]['path'],'sample.py')
        data=json.load(self.call('/api/file/read',dict(project=project['id'],path='sample.py')))
        self.assertIn('print',data['content'])
        with self.assertRaises(urllib.error.HTTPError):self.call('/api/file/read',dict(project=project['id'],path='../private'))
        for path in ('/agent','/static/agent.js','/static/agent.css'):
            with self.call(path) as r:self.assertEqual(r.status,200)

if __name__=='__main__':unittest.main(verbosity=2)
