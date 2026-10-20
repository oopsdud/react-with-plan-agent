import test from 'node:test';
import assert from 'node:assert/strict';
import { AgentAPI, APIError } from '../frontend/api.mjs';

test('POST payload, headers and business failure survive transport', async () => {
  let call;
  const api = new AgentAPI('http://localhost:8000/', { fetchImpl: async (url, options) => {
    call = { url, options }; return new Response(JSON.stringify({ success: false, steps: [] }));
  } });
  const payload = { user_input: '查询张三', user_id: 'u1', session_history: [] };
  assert.equal((await api.chat(payload)).success, false);
  assert.equal(call.url, 'http://localhost:8000/chat');
  assert.equal(call.options.method, 'POST');
  assert.deepEqual(JSON.parse(call.options.body), payload);
  assert.equal(call.options.credentials, 'omit');
  assert.equal(call.options.headers['Content-Type'], 'application/json');
});
test('GET encodes tool names and has no body', async () => {
  const api = new AgentAPI('', { fetchImpl: async (url, options) => {
    assert.equal(url, '/tools/a%2Fb%20%E4%B8%AD'); assert.equal(options.body, undefined);
    return new Response('{}');
  } });
  await api.tool('a/b 中');
});
test('preserves validation errors', async () => {
  const detail = [{ loc: ['body', 'user_input'], msg: 'required' }];
  const api = new AgentAPI('', { fetchImpl: async () => new Response(JSON.stringify({ detail }), { status: 422 }) });
  await assert.rejects(api.chat({}), error => error instanceof APIError && error.status === 422 && JSON.stringify(error.detail) === JSON.stringify(detail));
});
test('handles non-JSON and network failures', async () => {
  await assert.rejects(new AgentAPI('', { fetchImpl: async () => new Response('<html>bad gateway</html>', { status: 502 }) }).health(), /非 JSON/);
  await assert.rejects(new AgentAPI('', { fetchImpl: async () => { throw new TypeError('network'); } }).health(), /无法连接/);
});
test('timeout aborts waiting without claiming backend cancellation', async () => {
  const api = new AgentAPI('', { timeoutMs: 10, fetchImpl: (_, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
  }) });
  await assert.rejects(api.chat({}), /后端可能仍在执行/);
});
test('rejects unsafe URL schemes and embedded secrets', () => {
  for (const url of ['javascript:alert(1)', 'file:///tmp', 'https://user:secret@example.com', 'https://example.com?key=secret']) assert.throws(() => new AgentAPI(url));
});
