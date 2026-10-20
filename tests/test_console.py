"""Contract smoke tests; no actual LLM or business operations are called."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".python_packages"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from agent_system.api.routes import app
from agent_system.utils.llm_client import MockLLMClient


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        # No lifespan: smoke tests should not mutate the user's database.
        self.client = TestClient(app)

    def test_page_and_module_resources(self):
        response = self.client.get('/console/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Agent Studio', response.text)
        for name in ['styles.css', 'api.mjs', 'app.mjs']:
            response = self.client.get('/console/' + name)
            self.assertEqual(response.status_code, 200)
            if name.endswith('.mjs'):
                self.assertTrue('javascript' in response.headers['content-type'])
        self.assertEqual(self.client.get('/console/../data/agent.db').status_code, 404)

    def test_runtime_contains_no_credentials(self):
        with patch('agent_system.utils.llm_client.get_llm_client', return_value=MockLLMClient()):
            response = self.client.get('/runtime')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['llm_mode'], 'mock')
        self.assertEqual(data['tools_mode'], 'mock')
        self.assertFalse(data['streaming'])
        self.assertFalse(any('key' in key.lower() or 'secret' in key.lower() for key in data))

    def test_contract_and_existing_routes(self):
        self.assertEqual(self.client.get('/').json()['name'], 'Agent System')
        self.assertEqual(self.client.get('/health').json()['status'], 'healthy')
        names = self.client.get('/tools').json()
        self.assertEqual(names['count'], len(names['tools']))
        self.assertEqual(self.client.get('/tools/query_user').json()['name'], 'query_user')
        self.assertEqual(self.client.get('/tools/missing_tool').status_code, 404)
        self.assertEqual(self.client.post('/chat', json={}).status_code, 422)
        self.assertEqual(self.client.post('/intent', json={}).status_code, 422)
        schema = self.client.get('/openapi.json').json()
        for endpoint in ['/chat', '/intent', '/runtime', '/tools', '/tools/{tool_name}']:
            self.assertIn(endpoint, schema['paths'])
        snapshot = __import__('json').loads((ROOT / 'docs/openapi.json').read_text(encoding='utf-8'))
        self.assertEqual(schema, snapshot)

    def test_chat_forwards_history_and_preserves_business_failure(self):
        result = {
            'trace_id': 'test_run', 'success': False, 'answer': '执行失败',
            'intent': None, 'steps': [], 'latency_ms': 10, 'error': 'test error',
        }
        payload = {
            'user_input': '查询张三', 'user_id': 'test_user',
            'session_history': [{'role': 'user', 'content': '你好'}],
        }
        with patch('agent_system.api.routes.run_agent', new_callable=AsyncMock, return_value=result) as run:
            response = self.client.post('/chat', json=payload)
            run.assert_awaited_once_with(**payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)

    def test_chat_service_exception_is_http_error(self):
        with patch('agent_system.api.routes.run_agent', new_callable=AsyncMock, side_effect=RuntimeError('test failure')):
            response = self.client.post('/chat', json={'user_input': 'test'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['detail'], 'test failure')


if __name__ == '__main__':
    unittest.main()
