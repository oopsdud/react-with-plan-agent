"""Adversarial concurrency tests; fake providers, real SQLite and HTTP ownership checks."""
import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.python_packages'),str(ROOT)]
from agent_system.config.settings import Settings,get_settings
from agent_system.runtime.scheduler import Scheduler
from agent_system.runtime.resilience import DependencyGuard
from agent_system.runtime.errors import ServiceError
from agent_system.runtime.threads import ThreadRunner
from agent_system.runtime.process_lease import ProcessLease


def settings(**values):return Settings(_env_file=None,**values)

class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_user_limits_and_round_robin_fairness(self):
        scheduler=Scheduler(settings(AGENT_MAX_CONCURRENT=1,AGENT_MAX_QUEUED=4,USER_MAX_CONCURRENT=1,USER_MAX_QUEUED=2))
        first=scheduler.admit('a');await scheduler.wait(first)
        second=scheduler.admit('a');second_wait=asyncio.create_task(scheduler.wait(second))
        third=scheduler.admit('b');third_wait=asyncio.create_task(scheduler.wait(third))
        await asyncio.sleep(0)
        scheduler.release(first)
        await third_wait
        self.assertFalse(second_wait.done(),'User b must run before user a runs again')
        scheduler.release(third);await second_wait;scheduler.release(second)
        self.assertEqual(scheduler.snapshot()['active'],0)
    async def test_overload_and_rate_limits(self):
        scheduler=Scheduler(settings(AGENT_MAX_CONCURRENT=1,AGENT_MAX_QUEUED=1,USER_MAX_CONCURRENT=1,USER_MAX_QUEUED=0))
        a=scheduler.admit('a')
        with self.assertRaises(ServiceError) as error:scheduler.admit('a')
        self.assertEqual(error.exception.code,'USER_CAPACITY_EXCEEDED')
        b=scheduler.admit('b')
        with self.assertRaises(ServiceError) as error:scheduler.admit('c')
        self.assertEqual(error.exception.code,'QUEUE_FULL')
        scheduler.release(a);scheduler.release(b)
        scheduler=Scheduler(settings(USER_REQUESTS_PER_MINUTE=1))
        a=scheduler.admit('a');scheduler.release(a)
        with self.assertRaises(ServiceError) as error:scheduler.admit('a')
        self.assertEqual(error.exception.code,'USER_RATE_LIMITED')
    async def test_wait_timeout_does_not_consume_a_slot(self):
        scheduler=Scheduler(settings(AGENT_MAX_CONCURRENT=1,AGENT_QUEUE_TIMEOUT_SECONDS=.02))
        a=scheduler.admit('a');await scheduler.wait(a)
        b=scheduler.admit('b')
        with self.assertRaises(ServiceError) as error:await scheduler.wait(b)
        self.assertEqual(error.exception.code,'QUEUE_TIMEOUT')
        scheduler.release(b);scheduler.release(a)
        c=scheduler.admit('c');await scheduler.wait(c);scheduler.release(c)
    async def test_concurrent_work_is_bounded_and_user_scoped(self):
        scheduler=Scheduler(settings(AGENT_MAX_CONCURRENT=3,AGENT_MAX_QUEUED=40,USER_MAX_CONCURRENT=1,USER_MAX_QUEUED=12))
        active={};maximum=0
        async def run(owner):
            nonlocal maximum
            ticket=scheduler.admit(owner)
            try:
                await scheduler.wait(ticket)
                active[owner]=active.get(owner,0)+1
                self.assertEqual(active[owner],1)
                maximum=max(maximum,sum(active.values()))
                await asyncio.sleep(.002)
                active[owner]-=1
            finally:scheduler.release(ticket)
        await asyncio.gather(*(run(str(i%3)) for i in range(30)))
        self.assertEqual(maximum,3);self.assertEqual(scheduler.snapshot()['queued'],0)
    async def test_shutdown_rejects_and_cancels_then_releases(self):
        scheduler=Scheduler(settings(AGENT_SHUTDOWN_GRACE_SECONDS=0))
        ticket=scheduler.admit('a')
        async def work():
            try:await scheduler.wait(ticket);await asyncio.Event().wait()
            finally:scheduler.release(ticket)
        task=scheduler.track(asyncio.create_task(work()));await asyncio.sleep(0)
        await scheduler.close()
        self.assertTrue(task.cancelled());self.assertEqual(scheduler.snapshot()['active'],0)
        with self.assertRaises(ServiceError):scheduler.admit('a')

class CircuitTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_single_probe_and_recovery(self):
        guard=DependencyGuard(2,2,.01,.1)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                async with guard.call():raise RuntimeError('provider down')
        with self.assertRaises(ServiceError) as error:
            async with guard.call():self.fail('circuit must reject')
        self.assertEqual(error.exception.code,'CIRCUIT_OPEN')
        await asyncio.sleep(.015)
        entered=asyncio.Event();finish=asyncio.Event()
        async def probe():
            async with guard.call():entered.set();await finish.wait()
        task=asyncio.create_task(probe());await entered.wait()
        with self.assertRaises(ServiceError):
            async with guard.call():self.fail('only one probe')
        finish.set();await task
        self.assertEqual(guard.state,'closed');self.assertEqual(guard.inflight,0)
    async def test_nonretryable_errors_do_not_trip_and_slots_are_bounded(self):
        guard=DependencyGuard(1,1,30,.01)
        with self.assertRaises(ServiceError):
            async with guard.call():raise ServiceError('BAD_MODEL','bad model',422)
        self.assertEqual(guard.state,'closed')
        async with guard.call():
            with self.assertRaises(ServiceError) as error:
                async with guard.call():self.fail('must not exceed capacity')
            self.assertEqual(error.exception.code,'DEPENDENCY_BUSY')
    async def test_old_inflight_success_does_not_close_a_new_circuit(self):
        guard=DependencyGuard(2,1,30,.1)
        entered=asyncio.Event();release=asyncio.Event()
        async def old_call():
            async with guard.call():entered.set();await release.wait()
        old=asyncio.create_task(old_call());await entered.wait()
        with self.assertRaises(RuntimeError):
            async with guard.call():raise RuntimeError('down')
        release.set();await old
        self.assertEqual(guard.state,'open')
    def test_login_throttling_is_shared_across_ips_for_one_account(self):
        from agent_system.runtime.login_limit import LoginLimiter
        limiter=LoginLimiter()
        limiter.check('ip-a','user1',10,1)
        with self.assertRaises(ServiceError) as error:limiter.check('ip-b','user1',10,1)
        self.assertEqual(error.exception.code,'LOGIN_RATE_LIMITED')
        limiter.check('ip-b','user2',10,1)

    async def test_cancelled_thread_keeps_actual_thread_capacity(self):
        runner=ThreadRunner(1);entered=threading.Event();finish=threading.Event()
        def work():entered.set();finish.wait(2)
        task=asyncio.create_task(runner.run(work))
        await asyncio.to_thread(entered.wait,1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with self.assertRaises(ServiceError) as error:await runner.run(lambda:None)
        self.assertEqual(error.exception.code,'SEARCH_THREADS_BUSY')
        finish.set();await asyncio.sleep(.03)
        self.assertEqual(await runner.run(lambda:'ok'),'ok');runner.close()
    def test_process_lease_excludes_second_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            first=ProcessLease(Path(directory)/'lease').acquire()
            try:
                with self.assertRaises(RuntimeError):ProcessLease(Path(directory)/'lease').acquire()
            finally:first.release()
            second=ProcessLease(Path(directory)/'lease').acquire();second.release()

import httpx
import test_console as helpers
from agent_system.api.routes import app
from agent_system.api.auth import COOKIE
from agent_system.api.conversations import reserve_turn,finish_turn
from agent_system.db.models import AgentRun,Conversation
from agent_system.runtime import get_runtime

class ConcurrencyAPITests(unittest.IsolatedAsyncioTestCase):
    setUp=helpers.ConsoleTests.setUp
    tearDown=helpers.ConsoleTests.tearDown
    login=helpers.ConsoleTests.login
    async def asyncSetUp(self):
        self.runtime_config=patch('agent_system.runtime.get_settings',return_value=self.settings);self.runtime_config.start()
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',cookies=self.client.cookies)
    async def asyncTearDown(self):
        await self.http.aclose()
        await get_runtime().scheduler.close()
        get_runtime().search_threads.close()
        self.runtime_config.stop()
    async def test_same_conversation_race_and_foreign_cancel(self):
        self.settings.AGENT_MAX_CONCURRENT=2
        entered=asyncio.Event();release=asyncio.Event()
        async def agent(**kwargs):entered.set();await release.wait();return dict(helpers.RESULT)
        cid=(await self.http.post('/conversations',json={})).json()['id']
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            first=asyncio.create_task(self.http.post('/chat',json={'user_input':'private','conversation_id':cid}))
            await entered.wait()
            second=await self.http.post('/chat',json={'user_input':'race','conversation_id':cid})
            self.assertEqual(second.status_code,409,second.text)
            with self.factory() as db:run_id=db.query(AgentRun).filter_by(conversation_id=cid).first().id
            foreign=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver')
            await foreign.post('/auth/login',json={'username':'user2','password':'AgentUser2!'})
            self.assertEqual((await foreign.get('/runs/'+run_id)).status_code,404)
            self.assertEqual((await foreign.post('/runs/'+run_id+'/cancel')).status_code,404)
            self.assertEqual((await foreign.get('/operations')).status_code,403)
            await foreign.aclose()
            release.set();self.assertEqual((await first).status_code,200)
        self.assertEqual(get_runtime().scheduler.snapshot()['active'],0)
    async def test_queue_timeout_persists_and_releases_conversation(self):
        self.settings.AGENT_MAX_CONCURRENT=1;self.settings.AGENT_QUEUE_TIMEOUT_SECONDS=.04
        entered=asyncio.Event();release=asyncio.Event()
        async def agent(**kwargs):entered.set();await release.wait();return dict(helpers.RESULT)
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            first=asyncio.create_task(self.http.post('/chat',json={'user_input':'hold'}));await entered.wait()
            response=await self.http.post('/chat',json={'user_input':'queued'})
            self.assertEqual(response.status_code,200,response.text)
            data=response.json();self.assertEqual(data['error_info']['code'],'QUEUE_TIMEOUT')
            detail=(await self.http.get('/conversations/'+data['conversation_id'])).json()
            self.assertFalse(detail['running'])
            self.assertEqual((await self.http.get('/runs/'+data['run_id'])).json()['state'],'expired')
            release.set();await first
    async def test_cancel_running_and_pending_runs_release_slots(self):
        self.settings.AGENT_MAX_CONCURRENT=1
        entered=asyncio.Event()
        async def agent(**kwargs):entered.set();await asyncio.Event().wait()
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            first=asyncio.create_task(self.http.post('/chat',json={'user_input':'running'}));await entered.wait()
            second=asyncio.create_task(self.http.post('/chat',json={'user_input':'queued'}))
            for _ in range(100):
                with self.factory() as db:jobs=[(r.id,r.state) for r in db.query(AgentRun).all()]
                if len(jobs)==2:break
                await asyncio.sleep(.005)
            self.assertEqual(len(jobs),2)
            queued=next(r for r,s in jobs if s=='queued')
            await self.http.post('/runs/'+queued+'/cancel')
            self.assertEqual((await second).json()['run_state'],'cancelled')
            running=next(r for r,s in jobs if s=='running')
            await self.http.post('/runs/'+running+'/cancel')
            self.assertEqual((await first).json()['run_state'],'cancelled')
        self.assertEqual(get_runtime().scheduler.snapshot()['active'],0)
        self.assertEqual(get_runtime().scheduler.snapshot()['queued'],0)
    async def test_run_deadline_persists_and_next_run_can_start(self):
        self.settings.AGENT_RUN_TIMEOUT_SECONDS=.02
        async def agent(**kwargs):await asyncio.Event().wait()
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            value=(await self.http.post('/chat',json={'user_input':'timeout'})).json()
        self.assertEqual(value['error_info']['code'],'RUN_TIMEOUT')
        self.assertEqual(value['run_state'],'timed_out')
        with patch('agent_system.api.routes.run_agent',new_callable=AsyncMock,return_value=dict(helpers.RESULT)):
            response=await self.http.post('/chat',json={'user_input':'next','conversation_id':value['conversation_id']})
        self.assertTrue(response.json()['success'],response.text)
    async def test_stale_worker_cannot_overwrite_other_turn(self):
        uid=(await self.http.get('/auth/me')).json()['id']
        cid,mid,rid,_=reserve_turn(uid,None,'old','model-a')
        finish_turn(cid,mid,rid,dict(helpers.RESULT))
        new=reserve_turn(uid,cid,'new','model-a')
        self.assertFalse(finish_turn(cid,mid,rid,{**helpers.RESULT,'answer':'STALE OVERWRITE'}))
        detail=(await self.http.get('/conversations/'+cid)).json()
        self.assertTrue(detail['running']);self.assertEqual(detail['messages'][1]['content'],'answer')
        finish_turn(*new[:3],dict(helpers.RESULT))
    async def test_overload_has_retry_after_and_request_id(self):
        self.settings.AGENT_MAX_CONCURRENT=1;self.settings.AGENT_MAX_QUEUED=0
        entered=asyncio.Event();release=asyncio.Event()
        async def agent(**kwargs):entered.set();await release.wait();return dict(helpers.RESULT)
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            first=asyncio.create_task(self.http.post('/chat',json={'user_input':'hold'}));await entered.wait()
            response=await self.http.post('/chat',json={'user_input':'overload'})
            self.assertEqual(response.status_code,429,response.text)
            self.assertEqual(response.json()['error']['code'],'QUEUE_FULL')
            self.assertIn('retry-after',response.headers);self.assertIn('x-request-id',response.headers)
            release.set();await first
    async def test_history_pagination_and_context_are_bounded(self):
        from agent_system.db.models import ConversationMessage
        from agent_system.api.conversations import bounded_history
        cid=(await self.http.post('/conversations',json={})).json()['id']
        with self.factory() as db:
            db.add_all([ConversationMessage(conversation_id=cid,role='user' if i%2==0 else 'assistant',content=str(i),status='completed',created_at=i) for i in range(105)])
            db.commit()
        latest=(await self.http.get('/conversations/'+cid)).json()
        self.assertEqual(len(latest['messages']),100);self.assertTrue(latest['has_more'])
        older=(await self.http.get('/conversations/'+cid,params={'before_id':latest['next_before_id']})).json()
        self.assertEqual(len(older['messages']),5);self.assertFalse(older['has_more'])
        self.assertLess(older['messages'][-1]['id'],latest['messages'][0]['id'])
        history=bounded_history([{'role':'user','content':'x'*15000} for _ in range(50)])
        self.assertLessEqual(sum(len(row['content']) for row in history),get_settings().CONTEXT_MAX_CHARS)
        self.assertTrue(all(len(row['content'])<=12000 for row in history))

    async def test_idempotency_is_owner_scoped_and_conflicts_are_explicit(self):
        payload={'user_input':'one request','client_request_id':'request-123456'}
        with patch('agent_system.api.routes.run_agent',new_callable=AsyncMock,side_effect=lambda **kw:dict(helpers.RESULT)) as agent:
            original=(await self.http.post('/chat',json=payload)).json()
            duplicate=await self.http.post('/chat',json=payload)
            self.assertEqual(duplicate.status_code,409,duplicate.text)
            self.assertEqual(duplicate.json()['error']['code'],'DUPLICATE_REQUEST')
            self.assertEqual(duplicate.json()['error']['details']['run_id'],original['run_id'])
            conflict=await self.http.post('/chat',json={**payload,'user_input':'different'})
            self.assertEqual(conflict.json()['error']['code'],'IDEMPOTENCY_CONFLICT')
            self.assertEqual(agent.await_count,1)
            other=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver')
            await other.post('/auth/login',json={'username':'user2','password':'AgentUser2!'})
            self.assertEqual((await other.post('/chat',json=payload)).status_code,200)
            await other.aclose()
    async def test_slow_stream_detaches_without_losing_saved_result(self):
        self.settings.STREAM_BUFFER_EVENTS=4
        async def agent(**kw):
            for i in range(1000):await kw['on_event']({'type':'delta','content':str(i)})
            return dict(helpers.RESULT)
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            response=await self.http.post('/chat/stream',json={'user_input':'stream pressure'})
        import json
        events=[json.loads(line) for line in response.text.splitlines()]
        self.assertLessEqual(len(events),6)
        self.assertTrue(any(e.get('code')=='SLOW_CONSUMER' for e in events),events)
        await asyncio.gather(*get_runtime().scheduler.tasks)
        cid=events[0]['conversation_id']
        detail=(await self.http.get('/conversations/'+cid)).json()
        self.assertEqual(detail['messages'][1]['content'],'answer');self.assertFalse(detail['running'])
    async def test_storage_failure_is_not_reported_as_success(self):
        from sqlalchemy.exc import OperationalError
        with patch('agent_system.api.routes.run_agent',new_callable=AsyncMock,return_value=dict(helpers.RESULT)),patch('agent_system.api.routes.finish_turn',side_effect=OperationalError('private-statement',{},Exception('private-secret'))):
            response=await self.http.post('/chat',json={'user_input':'storage error'})
        self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(response.json()['error']['code'],'RESULT_PERSIST_FAILED')
        self.assertNotIn('private',response.text)
        self.assertEqual(get_runtime().scheduler.snapshot()['active'],0)
        self.assertEqual(get_runtime().persistence_failures,1)
    async def test_queued_run_keeps_original_model_credentials(self):
        self.settings.AGENT_MAX_CONCURRENT=1
        entered=asyncio.Event();release=asyncio.Event();keys=[]
        async def agent(**kw):
            keys.append(kw['llm_client'].api_key)
            if len(keys)==1:entered.set();await release.wait()
            return dict(helpers.RESULT)
        with patch('agent_system.api.routes.run_agent',side_effect=agent):
            first=asyncio.create_task(self.http.post('/chat',json={'user_input':'hold'}));await entered.wait()
            second=asyncio.create_task(self.http.post('/chat',json={'user_input':'keep original connection'}))
            for _ in range(100):
                with self.factory() as db:count=db.query(AgentRun).count()
                if count==2:break
                await asyncio.sleep(.005)
            self.settings.LLM_API_KEY='new-test-key'
            release.set();await asyncio.gather(first,second)
        self.assertEqual(keys,['test-key','test-key'])

class CatalogConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_flight_and_bounded_stale_cache_never_cross_credentials(self):
        from agent_system.api.model_settings import available_models
        config=settings(LLM_API_KEY='first-test-key',LLM_BASE_URL='https://provider.example/v1')
        async def discover(*args):await asyncio.sleep(.01);return ['model-a']
        with patch('agent_system.api.model_settings.get_settings',return_value=config),patch('agent_system.api.model_settings.discover_models',side_effect=discover) as call:
            results=await asyncio.gather(*(available_models() for _ in range(20)))
            self.assertEqual(call.await_count,1);self.assertTrue(all(value==['model-a'] for value in results))
            runtime=get_runtime();key=next(iter(runtime.catalog));_,values=runtime.catalog[key];runtime.catalog[key]=(time.monotonic()-65,values)
            with patch('agent_system.api.model_settings.discover_models',side_effect=ServiceError('CIRCUIT_OPEN','down',503,True)):
                self.assertEqual(await available_models(),['model-a'])
                config.LLM_API_KEY='different-key'
                with self.assertRaises(ServiceError):await available_models()
