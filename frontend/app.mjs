import { AgentAPI } from './api.mjs';

const $ = (id) => document.getElementById(id);
const storageKey = 'agent-studio.settings.v1';
let config = { baseURL: '', userId: 'default_user' };
try { config = { ...config, ...JSON.parse(localStorage.getItem(storageKey) || '{}') }; } catch { /* Storage is optional. */ }
let api;
try { api = new AgentAPI(config.baseURL); } catch { config.baseURL = ''; api = new AgentAPI(); }
let history = [], busy = false, mode = 'chat', tools = [], connectionEpoch = 0, toolsEpoch = 0;
$('base-url').value = config.baseURL;
$('user-id').value = config.userId;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function toast(text) {
  $('toast').textContent = text; $('toast').classList.remove('hidden');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').classList.add('hidden'), 4500);
}
function jsonBlock(value) { return el('pre', '', JSON.stringify(value, null, 2) ?? 'null'); }
function links() {
  $('swagger-link').href = `${api.baseURL}/docs`;
  $('openapi-link').href = `${api.baseURL}/openapi.json`;
}
async function connect() {
  const epoch = ++connectionEpoch;
  $('connection-text').textContent = '正在连接后端';
  const client = api;
  const [health, runtime] = await Promise.allSettled([client.health(), client.runtime()]);
  if (epoch !== connectionEpoch) return;
  const online = health.status === 'fulfilled' && health.value.status === 'healthy';
  $('connection-dot').className = `dot ${online ? '' : 'error'}`;
  $('connection-text').textContent = online ? '后端已连接' : '后端连接失败';
  if (runtime.status === 'fulfilled') {
    const r = runtime.value;
    $('runtime-banner').textContent = `运行环境 · 模型 ${r.llm_model}（${r.llm_mode}） · 工具 ${r.tools_mode} · Embedding 配置 ${r.embedding_provider}。${r.embedding_note}。`;
  } else {
    $('runtime-banner').textContent = online ? '后端已连接，运行配置不可用。业务工具可能是 Mock，请确认后端配置。' : '无法连接后端，请启动服务或修改连接设置。';
  }
}
function setBusy(value) {
  busy = value;
  ['send', 'new-chat', 'save-settings'].forEach(id => { $(id).disabled = value; });
  document.querySelectorAll('.mode').forEach(node => { node.disabled = value; });
  $('messages').setAttribute('aria-busy', String(value));
  $('send').textContent = value ? '执行中…' : '发送任务 ↑';
  $('request-state').textContent = value ? '等待后端响应' : '就绪';
}
function message(role, text, error = false) {
  $('welcome')?.remove();
  const row = el('article', `message ${role}${error ? ' error' : ''}`);
  row.append(el('div', 'avatar', role === 'user' ? '你' : '✳'));
  const content = el('div', 'message-content');
  content.append(el('div', 'message-label', role === 'user' ? '你' : mode === 'intent' ? '意图识别' : 'Agent'));
  content.append(el('div', 'message-text', text)); row.append(content);
  $('messages').append(row); $('messages').scrollTop = $('messages').scrollHeight;
  return row;
}
function showExecution(result, intentOnly = false) {
  const area = $('execution'); area.replaceChildren();
  const steps = result.steps || [];
  const failed = !intentOnly && (!result.success || steps.some(s => s.status === 'failed'));
  $('run-status').textContent = intentOnly ? '识别完成' : failed ? '存在失败' : '执行完成';
  $('run-status').className = `chip ${failed ? 'error' : 'success'}`;
  const intent = intentOnly ? result : result.intent;
  const grid = el('div', 'meta-grid');
  const values = [['意图', intent?.intent_id ?? '—'], ['复杂度', intent?.complexity ?? '—'], ['置信度', typeof intent?.confidence === 'number' ? `${(intent.confidence * 100).toFixed(1)}%` : '—'], ['耗时', intentOnly ? '—' : `${result.latency_ms} ms`]];
  for (const [label, value] of values) { const item = el('div', 'meta-item'); item.append(el('span', '', label), el('strong', '', value)); grid.append(item); }
  area.append(grid);
  if (result.trace_id) area.append(el('div', 'trace', `TRACE / ${result.trace_id}`));
  if (result.error) area.append(el('p', 'error-text', result.error));
  if (failed && result.success) area.append(el('p', 'error-text', '后端报告任务成功，但包含失败步骤，请以步骤详情核实结果。'));
  if (intent) { const detail = el('details', 'step'); detail.append(el('summary', '', '意图识别原始结果'), jsonBlock(intent)); area.append(detail); }
  area.append(el('div', 'section-label', `执行步骤 / ${steps.length.toString().padStart(2, '0')}`));
  if (!steps.length) area.append(el('p', 'muted-text', intentOnly ? '仅识别意图，未执行工具。' : '没有返回执行步骤。'));
  for (const step of steps) {
    const detail = el('details', `step ${step.status}`);
    const summary = el('summary', '', `${String(step.step_id).padStart(2, '0')}  ${step.tool_name}`);
    summary.append(el('span', 'step-state', step.status));
    const body = el('div', 'step-body');
    body.append(el('div', 'message-meta', `${step.latency_ms ?? 0} ms · 重试 ${step.retry_count ?? 0} 次`));
    body.append(el('div', 'section-label', '输入参数'), jsonBlock(step.params));
    body.append(el('div', 'section-label', '输出结果'), jsonBlock(step.result));
    if (step.error) { body.append(el('p', 'error-text', step.error)); detail.open = true; }
    detail.append(summary, body); area.append(detail);
  }
  const raw = el('details', 'step'); raw.append(el('summary', '', '完整响应 JSON'), jsonBlock(result)); area.append(raw);
}
$('chat-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('prompt').value.trim();
  if (busy || !input) return;
  const intentOnly = mode === 'intent';
  const previousHistory = history.map(turn => ({ ...turn }));
  message('user', input); $('prompt').value = ''; setBusy(true);
  const pending = message('assistant', intentOnly ? '正在分析意图…' : '正在执行任务，完成后将展示执行详情…');
  pending.classList.add('pending');
  $('run-status').textContent = '执行中'; $('run-status').className = 'chip';
  $('execution').replaceChildren(el('p', 'muted-text', '等待本次请求完成…'));
  try {
    const payload = { user_input: input, session_history: previousHistory };
    const result = intentOnly ? await api.intent(payload) : await api.chat({ ...payload, user_id: config.userId });
    pending.remove(); showExecution(result, intentOnly);
    const failed = !intentOnly && (!result.success || (result.steps || []).some(s => s.status === 'failed'));
    const text = intentOnly ? `识别意图：${result.intent_id}\n${result.reasoning || '未提供识别说明'}` : result.answer || result.error || '后端未返回回答。';
    const row = message('assistant', text, failed);
    const button = el('button', 'run-button', '查看本次执行详情 ↗'); button.type = 'button';
    button.addEventListener('click', () => showExecution(result, intentOnly)); row.querySelector('.message-content').append(button);
    if (!intentOnly && !failed) {
      history.push({ role: 'user', content: input }, { role: 'assistant', content: text });
      history = history.slice(-100);
    }
  } catch (error) {
    pending.remove(); message('assistant', error.message, true);
    $('run-status').textContent = '请求失败'; $('run-status').className = 'chip error';
    $('execution').replaceChildren(el('p', 'error-text', error.message));
  } finally { setBusy(false); $('prompt').focus(); }
});
$('prompt').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); }
});
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { $('prompt').value = button.dataset.prompt; $('prompt').focus(); }));
document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {
  mode = button.dataset.mode;
  document.querySelectorAll('[data-mode]').forEach(node => { const active = node === button; node.classList.toggle('selected', active); node.setAttribute('aria-pressed', String(active)); });
}));
const welcomeTemplate = $('welcome').cloneNode(true);
function clearConversation() {
  history = []; $('messages').replaceChildren(welcomeTemplate.cloneNode(true));
  $('messages').querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { $('prompt').value = button.dataset.prompt; $('prompt').focus(); }));
  $('execution').replaceChildren(el('div', 'inspector-empty', '新会话已就绪，等待任务。'));
  $('run-status').textContent = '等待任务'; $('run-status').className = 'chip'; $('prompt').value = '';
}
$('new-chat').addEventListener('click', () => { if (!busy) clearConversation(); });
$('settings-toggle').addEventListener('click', () => {
  const visible = $('settings').classList.contains('hidden'); $('settings').classList.toggle('hidden', !visible);
  $('settings-toggle').setAttribute('aria-expanded', String(visible));
});
$('save-settings').addEventListener('click', () => {
  if (busy) return;
  try {
    const baseURL = $('base-url').value.trim(); const userId = $('user-id').value.trim() || 'default_user';
    const nextAPI = new AgentAPI(baseURL);
    if (config.userId !== userId || config.baseURL !== nextAPI.baseURL) clearConversation();
    api = nextAPI; config = { baseURL: api.baseURL, userId }; tools = []; ++toolsEpoch;
    $('tools-list').replaceChildren(); $('tool-count').textContent = '尚未加载'; $('reload-tools').disabled = false;
    try { localStorage.setItem(storageKey, JSON.stringify(config)); } catch { toast('浏览器不允许存储，设置仅在本次页面有效'); }
    links(); connect(); if (!$('view-tools').classList.contains('hidden')) loadTools();
    toast('连接设置已应用');
  } catch (error) { toast(`地址无效：${error.message}`); }
});
$('refresh').addEventListener('click', connect);
function renderTools() {
  const query = $('tool-search').value.toLowerCase().trim();
  const visible = tools.filter(tool => `${tool.name} ${tool.description || ''}`.toLowerCase().includes(query));
  $('tools-list').replaceChildren(); $('tool-count').textContent = `${visible.length} / ${tools.length} 个工具`;
  if (!visible.length) $('tools-list').append(el('p', 'muted-text', '没有匹配的工具。'));
  for (const tool of visible) {
    const card = el('article', 'card tool-card'); card.append(el('div', 'tool-icon', '⌘'), el('h3', '', tool.name), el('p', '', tool.description || tool.error || '暂无描述'));
    const footer = el('div', 'tool-footer'); footer.append(el('span', '', tool.timeout_seconds === undefined ? '配置不可用' : `超时配置 ${tool.timeout_seconds}s`), el('span', '', 'MOCK')); card.append(footer); $('tools-list').append(card);
  }
}
async function loadTools() {
  const epoch = ++toolsEpoch, client = api;
  $('tools-list').replaceChildren(el('p', 'muted-text', '正在加载工具…'));
  $('reload-tools').disabled = true;
  try {
    const data = await client.tools();
    const results = await Promise.allSettled(data.tools.map(name => client.tool(name)));
    if (epoch !== toolsEpoch) return;
    tools = results.map((r, index) => r.status === 'fulfilled' ? r.value : { name: data.tools[index], error: r.reason.message }); renderTools();
  } catch (error) { if (epoch === toolsEpoch) { $('tools-list').replaceChildren(el('p', 'error-text', error.message)); $('tool-count').textContent = '加载失败'; } }
  finally { if (epoch === toolsEpoch) $('reload-tools').disabled = false; }
}
$('reload-tools').addEventListener('click', loadTools);
$('tool-search').addEventListener('input', renderTools);
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
  const name = button.dataset.view;
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('hidden', view.id !== `view-${name}`));
  document.querySelectorAll('.nav').forEach(nav => { nav.classList.toggle('active', nav === button); if (nav === button) nav.setAttribute('aria-current', 'page'); else nav.removeAttribute('aria-current'); });
  $('page-title').textContent = { chat: '对话工作台', tools: '工具目录', docs: '接口文档' }[name];
  if (name === 'tools' && !tools.length) loadTools();
}));
for (const [method, path, description] of [['GET', '/', '服务信息'], ['GET', '/health', '存活检查'], ['GET', '/runtime', '脱敏运行配置'], ['POST', '/chat', '完整执行'], ['POST', '/intent', '意图识别'], ['GET', '/tools', '工具列表'], ['GET', '/tools/{tool_name}', '工具详情']]) {
  const row = el('div', 'endpoint'); row.append(el('span', `method ${method.toLowerCase()}`, method), el('code', '', path), el('small', '', description)); $('endpoint-list').append(row);
}
links(); connect();
