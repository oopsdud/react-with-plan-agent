import sys
import unittest
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.python_packages'),str(ROOT)]
from agent_system.core.agent import AgentCore
from agent_system.core.intent_router import IntentDecision
from agent_system.tools.registry import get_tool_registry

class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self,reviews,results=None,intent='web_search',stream=False,failure=False):
        llm=SimpleNamespace(model='test',extract_structured=AsyncMock(side_effect=reviews),chat=AsyncMock(return_value='grounded [1]'))
        async def chunks(messages):
            for s in ['grounded ','[1]']:yield s
        llm.stream_chat=chunks
        agent=AgentCore(tool_registry=get_tool_registry(),llm_client=llm)
        agent.intent_router=SimpleNamespace(load_intents_from_db=Mock(),route=AsyncMock(return_value=IntentDecision(intent_id=intent,complexity='simple',confidence=.9,extracted_slots={'query':'first query'})))
        rows=results if results is not None else [{'title':'source','url':'https://example.com','content':'evidence','score':1.0}]
        events=[]
        async def emit(event):events.append(event)
        with patch('agent_system.core.agent.db_session',return_value=nullcontext(Mock())),patch('agent_system.tools.registry.WebSearchTool._run',new_callable=AsyncMock,return_value={'results':rows},side_effect=RuntimeError('network failure') if failure else None) as tool:
            result=await agent.run('original question','owner',[{'role':'user','content':'prior'}],emit if stream else None)
        return result,tool,llm,events
    async def test_evidence_success_stops_before_budget(self):
        result,tool,llm,_=await self.run_case([{'sufficient':False,'reason':'missing detail','next_query':'refined query'},{'sufficient':True,'reason':'covered'}])
        self.assertTrue(result['success']);self.assertTrue(result['evidence_sufficient'])
        self.assertEqual(tool.await_count,2)
        self.assertEqual(result['steps'][1]['params']['query'],'refined query')
        self.assertIn('original question',llm.extract_structured.await_args.args[0])
    async def test_three_round_limit_and_stream_prefix(self):
        reviews=[{'sufficient':False,'reason':'not enough','next_query':f'next {i}'} for i in range(3)]
        result,tool,_,events=await self.run_case(reviews,stream=True)
        self.assertEqual(tool.await_count,3);self.assertFalse(result['evidence_sufficient'])
        self.assertTrue(result['answer'].startswith('检索证据仍不足'))
        self.assertEqual(''.join(e['content'] for e in events if e['type']=='delta'),result['answer'])
    async def test_duplicate_query_does_not_repeat(self):
        result,tool,_,_=await self.run_case([{'sufficient':False,'next_query':' FIRST QUERY '}])
        self.assertEqual(tool.await_count,1);self.assertFalse(result['evidence_sufficient'])
    async def test_empty_results_cannot_be_sufficient(self):
        result,tool,_,_=await self.run_case([{'sufficient':True}],results=[])
        self.assertFalse(result['evidence_sufficient']);self.assertEqual(result['sources'],[])
        self.assertIn('没有获得',result['answer'])
    async def test_failed_searches_are_bounded_and_reported(self):
        result,tool,_,_=await self.run_case([{'sufficient':False,'next_query':f'query {i}'} for i in range(3)],failure=True)
        self.assertEqual(tool.await_count,3);self.assertFalse(result['success'])
        self.assertTrue(result['error']);self.assertFalse(result['evidence_sufficient'])
    async def test_review_service_failure_degrades_to_unverified_sources(self):
        from agent_system.utils.llm_client import LLMError
        result,tool,llm,_=await self.run_case([LLMError('provider down','UPSTREAM_UNAVAILABLE',True)])
        self.assertTrue(result['success']);self.assertTrue(result['degraded'])
        self.assertFalse(result['evidence_sufficient'])
        self.assertIn('尚未完成核验',result['answer'])
        self.assertEqual(result['error_info']['code'],'UPSTREAM_UNAVAILABLE')
        llm.chat.assert_not_awaited()

    async def test_general_chat_does_not_search(self):
        result,tool,llm,_=await self.run_case([],intent='general')
        tool.assert_not_awaited();llm.extract_structured.assert_not_awaited()
        self.assertTrue(result['success']);self.assertIsNone(result['evidence_sufficient'])
