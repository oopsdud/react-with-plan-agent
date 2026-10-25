"""Offline coverage for keyless search adapters and result normalization."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.python_packages'))
sys.path.insert(0,str(ROOT))
import httpx
from agent_system.config.settings import Settings
from agent_system.tools.registry import WebSearchTool,ToolRegistry
from agent_system.tools.web_search import normalize_results,search_configured
from agent_system.utils.llm_client import OpenAIClient,LLMError

class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent_system.runtime import get_runtime
        from agent_system.runtime.resilience import GuardRegistry
        # Adapter errors are tested independently; circuit transitions have separate tests.
        get_runtime().guards=GuardRegistry(Settings(_env_file=None,CIRCUIT_FAILURE_THRESHOLD=100))
    async def asyncTearDown(self):
        from agent_system.runtime import get_runtime
        get_runtime().search_threads.close()

    def settings(self,**kwargs):return Settings(_env_file=None,**kwargs)
    async def test_ddgs_needs_no_key_and_preserves_shared_contract(self):
        factory=MagicMock()
        client=factory.return_value.__enter__.return_value
        client.text.return_value=[{'href':'https://vuejs.org','title':'Vue','body':'Types'},
            {'href':'https://vuejs.org','title':'duplicate'}, {'href':'javascript:alert(1)'}]
        settings=self.settings(SEARCH_PROVIDER='ddgs',DDGS_BACKEND='duckduckgo')
        with patch('agent_system.tools.registry.get_settings',return_value=settings),patch('ddgs.DDGS',factory):
            result=await WebSearchTool().run(query='Vue')
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['provider'],'ddgs')
        self.assertEqual(result.data['count'],1)
        self.assertEqual(result.data['results'][0],{'title':'Vue','url':'https://vuejs.org','content':'Types','score':None})
        client.text.assert_called_once_with('Vue',max_results=5,backend='duckduckgo',region='us-en',safesearch='moderate')
        self.assertEqual(ToolRegistry().list_tools(),['web_search'])
    async def test_ddgs_errors_are_not_empty_results_or_secret_leaks(self):
        from ddgs.exceptions import DDGSException,TimeoutException,RatelimitException
        for error,label in [(DDGSException('blocked secret-proxy'),'failed'),(TimeoutException('secret-proxy'),'timeout'),(RatelimitException('secret-proxy'),'rate limit')]:
            with patch('ddgs.DDGS',side_effect=error):
                result=await WebSearchTool().run(query='x')
            self.assertFalse(result.success)
            self.assertIn(label,result.error)
            self.assertNotIn('secret-proxy',result.error)
        with patch('ddgs.DDGS',side_effect=DDGSException('No results found.')):
            result=await WebSearchTool().run(query='x')
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['results'],[])
    async def test_searxng_http_contract_without_credentials(self):
        def handler(request):
            self.assertEqual(request.method,'GET')
            self.assertEqual(request.url.path,'/search')
            self.assertEqual(request.url.params['q'],'Vue')
            self.assertEqual(request.url.params['format'],'json')
            self.assertNotIn('Authorization',request.headers)
            return httpx.Response(200,json={'results':[{'url':'https://vuejs.org','title':'Vue','content':'Types','score':.9}]})
        settings=self.settings(SEARCH_PROVIDER='searxng',SEARXNG_BASE_URL='http://127.0.0.1:8088')
        with patch('agent_system.tools.registry.get_settings',return_value=settings):
            result=await WebSearchTool(httpx.MockTransport(handler)).run(query='Vue')
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['provider'],'searxng')
    async def test_searxng_errors_and_empty_results_are_distinct(self):
        settings=self.settings(SEARCH_PROVIDER='searxng',SEARXNG_BASE_URL='http://127.0.0.1:8088')
        for status,payload,label in [(403,{'error':'secret'},'403'),(429,{},'rate limit'),(503,{},'temporary'),(200,{'wrong':[]},'invalid'),(200,{'results':[],'unresponsive_engines':[['bing','timeout']]},'unavailable')]:
            with patch('agent_system.tools.registry.get_settings',return_value=settings):
                result=await WebSearchTool(httpx.MockTransport(lambda request:httpx.Response(status,json=payload))).run(query='test')
            self.assertFalse(result.success)
            self.assertIn(label,result.error)
            self.assertNotIn('secret',result.error)
        with patch('agent_system.tools.registry.get_settings',return_value=settings):
            result=await WebSearchTool(httpx.MockTransport(lambda request:httpx.Response(200,json={'results':[]}))).run(query='test')
        self.assertTrue(result.success,result.error)
    async def test_validation_and_readiness(self):
        with patch('agent_system.tools.web_search.ddgs_search') as search:
            for params in [{'query':''},{'query':' '},{'query':['x']},{'query':'x','max_results':11},{'query':'x','extra':'bad'}]:
                self.assertFalse((await WebSearchTool().run(**params)).success)
            search.assert_not_called()
        for url in [None,'file:///tmp','https://user:pass@example.org','https://example.org?q=secret']:
            settings=self.settings(SEARCH_PROVIDER='searxng',SEARXNG_BASE_URL=url)
            self.assertFalse(search_configured(settings))
            with patch('agent_system.tools.registry.get_settings',return_value=settings):
                result=await WebSearchTool().run(query='test')
            self.assertFalse(result.success)
        self.assertTrue(search_configured(self.settings(SEARCH_PROVIDER='ddgs')))
        with patch('agent_system.tools.web_search.importlib.util.find_spec',return_value=None):
            self.assertFalse(search_configured(self.settings(SEARCH_PROVIDER='ddgs')))
    async def test_normalization_limits_and_invalid_provider_results(self):
        rows=[{'url':'https://a.example','score':float('nan'),'content':'x'*6000},
            {'url':'https://user:pass@bad.example'}, {'url':'https://b.example'}]
        result=normalize_results(rows,1)
        self.assertEqual(len(result),1)
        self.assertEqual(len(result[0]['content']),5000)
        self.assertIsNone(result[0]['score'])
        for value in [{},[None],[{'url':'javascript:alert(1)'}]]:
            with self.assertRaises(RuntimeError):normalize_results(value,5)
    async def test_model_missing_credentials_does_not_fall_back(self):
        with patch('agent_system.utils.llm_client.get_settings',return_value=self.settings(LLM_API_KEY='YOUR_LLM_API_KEY')):
            with self.assertRaises(LLMError):OpenAIClient()
