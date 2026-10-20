"""Regression coverage for execution correctness; all model calls are offline."""
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.python_packages'))
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from agent_system.core.agent import AgentCore
from agent_system.core.executor import ReActExecutor
from agent_system.core.intent_router import IntentDecision
from agent_system.core.planner import Plan, PlanStep, SkeletonPlanner, PlanningError
from agent_system.tools.registry import BaseTool, get_tool_registry
from agent_system.utils.embedding_client import MockEmbeddingClient
from agent_system.utils.llm_client import MockLLMClient, OpenAIClient


def step(number, tool, dependencies=(), critical=True, params=None):
    return PlanStep(step_id=number, tool_name=tool, depends_on=list(dependencies),
                    is_critical=critical, params_template=params or {})


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def executor(self, tools):
        executor = ReActExecutor(tools, MockLLMClient())
        executor.MAX_RETRIES = 0
        return executor

    async def test_critical_failure_allows_independent_later_branches(self):
        failed = AsyncMock(side_effect=RuntimeError('permanent failure'))
        good = AsyncMock(return_value={})
        later = AsyncMock(return_value={})
        blocked = AsyncMock()
        plan = Plan(intent_id='test', steps=[step(1, 'failed'), step(2, 'good'), step(3, 'later', [2]), step(4, 'blocked', [1])])
        result = await self.executor({'failed': failed, 'good': good, 'later': later, 'blocked': blocked}).execute(plan, 'test')
        self.assertEqual(result.status, 'failed')
        later.assert_awaited_once()
        blocked.assert_not_awaited()
        self.assertEqual(next(r for r in result.step_results if r.step_id == 4).status, 'skipped')

    async def test_failed_dependency_does_not_skip_unrelated_same_batch(self):
        optional = AsyncMock(side_effect=RuntimeError('permanent failure'))
        good = AsyncMock(return_value={})
        blocked = AsyncMock()
        independent = AsyncMock(return_value={})
        plan = Plan(intent_id='test', steps=[step(1, 'optional', critical=False), step(2, 'good'),
            step(3, 'blocked', [1], critical=False), step(4, 'independent', [2])])
        result = await self.executor({'optional': optional, 'good': good, 'blocked': blocked,
                                     'independent': independent}).execute(plan, 'test')
        independent.assert_awaited_once()
        blocked.assert_not_awaited()
        self.assertEqual(result.status, 'completed')

    async def test_critical_skipped_dependency_is_not_success(self):
        plan = Plan(intent_id='test', steps=[step(1, 'bad', critical=False), step(2, 'next', [1])])
        executor = self.executor({'bad': AsyncMock(side_effect=RuntimeError('bad')), 'next': AsyncMock()})
        result = await executor.execute(plan, 'test')
        self.assertEqual(result.status, 'failed')

    async def test_invalid_graphs_never_call_tools(self):
        tool = AsyncMock()
        for steps in [[], [step(1, 'a'), step(1, 'a')], [step(1, 'a', [2])],
                      [step(1, 'a', [2]), step(2, 'a', [1])], [step(1, 'a'), step(2, 'a', [1, 1])]]:
            result = await self.executor({'a': tool}).execute(Plan(intent_id='test', steps=steps), 'test')
            self.assertEqual(result.status, 'failed')
        tool.assert_not_awaited()

    async def test_templates_preserve_nested_types_and_mixed_text(self):
        executor = self.executor({})
        params = executor._render_template({
            'ids': '{{slots.ids}}', 'nested': ['{{slots.ids}}', {'x': '{{slots.name}}'}],
            'text': '{{slots.name}} / {{slots.name}}'
        }, {}, {'ids': ['u1'], 'name': '张三'})
        self.assertEqual(params['ids'], ['u1'])
        self.assertEqual(params['nested'], [['u1'], {'x': '张三'}])
        self.assertEqual(params['text'], '张三 / 张三')
        with self.assertRaises(ValueError):
            executor._render_template({'missing': '{{slots.missing}}'}, {}, {})

    async def test_planner_rejects_partially_invalid_model_output(self):
        llm = MockLLMClient()
        llm.extract_structured = AsyncMock(return_value={'skeleton': ['query_user', 'not_registered']})
        planner = SkeletonPlanner(llm, MockEmbeddingClient())
        with self.assertRaises(PlanningError):
            await planner.plan('test', 'test', {})

    async def test_tool_timeout_is_enforced(self):
        import asyncio
        class SlowTool(BaseTool):
            async def _run(self):
                await asyncio.sleep(1)
        tool = SlowTool('slow', 'slow')
        tool.timeout_seconds = .01
        result = await tool.run()
        self.assertFalse(result.success)
        self.assertIn('timeout', result.error)

    async def test_tool_validation_and_notification_contract(self):
        registry = get_tool_registry()
        invalid = await registry.get('query_user').run(names='张三')
        self.assertFalse(invalid.success)
        users = await registry.get('query_user').run(names=['张三'])
        self.assertEqual(list(users.data['users']), ['u1'])
        notification = await registry.get('send_notification').run(recipients=['u1'], contents={'u1': 'hello'})
        self.assertTrue(notification.success)
        self.assertEqual(notification.data['sent'], 1)
        reminder = await registry.get('create_reminder').run(time='2026-10-21T15:00:00+08:00', content='meeting')
        self.assertTrue(reminder.success)

    async def test_retry_count_and_original_plan_are_preserved(self):
        call = AsyncMock(side_effect=[RuntimeError('temporary network error'), {'ok': True}])
        executor = self.executor({'a': call})
        executor.MAX_RETRIES = 1
        original = Plan(intent_id='test', steps=[step(1, 'a')])
        with patch('agent_system.core.executor.asyncio.sleep', new_callable=AsyncMock):
            result = await executor.execute(original, 'test')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.step_results[0].retry_count, 1)
        self.assertEqual(original.steps[0].params_template, {})

    async def test_permission_failure_does_not_switch_tools(self):
        executor = self.executor({'send_notification': AsyncMock(side_effect=RuntimeError('permission forbidden')),
                                  'send_wechat': AsyncMock()})
        result = await executor.execute(Plan(intent_id='test', steps=[step(1, 'send_notification')]), 'test')
        self.assertEqual(result.status, 'failed')
        executor.tools['send_wechat'].assert_not_awaited()

    async def test_full_template_and_optional_memory_failure(self):
        agent = AgentCore.__new__(AgentCore)
        agent.tool_registry = get_tool_registry()
        agent.intent_router = SimpleNamespace(
            load_intents_from_db=Mock(),
            route=AsyncMock(return_value=IntentDecision(intent_id='notify_event', complexity='complex', confidence=.9,
                extracted_slots={'person': ['张三'], 'date': '2026-10-21', 'channel': 'email'})))
        agent.planner = SkeletonPlanner(MockLLMClient(), MockEmbeddingClient())
        agent.executor = self.executor({name: agent._create_tool_wrapper(name) for name in agent.tool_registry.list_tools()})
        agent.episodic_memory = SimpleNamespace(
            get_recommended_skeleton=AsyncMock(side_effect=RuntimeError('memory offline')),
            store_episode=AsyncMock(side_effect=RuntimeError('memory offline')))
        with patch('agent_system.core.agent.db_session', return_value=nullcontext(Mock())):
            result = await agent.run('通知张三明天开会', 'test_user')
        self.assertTrue(result['success'], result['error'])
        self.assertTrue(all(r['status'] == 'succeeded' for r in result['steps']))
        self.assertEqual(result['steps'][3]['result']['sent'], 1)
        self.assertEqual(result['steps'][3]['params']['recipients'], ['u1'])
        agent.intent_router.load_intents_from_db.assert_called_once()


class ConfigurationTests(unittest.TestCase):
    def test_configured_model_is_used(self):
        with patch('agent_system.utils.llm_client.settings') as settings:
            settings.LLM_MODEL = 'configured-model'
            settings.LLM_API_KEY = 'test-key'
            self.assertEqual(OpenAIClient().model, 'configured-model')

    def test_fresh_sqlite_directory_created_at_startup(self):
        from agent_system.db.session import init_db
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested' / 'agent.db'
            engine = create_engine('sqlite:///' + path.as_posix())
            try:
                with patch('agent_system.db.session.engine', engine):
                    init_db()
                self.assertTrue(path.is_file())
            finally:
                engine.dispose()


if __name__ == '__main__':
    unittest.main()
