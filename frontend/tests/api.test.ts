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

describe('queue and cancellation UI',()=>{
  function environment(reject=false){
    let stream:ReadableStreamDefaultController<Uint8Array>|undefined
    const result={success:false,answer:'任务已取消',error:'任务已取消',sources:[],rounds:[],steps:[],latency_ms:2,trace_id:'run',message_id:2,run_id:'run-1'}
    const fetcher=vi.fn(async(url:string,options:RequestInit)=>{
      const value=url.endsWith('/auth/me')?{id:'user-1',username:'user1',is_admin:true}:url.endsWith('/models')?{models:['model-a'],default_model:'model-a'}:url.endsWith('/runtime')?{llm_configured:true}:url.endsWith('/tools/web_search')?{name:'web_search',input_schema:{}}:url.endsWith('/conversations')?(options.method==='POST'?{id:'cid',title:'new',model:'model-a',updated_at:1,running:false}:{conversations:[]}):{}
      if(url.endsWith('/chat/stream')){
        if(reject)return {ok:false,status:429,json:async()=>({detail:'队列已满',error:{code:'QUEUE_FULL',retry_after:5},request_id:'request-123'})}
        return {ok:true,body:new ReadableStream<Uint8Array>({start(c){stream=c;c.enqueue(new TextEncoder().encode('{"type":"start","run_id":"run-1"}\n{"type":"status","phase":"queued","message":"任务已受理，等待执行配额"}\n'))}})}
      }
      if(url.endsWith('/runs/run-1/cancel')){stream?.enqueue(new TextEncoder().encode(JSON.stringify({type:'done',result})+'\n'));stream?.close()}
      return {ok:true,json:async()=>value}
    })
    vi.stubGlobal('fetch',fetcher)
    return fetcher
  }
  it('shows queued state and cancels the actual server run',async()=>{
    const fetcher=environment(),wrapper=mount(App);await flushPromises()
    await wrapper.get('#prompt').setValue('queued task');await wrapper.get('form.composer').trigger('submit');await flushPromises()
    expect(wrapper.text()).toContain('等待执行配额');expect(wrapper.get('.cancel-run').attributes('disabled')).toBeUndefined()
    await wrapper.get('.cancel-run').trigger('click');await flushPromises()
    expect(fetcher.mock.calls.some(([url])=>url.endsWith('/runs/run-1/cancel'))).toBe(true)
    expect(wrapper.text()).toContain('任务已取消');expect(wrapper.find('.cancel-run').exists()).toBe(false);wrapper.unmount()
  })
  it('rolls back unsaved optimistic messages and keeps input when admission rejects',async()=>{
    environment(true);const wrapper=mount(App);await flushPromises()
    await wrapper.get('#prompt').setValue('keep this input');await wrapper.get('form.composer').trigger('submit');await flushPromises()
    expect(wrapper.findAll('.chat-message')).toHaveLength(0)
    expect((wrapper.get('#prompt').element as HTMLTextAreaElement).value).toBe('keep this input')
    expect(wrapper.text()).toContain('队列已满');expect(wrapper.text()).toContain('5 秒后重试');wrapper.unmount()
  })
})

describe('durable recovery controls',()=>{
  it('offers owner task inspection and resume for an interrupted message',async()=>{
    const message={id:2,role:'assistant',content:'任务中断',status:'interrupted',created_at:1,result:null,checkpoint_available:true,run_id:'run-1',run_state:'interrupted'} as StoredMessage
    const wrapper=mount(MessageBubble,{props:{message}})
    const checkpoint=wrapper.findAll('button').find(b=>b.text()==='检查点')
    const resume=wrapper.findAll('button').find(b=>b.text()==='从检查点继续')
    await checkpoint?.trigger('click');await resume?.trigger('click')
    expect(wrapper.emitted('checkpoint')).toHaveLength(1);expect(wrapper.emitted('resume')).toHaveLength(1)
    await wrapper.setProps({busy:true});expect(resume?.attributes('disabled')).toBeDefined();wrapper.unmount()
  })
  it('sends generation and plan version comparisons without a client user identity',async()=>{
    const fetcher=vi.fn().mockResolvedValue({ok:true,json:async()=>({})});vi.stubGlobal('fetch',fetcher)
    const api=new AgentAPI('/api'),plan={intent_id:'web_search',steps:[]}
    await api.resumeRun('run-1',3);await api.revisePlan('run-1',2,plan,'A needs B')
    expect(JSON.parse(fetcher.mock.calls[0]?.[1].body)).toEqual({expected_generation:3})
    expect(JSON.parse(fetcher.mock.calls[1]?.[1].body)).toEqual({expected_version:2,plan,reason:'A needs B'})
    expect(fetcher.mock.calls[0]?.[1].credentials).toBe('include')
  })
})
