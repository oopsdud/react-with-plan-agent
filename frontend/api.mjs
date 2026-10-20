/** Public browser client. Contract: docs/frontend-api.md */
export class APIError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message);
    this.name = 'APIError';
    this.status = status;
    this.detail = detail;
  }
}

export class AgentAPI {
  constructor(baseURL = '', { timeoutMs = 120000, fetchImpl = globalThis.fetch.bind(globalThis) } = {}) {
    this.baseURL = baseURL.trim().replace(/\/+$/, '');
    if (this.baseURL) {
      const url = new URL(this.baseURL);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
        throw new APIError('后端地址必须是无凭证、查询参数或片段的 HTTP(S) 地址');
      }
    }
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
  }

  async request(path, { method = 'GET', body } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(`${this.baseURL}${path}`, {
        method,
        headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        credentials: 'omit',
      });
      let data;
      try { data = await response.json(); }
      catch (error) {
        if (controller.signal.aborted) throw error;
        throw new APIError(`服务返回非 JSON 响应（HTTP ${response.status}）`, response.status);
      }
      if (!response.ok) {
        const detail = data.detail ?? data;
        throw new APIError(typeof detail === 'string' ? detail : JSON.stringify(detail), response.status, detail);
      }
      return data;
    } catch (error) {
      if (error instanceof APIError) throw error;
      if (error.name === 'AbortError') throw new APIError('等待响应超时，后端可能仍在执行。请核实结果后再决定是否重试。');
      throw new APIError('无法连接后端，请检查地址、服务状态及跨域配置。');
    } finally { clearTimeout(timer); }
  }

  health() { return this.request('/health'); }
  runtime() { return this.request('/runtime'); }
  tools() { return this.request('/tools'); }
  tool(name) { return this.request(`/tools/${encodeURIComponent(name)}`); }
  chat(payload) { return this.request('/chat', { method: 'POST', body: payload }); }
  intent(payload) { return this.request('/intent', { method: 'POST', body: payload }); }
}
