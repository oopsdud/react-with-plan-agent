"""Configuration persistence and model discovery never call paid services."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.python_packages'))
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
import httpx
from agent_system.api.routes import app
from agent_system.config.settings import Settings,get_settings
from agent_system.config.model_config import load_model_config,save_model_config

class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'data/model-config.json'
        self.location=patch('agent_system.config.model_config.CONFIG_PATH',self.path)
        self.location.start()
        get_settings.cache_clear()
        from agent_system.api.auth import require_user,require_admin
        from types import SimpleNamespace
        admin=SimpleNamespace(id='test',username='user1',is_admin=True)
        app.dependency_overrides[require_user]=lambda:admin
        app.dependency_overrides[require_admin]=lambda:admin
        self.client=TestClient(app)
        self.connection={'base_url':'https://provider.example/v1','api_key':'test-private-key'}
    def tearDown(self):
        app.dependency_overrides.clear()
        self.location.stop()
        get_settings.cache_clear()
        self.directory.cleanup()
    def test_discover_save_read_and_hot_apply(self):
        with patch('agent_system.api.model_settings.discover_models',new_callable=AsyncMock,return_value=['model-b','model-a']) as discover:
            result=self.client.post('/config/model/models',json=self.connection)
            self.assertEqual(result.json()['models'],['model-b','model-a'])
            self.assertFalse(self.path.exists(),'Discovery must not persist credentials')
            result=self.client.put('/config/model',json={**self.connection,'model':'model-a','json_mode':False})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.headers['cache-control'],'no-store')
            self.assertNotIn('test-private-key',result.text)
            self.assertTrue(result.json()['api_key_configured'])
            self.assertEqual(load_model_config()['LLM_API_KEY'],'test-private-key')
            self.assertEqual(get_settings().LLM_MODEL,'model-a')
            self.assertFalse(get_settings().LLM_JSON_MODE)
            # A new cache read simulates the next process reading the file.
            get_settings.cache_clear()
            self.assertEqual(get_settings().LLM_MODEL,'model-a')
            public=self.client.get('/config/model')
            self.assertNotIn('test-private-key',public.text)
            self.assertEqual(public.json()['model'],'model-a')
            self.assertEqual(self.client.get('/runtime').json()['llm_model'],'model-a')
            self.client.post('/config/model/models',json={'base_url':self.connection['base_url']})
            self.assertEqual(discover.await_args.args[1],'test-private-key')
    def test_changed_provider_never_reuses_saved_secret(self):
        save_model_config({'LLM_BASE_URL':'https://provider.example/v1','LLM_API_KEY':'secret-original','LLM_MODEL':'model-a'})
        get_settings.cache_clear()
        with patch('agent_system.api.model_settings.discover_models',new_callable=AsyncMock) as discover:
            result=self.client.post('/config/model/models',json={'base_url':'https://other.example/v1'})
            self.assertEqual(result.status_code,422)
            discover.assert_not_awaited()
    def test_unknown_model_and_invalid_url_do_not_modify_file(self):
        with patch('agent_system.api.model_settings.discover_models',new_callable=AsyncMock,return_value=['valid']):
            result=self.client.put('/config/model',json={**self.connection,'model':'unknown'})
            self.assertEqual(result.status_code,422)
            self.assertFalse(self.path.exists())
        for url in ['file:///tmp','https://user:password@provider.example/v1','https://provider.example/v1?key=secret']:
            result=self.client.post('/config/model/models',json={'base_url':url,'api_key':'test'})
            self.assertEqual(result.status_code,422)
    def test_write_failure_is_reported_without_exposing_key(self):
        with patch('agent_system.api.model_settings.discover_models',new_callable=AsyncMock,return_value=['a']),patch('agent_system.api.model_settings.save_model_config',side_effect=OSError('secret')):
            result=self.client.put('/config/model',json={**self.connection,'model':'a'})
        self.assertEqual(result.status_code,500)
        self.assertNotIn('test-private-key',result.text)
        self.assertNotIn('secret',result.text)

class ModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_models_http_shape_deduplication_and_errors(self):
        from agent_system.api.model_settings import discover_models
        real_client=httpx.AsyncClient
        def handler(request):
            self.assertEqual(request.url.path,'/v1/models')
            self.assertEqual(request.headers['Authorization'],'Bearer private-key')
            return httpx.Response(200,json={'data':[{'id':'b'},{'id':'a'},{'id':'b'},{'bad':'x'}]})
        with patch('agent_system.api.model_settings.httpx.AsyncClient',side_effect=lambda **kwargs:real_client(transport=httpx.MockTransport(handler),**kwargs)):
            self.assertEqual(await discover_models('https://provider.example/v1','private-key'),['a','b'])
        for status,payload in [(401,{'error':'private-key'}),(200,{'wrong':'shape'})]:
            with patch('agent_system.api.model_settings.httpx.AsyncClient',side_effect=lambda **kwargs:real_client(transport=httpx.MockTransport(lambda request:httpx.Response(status,json=payload)),**kwargs)):
                with self.assertRaises(Exception) as caught:await discover_models('https://provider.example/v1','private-key')
                self.assertNotIn('private-key',str(caught.exception))
