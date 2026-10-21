import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { AgentAPI, safeURL } from '../src/api'
import App from '../src/App.vue'
import MessageBubble from '../src/MessageBubble.vue'
import type { StoredMessage } from '../src/types'
afterEach(() => vi.unstubAllGlobals())
describe('authenticated chat', () => {
  it('sends conversation and selected model with cookies', async () => {
    const fetcher=vi.fn().mockResolvedValue({ok:true,json:async()=>({success:false})});vi.stubGlobal('fetch',fetcher)
    expect((await new AgentAPI('/api/').chat('question','conv-1','model-b')).success).toBe(false)
    expect(fetcher.mock.calls[0]?.[0]).toBe('/api/chat')
    expect(fetcher.mock.calls[0]?.[1].credentials).toBe('include')
    expect(JSON.parse(fetcher.mock.calls[0]?.[1].body)).toEqual({user_input:'question',conversation_id:'conv-1',model:'model-b'})
  })
  it('decodes split UTF-8 chunks and requires a final event',async()=>{
    const encoded=new TextEncoder().encode('{"type":"delta","content":"你好"}\n{"type":"done"}\n')
    vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,body:new ReadableStream({start(c){for(let i=0;i<encoded.length;i+=3)c.enqueue(encoded.slice(i,i+3));c.close()}})})))
    const events:unknown[]=[];await new AgentAPI().stream('q','c','m',e=>events.push(e))
    expect(events).toEqual([{type:'delta',content:'你好'},{type:'done'}])
    vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,body:new ReadableStream({start(c){c.close()}})})))
    await expect(new AgentAPI().stream('q','c','m',()=>{})).rejects.toThrow('连接已断开')
  })
  it('reports authentication errors and rejects unsafe links', async () => {
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:401,json:async()=>({detail:'请先登录'})}))
    await expect(new AgentAPI().runtime()).rejects.toThrow('请先登录')
    expect(safeURL('javascript:alert(1)')).toBeNull();expect(safeURL('https://vuejs.org')).toBe('https://vuejs.org/')
  })
  it('renders external content safely',async()=>{
    const message={id:1,role:'assistant',content:'<img src=x onerror=alert(1)>\n```html\n<script>bad()</script>\n```',status:'completed',created_at:1,result:{sources:[{title:'Vue',url:'https://vuejs.org'}],steps:[],rounds:[],latency_ms:2}} as unknown as StoredMessage
    const wrapper=mount(MessageBubble,{props:{message}})
    expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>');expect(wrapper.findAll('.message-text img')).toHaveLength(0);expect(wrapper.findAll('.message-text script')).toHaveLength(0)
    expect(wrapper.get('.sources-grid a').attributes('href')).toBe('https://vuejs.org/');wrapper.unmount()
  })
  it('starts at login with registration disabled',async()=>{
    vi.stubGlobal('fetch',vi.fn(async()=>({ok:false,status:401,json:async()=>({detail:'请先登录'})})))
    const wrapper=mount(App);await flushPromises()
    expect(wrapper.find('#username').exists()).toBe(true);expect(wrapper.find('#prompt').exists()).toBe(false);expect(wrapper.get('.register-note button').attributes('disabled')).toBeDefined();wrapper.unmount()
  })
})
