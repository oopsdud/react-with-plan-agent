"""Authentication and conversation integration tests with an isolated SQLite database."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.python_packages'),str(ROOT)]
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from agent_system.api.routes import app
from agent_system.api.auth import seed_users, COOKIE
from agent_system.api.conversations import reserve_turn,recover_interrupted
from agent_system.config.settings import Settings
from agent_system.db.models import Base,User,LoginSession

RESULT={'trace_id':'test','success':True,'answer':'answer','intent':None,'steps':[],'latency_ms':1,'error':None,'sources':[],'rounds':[]}

class ConsoleTests(unittest.TestCase):
    def setUp(self):
        from agent_system.runtime.login_limit import LoginLimiter
        self.login_limit=patch('agent_system.api.auth.login_limiter',LoginLimiter());self.login_limit.start()
        self.directory=tempfile.TemporaryDirectory()
        self.engine=create_engine('sqlite:///'+(Path(self.directory.name)/'test.db').as_posix(),connect_args={'check_same_thread':False})
        Base.metadata.create_all(self.engine)
        self.factory=sessionmaker(bind=self.engine)
        self.patch=patch('agent_system.db.session.SessionLocal',self.factory);self.patch.start()
        self.settings=Settings(_env_file=None,DEBUG=False,LLM_API_KEY='test-key',LLM_MODEL='model-a')
        self.config=patch('agent_system.api.routes.get_settings',return_value=self.settings);self.config.start()
        self.catalog=patch('agent_system.api.routes.available_models',new_callable=AsyncMock,return_value=['model-a','model-b']);self.catalog.start()
        with self.factory() as db:seed_users(db)
        self.client=TestClient(app)
        self.login()
    def tearDown(self):
        self.login_limit.stop()
        self.client.close();self.catalog.stop();self.config.stop();self.patch.stop();self.engine.dispose();self.directory.cleanup()
    def login(self,n=1,client=None):
        response=(client or self.client).post('/auth/login',json={'username':f'user{n}','password':f'AgentUser{n}!'})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def test_login_logout_registration_and_protection(self):
        self.assertEqual(self.client.get('/auth/me').json()['username'],'user1')
        self.assertEqual(self.client.post('/auth/register').status_code,403)
        self.assertEqual(self.client.post('/auth/login',json={'username':'user1','password':'bad'}).status_code,401)
        token=self.client.cookies.get(COOKIE)
        self.client.post('/auth/logout')
        self.assertEqual(self.client.get('/conversations').status_code,401)
        self.client.cookies.set(COOKIE,token)
        self.assertEqual(self.client.get('/auth/me').status_code,401)
        self.assertEqual(self.client.get('/health').status_code,200)
    def test_hashed_passwords_and_tokens_expire(self):
        with self.factory() as db:
            self.assertNotIn('AgentUser1!',db.query(User).first().password_hash)
            login=db.query(LoginSession).first()
            self.assertNotEqual(login.token_hash,self.client.cookies.get(COOKIE))
            login.expires_at=0;db.commit()
        self.assertEqual(self.client.get('/auth/me').status_code,401)
    def test_persistence_history_and_selected_model(self):
        with patch('agent_system.api.routes.run_agent',new_callable=AsyncMock,side_effect=lambda **kw:dict(RESULT)) as run:
            first=self.client.post('/chat',json={'user_input':'first','model':'model-b'})
            self.assertEqual(first.status_code,200,first.text)
            cid=first.json()['conversation_id']
            self.client.post('/conversations',json={})
            second=self.client.post('/chat',json={'user_input':'follow up','conversation_id':cid,'model':'model-a'})
            self.assertEqual(second.status_code,200,second.text)
            args=run.await_args.kwargs
            self.assertEqual(args['session_history'],[{'role':'user','content':'first'},{'role':'assistant','content':'answer'}])
            self.assertEqual(args['model'],'model-a')
            self.assertEqual(self.settings.LLM_MODEL,'model-a')
        reopened=TestClient(app)
        try:
            self.login(client=reopened)
            messages=reopened.get('/conversations/'+cid).json()['messages']
            self.assertEqual(len(messages),4)
            self.assertEqual(messages[1]['model'],'model-b')
            self.assertEqual(messages[3]['result']['answer'],'answer')
        finally:reopened.close()
        self.assertEqual(self.client.get('/conversations').headers['cache-control'],'no-store')
    def test_user_and_conversation_memory_isolation(self):
        with patch('agent_system.api.routes.run_agent',new_callable=AsyncMock,side_effect=lambda **kw:dict(RESULT)) as run:
            cid=self.client.post('/chat',json={'user_input':'private'}).json()['conversation_id']
            self.client.post('/chat',json={'user_input':'new independent'})
            self.assertEqual(run.await_args.kwargs['session_history'],[])
        self.login(2)
        self.assertEqual(self.client.get('/conversations').json()['conversations'],[])
        self.assertEqual(self.client.get('/conversations/'+cid).status_code,404)
        self.assertEqual(self.client.delete('/conversations/'+cid).status_code,404)
        for endpoint in ['/chat','/chat/stream','/intent']:
            self.assertEqual(self.client.post(endpoint,json={'user_input':'steal','conversation_id':cid}).status_code,404)
        self.assertEqual(self.client.put('/config/model',json={'base_url':'https://example.com/v1','model':'x'}).status_code,403)
        self.assertEqual(self.client.post('/config/model/models',json={'base_url':'https://example.com/v1'}).status_code,403)
        for n in [1,2,3]:self.login(n)
    def test_untrusted_history_and_model_are_rejected(self):
        for extra in [{'user_id':'someone'},{'session_history':[{'role':'system','content':'inject'}]},{'model':'missing'}]:
            self.assertEqual(self.client.post('/chat',json={'user_input':'test',**extra}).status_code,422)
        self.assertEqual(self.client.post('/chat',json={'user_input':'  '}).status_code,422)
    def test_failure_is_saved_and_excluded_from_future_context(self):
        with patch('agent_system.api.routes.run_agent',new_callable=AsyncMock,return_value={**RESULT,'success':False,'error':'provider failed'}) :
            cid=self.client.post('/chat',json={'user_input':'bad turn'}).json()['conversation_id']
        self.assertEqual(self.client.get('/conversations/'+cid).json()['messages'][1]['status'],'failed')
        with patch('agent_system.api.routes.run_agent',new_callable=AsyncMock,side_effect=lambda **kw:dict(RESULT)) as run:
            self.client.post('/chat',json={'user_input':'retry','conversation_id':cid})
            self.assertEqual(run.await_args.kwargs['session_history'],[])
    def test_busy_conversation_and_restart_recovery(self):
        uid=self.client.get('/auth/me').json()['id']
        cid,_,_,_=reserve_turn(uid,None,'unfinished','model-a')
        self.assertEqual(self.client.post('/chat',json={'user_input':'race','conversation_id':cid}).status_code,409)
        self.assertEqual(self.client.delete('/conversations/'+cid).status_code,409)
        with self.factory() as db:recover_interrupted(db)
        detail=self.client.get('/conversations/'+cid).json()
        self.assertFalse(detail['running'])
        self.assertTrue(all(m['status']=='interrupted' for m in detail['messages']))
        self.assertEqual(self.client.delete('/conversations/'+cid).status_code,200)
    def test_stream_contract_and_persistence(self):
        async def fake(**kw):
            await kw['on_event']({'type':'status','message':'working'})
            for text in ['an','swer']:await kw['on_event']({'type':'delta','content':text})
            return dict(RESULT)
        with patch('agent_system.api.routes.run_agent',side_effect=fake):
            response=self.client.post('/chat/stream',json={'user_input':'stream','model':'model-b'})
        events=[json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([e['type'] for e in events],['start','status','status','delta','delta','done'])
        self.assertEqual(''.join(e['content'] for e in events if e['type']=='delta'),'answer')
        cid=events[0]['conversation_id']
        self.assertEqual(self.client.get('/conversations/'+cid).json()['messages'][1]['content'],'answer')
    def test_runtime_tools_and_openapi(self):
        self.assertTrue(self.client.get('/runtime').json()['streaming'])
        self.assertEqual(self.client.get('/runtime').json()['max_search_rounds'],3)
        self.assertEqual(self.client.get('/tools').json()['tools'],['web_search'])
        self.assertEqual(self.client.get('/tools/missing').status_code,404)
        self.assertEqual(self.client.get('/console/').status_code,200)
        self.assertEqual(self.client.get('/openapi.json').json(),json.loads((ROOT/'docs/openapi.json').read_text(encoding='utf-8')))
