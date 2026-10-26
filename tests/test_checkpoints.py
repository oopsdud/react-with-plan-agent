"""Durable graph, crash recovery and partial-rescheduling integration coverage."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.python_packages'),str(ROOT)]
import httpx
import test_console as helpers
from agent_system.api.routes import app
from agent_system.core.executor import ReActExecutor
from agent_system.core.planner import Plan,PlanStep
from agent_system.api.conversations import reserve_turn,mark_running,finish_turn,recover_interrupted
from agent_system.runtime.checkpoints import Checkpoint,revise_plan,claim_resume,inspect_checkpoint,model_identity
from agent_system.runtime.errors import ServiceError
from agent_system.db.models import RunCheckpoint,StepAttempt,StepCheckpoint
from agent_system.runtime import get_runtime
from fakes import FakeLLM


def node(i,name,deps=(),params=None):
    return PlanStep(step_id=i,tool_name=name,depends_on=list(deps),params_template=params or {'value':str(i)})

class DurableTests(unittest.IsolatedAsyncioTestCase):
    setUp=helpers.ConsoleTests.setUp
    tearDown=helpers.ConsoleTests.tearDown
    login=helpers.ConsoleTests.login
    async def asyncSetUp(self):
        from agent_system.tools.registry import get_tool_registry
        get_tool_registry().get('web_search').input_model
        self.runtime_config=patch('agent_system.runtime.get_settings',return_value=self.settings);self.runtime_config.start()
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',cookies=self.client.cookies)
        self.uid=(await self.http.get('/auth/me')).json()['id']
    async def asyncTearDown(self):
        await self.http.aclose();await get_runtime().scheduler.close();get_runtime().search_threads.close();self.runtime_config.stop()
    def create(self):
        reservation=reserve_turn(self.uid,None,'graph task','model-a',checkpoint_spec={'identity':model_identity(self.settings,'model-a'),'mode':'graph'})
        self.assertEqual(mark_running(reservation[2]),1)
        return reservation,Checkpoint(reservation[2],self.uid,1)
    def executor(self,tools):
        for tool in tools.values():tool.replay_safe=True
        executor=ReActExecutor(tools,FakeLLM());executor.MAX_RETRIES=0
        return executor
    async def resume(self,reservation,generation):
        updated,new_generation,state=claim_resume(reservation[2],self.uid,generation,model_identity(self.settings,'model-a'))
        self.assertEqual(mark_running(reservation[2],new_generation),1)
        return updated,Checkpoint(reservation[2],self.uid,new_generation)
    async def test_repair_only_failed_a_reuses_completed_b_c(self):
        reservation,cp=self.create()
        a=AsyncMock(side_effect=[RuntimeError('missing source'),{'ok':True}]);b=AsyncMock(return_value={'value':'B'});c=AsyncMock(return_value={'value':'C'})
        executor=self.executor({'a':a,'b':b,'c':c})
        initial=Plan(intent_id='test',steps=[node(1,'a'),node(2,'b'),node(3,'c')])
        first=await executor.execute(initial,'trace',checkpoint=cp)
        self.assertEqual(first.status,'failed')
        finish_turn(*reservation[:3],{**helpers.RESULT,'success':False})
        revised=initial.model_copy(deep=True);revised.steps[0].depends_on=[2];revised.steps[0].params_template={'value':'{{steps.2.result.value}}'}
        version=revise_plan(cp.run_id,self.uid,revised,1,'A requires B',['a','b','c'])
        self.assertEqual(version,2)
        reservation,cp=await self.resume(reservation,1)
        result=await executor.execute(revised,'trace',checkpoint=cp)
        self.assertEqual(result.status,'completed')
        self.assertEqual(a.await_count,2);self.assertEqual(b.await_count,1);self.assertEqual(c.await_count,1)
        self.assertEqual(a.await_args.kwargs['value'],'B')
        saved=inspect_checkpoint(cp.run_id,self.uid)
        self.assertEqual(saved['revisions'][1]['invalidated_steps'],[1])
        self.assertEqual(len([x for x in saved['attempts'] if x['step_key']=='step:1']),2)
    async def test_revision_invalidates_transitive_descendants_only(self):
        reservation,cp=self.create();a=AsyncMock(return_value={'value':'A'});b=AsyncMock(return_value={'value':'B'});c=AsyncMock(return_value={'value':'C'});d=AsyncMock(return_value={'value':'D'})
        executor=self.executor({'a':a,'b':b,'c':c,'d':d})
        plan=Plan(intent_id='test',steps=[node(1,'a'),node(2,'b'),node(3,'c'),node(4,'d',[1],{'value':'{{steps.1.result.value}}'})])
        await executor.execute(plan,'trace',checkpoint=cp);finish_turn(*reservation[:3],dict(helpers.RESULT))
        revised=plan.model_copy(deep=True);revised.steps[0].params_template={'value':'changed'}
        revise_plan(cp.run_id,self.uid,revised,1,'change A',['a','b','c','d'])
        reservation,cp=await self.resume(reservation,1)
        await executor.execute(revised,'trace',checkpoint=cp)
        self.assertEqual([a.await_count,b.await_count,c.await_count,d.await_count],[2,1,1,2])
    async def test_ready_node_starts_without_waiting_for_independent_slow_node(self):
        reservation,cp=self.create();a_started=asyncio.Event();release_c=asyncio.Event()
        async def b(**kw):return {'value':'B'}
        async def a(**kw):a_started.set();return {'value':'A'}
        async def c(**kw):await release_c.wait();return {'value':'C'}
        executor=self.executor({'a':a,'b':b,'c':c})
        plan=Plan(intent_id='test',steps=[node(1,'a',[2]),node(2,'b'),node(3,'c')])
        task=asyncio.create_task(executor.execute(plan,'trace',checkpoint=cp))
        await asyncio.wait_for(a_started.wait(),2)
        self.assertFalse(task.done());release_c.set();await task
    async def test_cancel_preserves_completed_parallel_branch_and_resume(self):
        reservation,cp=self.create();release_a=asyncio.Event();a_calls=0;b_calls=0
        async def a(**kw):
            nonlocal a_calls
            a_calls+=1
            if a_calls==1:await release_a.wait()
            return {'value':'A'}
        async def b(**kw):
            nonlocal b_calls
            b_calls+=1;return {'value':'B'}
        executor=self.executor({'a':a,'b':b});plan=Plan(intent_id='test',steps=[node(1,'a'),node(2,'b')])
        task=asyncio.create_task(executor.execute(plan,'trace',checkpoint=cp))
        for _ in range(200):
            with self.factory() as db:
                row=db.get(StepCheckpoint,(cp.run_id,1,'step:2'))
                saved=bool(row and row.status=='succeeded')
            if saved:break
            await asyncio.sleep(.005)
        self.assertTrue(saved);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        finish_turn(*reservation[:3],{**helpers.RESULT,'success':False,'run_state':'cancelled'})
        reservation,cp=await self.resume(reservation,1)
        self.assertEqual((await executor.execute(plan,'trace',checkpoint=cp)).status,'completed')
        self.assertEqual((a_calls,b_calls),(2,1))
    async def test_startup_marks_unknown_attempt_and_fences_old_writer(self):
        reservation,old=self.create()
        plan=Plan(intent_id='test',steps=[node(1,'a')]);await old.ensure_plan(plan)
        attempt=await old.begin(1,'step:1','a',{'value':1},'hash',True)
        with self.factory() as db:recover_interrupted(db)
        with self.assertRaises(ServiceError):await old.end(attempt,'succeeded',{'step_id':1})
        self.assertFalse(finish_turn(*reservation[:3],dict(helpers.RESULT),generation=1))
        updated,cp=await self.resume(reservation,2)
        a=AsyncMock(return_value={'ok':True});executor=self.executor({'a':a})
        self.assertEqual((await executor.execute(plan,'trace',checkpoint=cp)).status,'completed')
        saved=inspect_checkpoint(cp.run_id,self.uid)
        self.assertEqual(saved['attempts'][0]['status'],'unknown')
    async def test_unknown_non_readonly_tool_is_not_replayed(self):
        reservation,cp=self.create();plan=Plan(intent_id='test',steps=[node(1,'unsafe')]);await cp.ensure_plan(plan)
        await cp.begin(1,'step:1','unsafe',{},'hash',False)
        with self.factory() as db:recover_interrupted(db)
        reservation,cp=await self.resume(reservation,2)
        calls=AsyncMock();calls.replay_safe=False
        executor=ReActExecutor({'unsafe':calls},FakeLLM());executor.MAX_RETRIES=0
        result=await executor.execute(plan,'trace',checkpoint=cp)
        calls.assert_not_awaited();self.assertEqual(result.status,'failed')
    async def test_revision_cycle_version_conflict_and_missing_edge_rejected(self):
        reservation,cp=self.create();plan=Plan(intent_id='test',steps=[node(1,'a'),node(2,'b')])
        await cp.ensure_plan(plan);finish_turn(*reservation[:3],dict(helpers.RESULT))
        bad=plan.model_copy(deep=True);bad.steps[0].depends_on=[2];bad.steps[1].depends_on=[1]
        with self.assertRaises(ServiceError):revise_plan(cp.run_id,self.uid,bad,1,'cycle',['a','b'])
        bad=plan.model_copy(deep=True);bad.steps[0].params_template={'value':'{{steps.2.result.value}}'}
        with self.assertRaises(ServiceError):revise_plan(cp.run_id,self.uid,bad,1,'missing edge',['a','b'])
        with self.assertRaises(ServiceError):revise_plan(cp.run_id,self.uid,plan,99,'stale version',['a','b'])
        self.assertEqual(inspect_checkpoint(cp.run_id,self.uid)['plan_version'],1)
    async def test_checkpoint_api_is_owner_scoped_and_resume_is_single_claim(self):
        reservation,cp=self.create();finish_turn(*reservation[:3],{**helpers.RESULT,'success':False})
        foreign=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver')
        await foreign.post('/auth/login',json={'username':'user2','password':'AgentUser2!'})
        self.assertEqual((await foreign.get('/runs/'+cp.run_id+'/checkpoint')).status_code,404)
        self.assertEqual((await foreign.post('/runs/'+cp.run_id+'/resume',json={'expected_generation':1})).status_code,404)
        entered=asyncio.Event();release=asyncio.Event()
        async def agent(**kw):entered.set();await release.wait();return dict(helpers.RESULT)
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            response=await self.http.post('/runs/'+cp.run_id+'/resume',json={'expected_generation':1})
            self.assertEqual(response.status_code,202,response.text);await entered.wait()
            self.assertEqual((await foreign.post('/runs/'+cp.run_id+'/resume',json={'expected_generation':1})).status_code,404)
            await foreign.aclose()
            self.assertEqual((await self.http.post('/runs/'+cp.run_id+'/resume',json={'expected_generation':1})).status_code,409)
            release.set();await asyncio.gather(*get_runtime().scheduler.tasks)
        self.assertEqual((await self.http.get('/runs/'+cp.run_id)).json()['state'],'succeeded')
    async def test_model_change_and_nonlatest_turn_block_resume(self):
        reservation,cp=self.create();finish_turn(*reservation[:3],{**helpers.RESULT,'success':False})
        with self.assertRaises(ServiceError):claim_resume(cp.run_id,self.uid,1,'changed')
        other=reserve_turn(self.uid,reservation[0],'later','model-a');finish_turn(*other[:3],dict(helpers.RESULT))
        with self.assertRaises(ServiceError) as error:await self.resume(reservation,1)
        self.assertEqual(error.exception.code,'RESUME_NOT_LATEST_TURN')
    async def test_real_chat_resume_reuses_search_and_route_checkpoints(self):
        from agent_system.utils.llm_client import LLMError
        class Model:
            model='model-a'
            def __init__(self):self.routes=0;self.reviews=0
            async def extract_structured(inner,prompt,schema,**kw):
                if 'sufficient' in schema.get('properties',{}):
                    inner.reviews+=1
                    if inner.reviews==1:raise LLMError('temporary outage','UPSTREAM_UNAVAILABLE',True)
                    return {'sufficient':True,'reason':'covered','next_query':''}
                inner.routes+=1
                return {'intent_id':'web_search','complexity':'simple','confidence':.99,'extracted_slots':{'query':'source'}}
            async def chat(inner,*args,**kw):return 'completed [1]'
            async def stream_chat(inner,*args,**kw):yield 'completed [1]'
        model=Model()
        async def search(**kw):return {'query':kw['query'],'results':[{'url':'https://example.com','title':'source','content':'evidence','score':1.0}]}
        with patch('agent_system.api.routes.OpenAIClient',return_value=model),patch('agent_system.tools.registry.WebSearchTool._run',side_effect=search) as tool:
            first=(await self.http.post('/chat',json={'user_input':'search source'})).json()
            self.assertTrue(first['degraded'],first)
            cp=(await self.http.get('/runs/'+first['run_id']+'/checkpoint')).json()
            self.assertTrue(any(s['key']=='stage:review:1' and s['status']=='failed' for s in cp['steps']))
            response=await self.http.post('/runs/'+first['run_id']+'/resume',json={'expected_generation':cp['generation']})
            self.assertEqual(response.status_code,202,response.text)
            await asyncio.gather(*get_runtime().scheduler.tasks)
            self.assertEqual(tool.await_count,1)
        self.assertEqual(model.routes,1);self.assertEqual(model.reviews,2)
        detail=(await self.http.get('/conversations/'+first['conversation_id'])).json()
        self.assertEqual(len(detail['messages']),2)
        self.assertEqual(detail['messages'][1]['content'],'completed [1]')
        self.assertTrue(detail['messages'][1]['result']['success'])
    async def test_graph_http_api_revises_and_locally_resumes(self):
        from collections import Counter
        class Model:
            model='model-a'
            async def chat(self,*args,**kw):return 'summary'
        async def search(**kw):
            if kw['query']=='broken':raise RuntimeError('permanent failure')
            return {'query':kw['query'],'results':[{'url':'https://example.com/'+kw['query'],'title':kw['query'],'content':'evidence','score':1.0}]}
        plan=Plan(intent_id='web_search',steps=[node(1,'web_search',params={'query':'broken'}),node(2,'web_search',params={'query':'b'}),node(3,'web_search',params={'query':'c'})])
        with patch('agent_system.api.routes.OpenAIClient',return_value=Model()),patch('agent_system.tools.registry.WebSearchTool._run',side_effect=search) as tool:
            response=await self.http.post('/runs/graph',json={'user_input':'compare sources','plan':plan.model_dump()})
            self.assertEqual(response.status_code,202,response.text)
            rid=response.json()['run_id'];await asyncio.gather(*get_runtime().scheduler.tasks)
            initial=(await self.http.get('/runs/'+rid+'/checkpoint')).json()
            self.assertEqual(initial['state'],'failed',initial)
            revised=plan.model_copy(deep=True);revised.steps[0].depends_on=[2];revised.steps[0].params_template={'query':'{{steps.2.result.query}}'}
            response=await self.http.post('/runs/'+rid+'/plan/revisions',json={'expected_version':1,'reason':'A requires B','plan':revised.model_dump()})
            self.assertEqual(response.status_code,200,response.text)
            response=await self.http.post('/runs/'+rid+'/resume',json={'expected_generation':1})
            self.assertEqual(response.status_code,202,response.text);await asyncio.gather(*get_runtime().scheduler.tasks)
            final=(await self.http.get('/runs/'+rid+'/checkpoint')).json()
            self.assertEqual(final['state'],'succeeded',final)
            self.assertEqual(Counter(a.kwargs['query'] for a in tool.await_args_list),{'broken':1,'b':2,'c':1})
            self.assertEqual(final['revisions'][-1]['invalidated_steps'],[1])
    async def test_hard_process_exit_then_new_process_resumes_only_unfinished_node(self):
        import os,subprocess
        reservation=reserve_turn(self.uid,None,'crash test','model-a',checkpoint_spec={'identity':'offline','mode':'graph'})
        rid=reservation[2]
        program=Path(self.directory.name)/'worker.py'
        program.write_text('''import asyncio,os,sys
from agent_system.runtime.checkpoints import Checkpoint,claim_resume
from agent_system.api.conversations import mark_running,recover_interrupted,finish_turn
from agent_system.db.session import db_session
from agent_system.db.models import RunCheckpoint,StepCheckpoint
from agent_system.core.executor import ReActExecutor
from agent_system.core.planner import Plan,PlanStep
from agent_system.utils.llm_client import BaseLLMClient
class Model(BaseLLMClient):
 async def chat(self,*a,**k):return 'unused'
 async def extract_structured(self,*a,**k):return {}
async def main():
 rid,uid,phase=sys.argv[1:]
 if phase=='first':
  generation=1;mark_running(rid,generation)
 else:
  with db_session() as db:recover_interrupted(db)
  with db_session() as db:g=db.get(RunCheckpoint,rid).generation
  reservation,generation,_=claim_resume(rid,uid,g,'offline');mark_running(rid,generation)
 cp=Checkpoint(rid,uid,generation)
 async def a(**kw):
  if phase=='first':
   for _ in range(200):
    with db_session() as db:
     row=db.get(StepCheckpoint,(rid,1,'step:2'));ready=row is not None and row.status=='succeeded'
    if ready:os._exit(23)
    await asyncio.sleep(.01)
   raise RuntimeError('B checkpoint did not commit')
  return {'value':'A'}
 async def b(**kw):
  if phase!='first':raise AssertionError('completed B must not run again')
  return {'value':'B'}
 a.replay_safe=b.replay_safe=True
 executor=ReActExecutor({'a':a,'b':b},Model());executor.MAX_RETRIES=0
 if phase=='first':plan=Plan(intent_id='test',steps=[PlanStep(step_id=1,tool_name='a',params_template={'value':1}),PlanStep(step_id=2,tool_name='b',params_template={'value':2})])
 else:plan,_=await cp.plan()
 result=await executor.execute(plan,rid,checkpoint=cp)
 assert result.status=='completed',result
 finish_turn(*reservation[:3],{'success':True,'answer':'restored'},generation=generation)
 print('RESTORED_WITH_B_REUSED')
asyncio.run(main())
''',encoding='utf-8')
        environment=os.environ.copy();environment.update(DEBUG='false',DATABASE_URL=str(self.engine.url),PYTHONPATH=str(ROOT/'.python_packages')+os.pathsep+str(ROOT))
        first=await asyncio.to_thread(subprocess.run,[sys.executable,str(program),rid,self.uid,'first'],env=environment,cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(first.returncode,23,first.stderr)
        with self.factory() as db:
            self.assertEqual(db.get(StepCheckpoint,(rid,1,'step:2')).status,'succeeded')
            self.assertEqual(db.get(StepCheckpoint,(rid,1,'step:1')).status,'running')
            self.assertEqual(db.query(StepAttempt).filter_by(run_id=rid,step_key='step:1').first().params,{'value':1})
        second=await asyncio.to_thread(subprocess.run,[sys.executable,str(program),rid,self.uid,'second'],env=environment,cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(second.returncode,0,second.stderr);self.assertIn('RESTORED_WITH_B_REUSED',second.stdout)
        with self.factory() as db:
            attempts=db.query(StepAttempt).filter_by(run_id=rid).all()
            self.assertEqual(sum(a.step_key=='step:2' for a in attempts),1)
            self.assertEqual(sum(a.step_key=='step:1' for a in attempts),2)
    async def test_attempt_budget_does_not_reset_on_resume(self):
        reservation,cp=self.create();calls=AsyncMock(side_effect=RuntimeError('permanent failure'))
        executor=self.executor({'a':calls});plan=Plan(intent_id='test',steps=[node(1,'a')])
        from agent_system.config.settings import Settings
        limit=Settings(_env_file=None,CHECKPOINT_MAX_STEP_ATTEMPTS=1)
        with patch('agent_system.runtime.checkpoints.get_settings',return_value=limit):
            await executor.execute(plan,'trace',checkpoint=cp);finish_turn(*reservation[:3],{**helpers.RESULT,'success':False})
            reservation,cp=await self.resume(reservation,1)
            self.assertEqual((await executor.execute(plan,'trace',checkpoint=cp)).status,'failed')
        self.assertEqual(calls.await_count,1)
    async def test_chat_search_budget_survives_revision_and_resume(self):
        class Model:
            model='model-a'
            async def extract_structured(self,prompt,schema,**kw):
                if 'sufficient' in schema.get('properties',{}):
                    number=prompt.split('"round": ')[-1].split(',')[0].strip()
                    return {'sufficient':False,'reason':'no evidence','next_query':'next '+number}
                return {'intent_id':'web_search','complexity':'simple','confidence':.99,'extracted_slots':{'query':'first'}}
            async def chat(self,*args,**kw):return 'no evidence'
        with patch('agent_system.api.routes.OpenAIClient',return_value=Model()),patch('agent_system.tools.registry.WebSearchTool._run',side_effect=RuntimeError('permanent failure')) as tool:
            response=await self.http.post('/chat',json={'user_input':'search'})
            initial=response.json();self.assertFalse(initial['success'],initial)
            rid=initial['run_id'];self.assertEqual(tool.await_count,3)
            cp=(await self.http.get('/runs/'+rid+'/checkpoint')).json()
            plan=cp['plan'];plan['steps'][0]['params_template']={'query':'repaired'}
            revised=await self.http.post('/runs/'+rid+'/plan/revisions',json={'expected_version':cp['plan_version'],'reason':'repair first query','plan':plan})
            self.assertEqual(revised.status_code,200,revised.text)
            resumed=await self.http.post('/runs/'+rid+'/resume',json={'expected_generation':cp['generation']})
            self.assertEqual(resumed.status_code,202,resumed.text);await asyncio.gather(*get_runtime().scheduler.tasks)
            self.assertEqual(tool.await_count,3,'Recovery must not reset the total search attempt budget')
    async def test_cancel_after_success_commit_preserves_success_and_cancellation(self):
        import threading
        reservation,cp=self.create();committed=threading.Event();release=threading.Event()
        original=Checkpoint._end
        def delayed(handle,*args):
            value=original(handle,*args)
            if value:
                committed.set();release.wait(2)
            return value
        async def operation():return 'saved answer'
        with patch.object(Checkpoint,'_end',delayed):
            task=asyncio.create_task(cp.stage('answer',{'question':'q'},operation))
            self.assertTrue(await asyncio.to_thread(committed.wait,2))
            task.cancel();release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        with self.factory() as db:
            node=db.get(StepCheckpoint,(cp.run_id,0,'stage:answer'))
            self.assertEqual(node.status,'succeeded');self.assertEqual(node.result,'saved answer')
