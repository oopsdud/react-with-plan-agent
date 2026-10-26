import type { ChatResult, Intent, RuntimeInfo, ToolInfo, ModelConfig, ModelConnection, ModelSave, User, Conversation, ConversationDetail, StreamEvent, CheckpointView } from './types'
export const defaultBaseURL=import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV?'/api':'')
export function safeURL(value:string):string|null {try{const url=new URL(value);return ['http:','https:'].includes(url.protocol)?url.href:null}catch{return null}}
export class APIError extends Error { constructor(message:string,readonly status:number,readonly code?:string,readonly requestId?:string,readonly retryAfter?:number){super(message)} }
export class AgentAPI {
  constructor(readonly baseURL=defaultBaseURL){}
  private url(path:string){return this.baseURL.replace(/\/$/,'')+path}
  async request<T>(path:string,body?:unknown,method?:string):Promise<T>{
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),60_000)
    try{
      const response=await fetch(this.url(path),{method:method||(body===undefined?'GET':'POST'),credentials:'include',signal:controller.signal,headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)})
      const value=await response.json().catch(()=>null)
      if(!response.ok)throw new APIError(typeof value?.detail==='string'?value.detail:`请求失败（HTTP ${response.status}）`,response.status,value?.error?.code,value?.request_id,value?.error?.retry_after)
      if(value===null)throw new Error('服务返回格式错误，请检查后端地址。')
      return value as T
    }finally{clearTimeout(timer)}
  }
  login(username:string,password:string){return this.request<User>('/auth/login',{username,password})}
  me(){return this.request<User>('/auth/me')}
  logout(){return this.request('/auth/logout',{})}
  conversations(){return this.request<{conversations:Conversation[]}>('/conversations')}
  createConversation(){return this.request<Conversation>('/conversations',{})}
  conversation(id:string,beforeId?:number){return this.request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}${beforeId?`?before_id=${beforeId}`:''}`)}
  deleteConversation(id:string){return this.request(`/conversations/${encodeURIComponent(id)}`,undefined,'DELETE')}
  checkpoint(id:string){return this.request<CheckpointView>(`/runs/${encodeURIComponent(id)}/checkpoint`)}
  resumeRun(id:string,generation:number){return this.request(`/runs/${encodeURIComponent(id)}/resume`,{expected_generation:generation})}
  revisePlan(id:string,version:number,plan:Record<string,unknown>,reason:string){return this.request(`/runs/${encodeURIComponent(id)}/plan/revisions`,{expected_version:version,plan,reason})}
  cancelRun(id:string){return this.request(`/runs/${encodeURIComponent(id)}/cancel`,{})}
  run(id:string){return this.request<{id:string;state:string}>(`/runs/${encodeURIComponent(id)}`)}
  catalog(){return this.request<{models:string[];default_model:string}>('/models')}
  modelConfig(){return this.request<ModelConfig>('/config/model')}
  models(connection:ModelConnection){return this.request<{models:string[]}>('/config/model/models',connection)}
  saveModel(config:ModelSave){return this.request<ModelConfig>('/config/model',config,'PUT')}
  runtime(){return this.request<RuntimeInfo>('/runtime')}
  tool(){return this.request<ToolInfo>('/tools/web_search')}
  intent(input:string,conversationId?:string,model?:string){return this.request<Intent>('/intent',{user_input:input,conversation_id:conversationId,model})}
  chat(input:string,conversationId?:string,model?:string){return this.request<ChatResult>('/chat',{user_input:input,conversation_id:conversationId,model})}
  async stream(input:string,conversationId:string,model:string,onEvent:(event:StreamEvent)=>void,signal?:AbortSignal){
    const response=await fetch(this.url('/chat/stream'),{method:'POST',credentials:'include',signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({user_input:input,conversation_id:conversationId,model,client_request_id:crypto.randomUUID()})})
    if(!response.ok){const body=await response.json().catch(()=>null);throw new APIError(typeof body?.detail==='string'?body.detail:`请求失败（HTTP ${response.status}）`,response.status,body?.error?.code,body?.request_id,body?.error?.retry_after)}
    if(!response.body)throw new Error('浏览器不支持响应流。')
    const reader=response.body.getReader(),decoder=new TextDecoder()
    let buffer='',complete=false
    function consume(line:string){if(!line.trim())return;const event=JSON.parse(line) as StreamEvent;onEvent(event);if(event.type==='done')complete=true;if(event.type==='error')throw new Error(event.message||'执行失败')}
    try{
      for(;;){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let index:number;while((index=buffer.indexOf('\n'))>=0){consume(buffer.slice(0,index));buffer=buffer.slice(index+1)}}
      buffer+=decoder.decode();if(buffer.trim())consume(buffer)
      if(!complete)throw new Error('连接已断开；任务可能仍在后台运行，请刷新会话查看。')
    }finally{await reader.cancel().catch(()=>{});reader.releaseLock()}
  }
}
