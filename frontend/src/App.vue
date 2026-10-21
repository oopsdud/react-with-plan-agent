<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { AgentAPI, APIError, defaultBaseURL } from './api'
import ModelSettings from './ModelSettings.vue'
import MessageBubble from './MessageBubble.vue'
import type { User, Conversation, StoredMessage, ChatResult, RuntimeInfo, ToolInfo } from './types'
interface Thread { messages:StoredMessage[]; running:boolean; phase:string; model:string; result?:ChatResult; loaded:boolean }
const apiBase=ref(defaultBaseURL),user=ref<User>(),booting=ref(true),loginBusy=ref(false),username=ref('user1'),password=ref(''),error=ref('')
const view=ref('chat'),sidebarOpen=ref(false),connectionOpen=ref(false),activeId=ref(''),conversations=ref<Conversation[]>([]),filter=ref('')
const threads=reactive<Record<string,Thread>>({}),models=ref<string[]>([]),defaultModel=ref(''),runtime=ref<RuntimeInfo>(),tool=ref<ToolInfo>()
const input=ref(''),messageBox=ref<HTMLElement>(),textarea=ref<HTMLTextAreaElement>(),atBottom=ref(true),loadingConversation=ref(false),catalogBusy=ref(false)
const inspection=ref<ChatResult>(),toast=ref(''),controllers=new Set<AbortController>()
let generation=0,tempId=-1,scrollFrame=0
const thread=computed(()=>threads[activeId.value])
const busy=computed(()=>thread.value?.running||false)
const filteredConversations=computed(()=>conversations.value.filter(c=>c.title.toLowerCase().includes(filter.value.toLowerCase())))
const currentModel=computed({get:()=>thread.value?.model||defaultModel.value,set:value=>{if(thread.value)thread.value.model=value;else defaultModel.value=value}})
const canSend=computed(()=>!!input.value.trim()&&models.value.includes(currentModel.value)&&!busy.value&&!loadingConversation.value)
function api(){return new AgentAPI(apiBase.value.trim())}
function showError(e:unknown){error.value=e instanceof Error?e.message:'操作失败';if(e instanceof APIError&&e.status===401)clearSession()}
function clearSession(){generation++;controllers.forEach(c=>c.abort());controllers.clear();user.value=undefined;conversations.value=[];activeId.value='';Object.keys(threads).forEach(k=>delete threads[k]);models.value=[];defaultModel.value='';runtime.value=undefined;tool.value=undefined;input.value='';password.value='';inspection.value=undefined;view.value='chat'}
async function refreshConversations(){const epoch=generation,result=await api().conversations();if(epoch===generation)conversations.value=result.conversations}
async function catalog(){const epoch=generation;catalogBusy.value=true;try{const result=await api().catalog();if(epoch!==generation)return;models.value=result.models;defaultModel.value=result.default_model;if(thread.value&&!models.value.includes(thread.value.model))thread.value.model=models.value.includes(result.default_model)?result.default_model:models.value[0]||''}catch(e){if(epoch===generation)showError(e)}finally{if(epoch===generation)catalogBusy.value=false}}
async function initialize(){
  const epoch=generation
  await Promise.all([refreshConversations(),catalog(),api().runtime().then(value=>{if(epoch===generation)runtime.value=value}),api().tool().then(value=>{if(epoch===generation)tool.value=value})])
  if(epoch!==generation)return
  let remembered='';try{remembered=sessionStorage.getItem('agent.active.'+user.value?.id)||''}catch{}
  const initial=conversations.value.find(c=>c.id===remembered)||conversations.value[0]
  if(initial)await openConversation(initial.id)
}
async function login(){if(loginBusy.value)return;loginBusy.value=true;error.value='';try{user.value=await api().login(username.value.trim(),password.value);password.value='';await initialize()}catch(e){showError(e)}finally{loginBusy.value=false}}
async function logout(){try{await api().logout();clearSession();error.value=''}catch(e){showError(e)}}
onMounted(async()=>{try{user.value=await api().me();await initialize()}catch(e){if(!(e instanceof APIError&&e.status===401))showError(e)}finally{booting.value=false}})
onUnmounted(()=>{controllers.forEach(c=>c.abort());cancelAnimationFrame(scrollFrame)})
function scroll(force=false){if(scrollFrame)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=0;if(messageBox.value&&(force||atBottom.value))messageBox.value.scrollTop=messageBox.value.scrollHeight})}
function trackScroll(){const box=messageBox.value;if(box)atBottom.value=box.scrollHeight-box.scrollTop-box.clientHeight<100}
async function openConversation(id:string){
  try{sessionStorage.setItem('agent.active.'+user.value?.id,id)}catch{}
  const epoch=generation;loadingConversation.value=false;activeId.value=id;view.value='chat';inspection.value=undefined;sidebarOpen.value=false;input.value='';error.value=''
  if(threads[id]?.loaded){loadingConversation.value=false;atBottom.value=true;await nextTick();scroll(true);return}
  loadingConversation.value=true
  try{const detail=await api().conversation(id);if(epoch!==generation)return;threads[id]={messages:detail.messages,running:detail.running,phase:detail.running?'后台任务运行中，可稍后刷新查看':'',model:detail.model||defaultModel.value,loaded:true};if(activeId.value===id){await nextTick();atBottom.value=true;scroll(true)}}catch(e){if(epoch===generation)showError(e)}finally{if(epoch===generation&&activeId.value===id)loadingConversation.value=false}
}
async function newConversation(){const epoch=generation;error.value='';try{const item=await api().createConversation();if(epoch!==generation)return;conversations.value.unshift(item);threads[item.id]={messages:[],running:false,phase:'',model:defaultModel.value,loaded:true};await openConversation(item.id);await nextTick();textarea.value?.focus()}catch(e){if(epoch===generation)showError(e)}}
async function deleteConversation(item:Conversation){if(item.running||threads[item.id]?.running)return;if(!window.confirm(`删除会话「${item.title}」？`))return;try{await api().deleteConversation(item.id);delete threads[item.id];conversations.value=conversations.value.filter(c=>c.id!==item.id);if(activeId.value===item.id){activeId.value='';if(conversations.value[0])await openConversation(conversations.value[0].id)}}catch(e){showError(e)}}
async function reloadConversation(){if(!activeId.value)return;const id=activeId.value;if(threads[id]?.running&&controllers.size)return;delete threads[id];await openConversation(id);await refreshConversations()}
async function send(){
  if(!canSend.value)return
  const text=input.value.trim(),model=currentModel.value,epoch=generation
  if(!activeId.value)await newConversation()
  const id=activeId.value,state=threads[id]
  if(!state||state.running||epoch!==generation)return
  state.model=model
  error.value='';input.value='';if(textarea.value)textarea.value.style.height='auto'
  state.running=true;state.phase='正在理解任务';state.result=undefined
  state.messages.push({id:tempId--,role:'user',content:text,model,status:'completed',result:null,created_at:Date.now()/1000})
  const assistant=reactive<StoredMessage>({id:tempId--,role:'assistant',content:'',model,status:'pending',result:null,created_at:Date.now()/1000})
  state.messages.push(assistant)
  atBottom.value=true;await nextTick();scroll(true)
  const controller=new AbortController();controllers.add(controller)
  let pending='',frame=0
  const flush=()=>{frame=0;if(epoch!==generation)return;if(pending){assistant.content+=pending;pending='';scroll()}}
  try{
    await api().stream(text,id,model,event=>{
      if(epoch!==generation)return
      if(event.type==='status')state.phase=event.message||''
      if(event.type==='delta'){pending+=event.content||'';if(!frame)frame=requestAnimationFrame(flush)}
      if(event.type==='review')state.phase=event.sufficient?'证据已充分，准备回答':`第 ${event.round} 轮证据不足，正在调整检索`
      if(event.type==='done'&&event.result){cancelAnimationFrame(frame);flush();assistant.content=event.result.answer||event.result.error||'';assistant.result=event.result;assistant.status=event.result.success?'completed':'failed';assistant.id=event.result.message_id||assistant.id;state.result=event.result;scroll()}
    },controller.signal)
  }catch(e){if(epoch===generation){assistant.status='interrupted';assistant.content=assistant.content||'连接中断，任务可能仍在后台运行。点击刷新会话恢复结果。';showError(e)}}
  finally{cancelAnimationFrame(frame);flush();controllers.delete(controller);if(epoch===generation){state.running=false;state.phase='';void refreshConversations().catch(showError);await nextTick();scroll()}}
}
function keyboard(event:KeyboardEvent){if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();void send()}}
function resizeInput(){if(textarea.value){textarea.value.style.height='auto';textarea.value.style.height=Math.min(textarea.value.scrollHeight,180)+'px'}}
async function saved(){toast.value='模型配置已应用';await Promise.all([catalog(),api().runtime().then(value=>runtime.value=value)]);setTimeout(()=>toast.value='',2500)}
function formatDate(value:number){return new Date(value*1000).toLocaleDateString('zh-CN',{month:'short',day:'numeric'})}
</script>
<template>
  <div v-if="booting" class="boot-screen"><span class="loader"/>正在打开工作空间…</div>
  <div v-else-if="!user" class="login-screen">
    <div class="login-brand"><span class="brand-mark">✳</span><div><strong>Agent Studio</strong><small>REACT WITH PLAN</small></div></div>
    <div class="login-layout"><section class="login-intro"><div class="eyebrow">YOUR LOCAL AGENT WORKSPACE</div><h1>让每一步思考，<br>都有迹可循。</h1><p>从对话到计划，从检索到证据。<br>在一个安静、专注的工作空间中调试你的 Agent。</p><div class="intro-flow"><span>理解任务</span><i>→</i><span>检索 · 核验</span><i>→</i><span>形成回答</span></div><small>每个账号独立的会话记录 · 最多 3 轮证据检索</small></section>
      <form class="login-card" @submit.prevent="login"><span class="login-icon">↗</span><h2>登录工作空间</h2><p>继续上一次对话，或开始一个新的想法。</p><label for="username">用户名</label><input id="username" v-model="username" autocomplete="username" required maxlength="64" :disabled="loginBusy"><label for="password">密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" required :disabled="loginBusy" placeholder="输入账号密码"><div v-if="error" class="error-notice" role="alert">{{error}}</div><button class="button primary login-submit" :disabled="loginBusy">{{loginBusy?'正在登录…':'登录 →'}}</button><div class="register-note">已有 3 个预置账号 <button type="button" disabled title="注册功能暂未开放">注册暂未开放</button></div><details class="demo-accounts"><summary>本地预置账号</summary><p>user1 / AgentUser1!（管理员）<br>user2 / AgentUser2!<br>user3 / AgentUser3!</p><small>可在首次启动前通过 .env 修改初始密码。</small></details><button type="button" class="text-button" @click="connectionOpen=!connectionOpen">连接设置</button><label v-if="connectionOpen">后端地址<input v-model="apiBase" placeholder="同源留空；Vite 开发 /api"></label></form>
    </div><footer class="login-footer">ReAct with Plan Agent · 本地运行框架</footer>
  </div>
  <div v-else class="app-shell">
    <div v-if="sidebarOpen" class="sidebar-backdrop" @click="sidebarOpen=false"/>
    <aside class="sidebar" :class="{open:sidebarOpen}"><div class="brand"><span class="brand-mark">✳</span><div>Agent Studio<small>REACT WITH PLAN</small></div></div><button class="new-chat button" @click="newConversation">＋ 新会话 <span>↗</span></button><nav><button v-for="item in [['chat','◈','对话'],['tools','⌕','工具'],['settings','⚙','模型配置'],['docs','≡','接口文档']]" :key="item[0]" :class="{active:view===item[0]}" @click="view=item[0]!;sidebarOpen=false"><span>{{item[1]}}</span>{{item[2]}}</button></nav><div class="history-heading"><span>最近会话</span><button title="刷新会话" @click="refreshConversations().catch(showError)">↻</button></div><input v-model="filter" class="history-search" placeholder="搜索会话…" aria-label="搜索会话"><div class="conversation-list"><div v-for="item in filteredConversations" :key="item.id" class="conversation-item" :class="{selected:activeId===item.id&&view==='chat'}"><button class="conversation-open" @click="openConversation(item.id)"><span>{{item.title}}</span><small>{{item.running?'运行中':formatDate(item.updated_at)}}</small></button><button class="conversation-delete" :disabled="item.running||threads[item.id]?.running" title="删除会话" @click="deleteConversation(item)">×</button></div><p v-if="!filteredConversations.length" class="empty-history">{{filter?'没有匹配的会话':'新的对话将保存在这里'}}</p></div><div class="user-panel"><span class="user-avatar">{{user.username.slice(-1)}}</span><div><strong>{{user.username}}</strong><small>{{user.is_admin?'管理员':'独立工作空间'}}</small></div><button title="退出登录" @click="logout">↪</button></div></aside>
    <main class="workspace-main"><header class="topbar"><button class="mobile-toggle" aria-label="打开导航" @click="sidebarOpen=true">☰</button><div class="breadcrumb">工作空间 <span>/</span><strong>{{view==='chat'?(conversations.find(c=>c.id===activeId)?.title||'新对话'):view==='settings'?'模型配置':view==='tools'?'工具':'接口文档'}}</strong></div><div class="topbar-actions"><span class="connection-dot" :class="{connected:runtime}"/> <span class="connection-label">{{runtime?'已连接':'连接中'}}</span><button class="icon-button" title="刷新当前会话" @click="reloadConversation">↻</button></div></header>
      <div v-if="error" class="error-notice global-error" role="alert"><span>{{error}}</span><button @click="error=''">×</button></div>
      <section v-if="view==='chat'" class="chat-view"><header class="chat-toolbar"><div><span class="eyebrow">CONVERSATION</span><span class="privacy-label">仅当前账号可见</span></div><div class="model-picker"><span>◈</span><select v-model="currentModel" :disabled="busy||catalogBusy||!models.length" aria-label="当前对话模型"><option v-if="!models.length" value="">请先配置模型</option><option v-for="model in models" :key="model" :value="model">{{model}}</option></select><button title="刷新模型列表" :disabled="catalogBusy" @click="catalog">↻</button></div></header>
        <div ref="messageBox" class="messages" @scroll.passive="trackScroll"><div v-if="loadingConversation" class="history-loading"><span class="loader"/>正在读取会话…</div><div v-else-if="!thread?.messages.length" class="welcome"><div class="welcome-mark">✳</div><h1>从一个问题开始。</h1><p>给 Agent 一个任务，观察它如何检索、核验和回答。</p><div class="suggestions"><button v-for="(text,i) in ['检索 ReAct 论文的公开资料','比较 ReAct 与计划执行的基本概念','查找 Vue 官方文档中的 TypeScript 说明']" :key="text" @click="input=text;textarea?.focus()"><span>{{['⌕','◇','↗'][i]}}</span>{{text}}<small>填入输入框 →</small></button></div><button v-if="!models.length" class="button primary" @click="view='settings'">连接模型服务</button></div><div v-else class="message-column"><MessageBubble v-for="message in thread.messages" :key="message.id" :message="message" @inspect="inspection=message.result||undefined"/><div v-if="thread.running" class="run-progress" role="status"><span class="thinking-dots"><i/><i/><i/></span>{{thread.phase}}</div></div></div>
        <div class="composer-area"><button v-if="!atBottom" class="scroll-bottom" @click="atBottom=true;scroll(true)">↓ 回到最新消息</button><form class="composer" @submit.prevent="send"><textarea id="prompt" ref="textarea" v-model="input" maxlength="12000" rows="2" placeholder="输入任务或继续追问…" :disabled="busy" aria-label="任务输入" @keydown="keyboard" @input="resizeInput"/><div class="composer-bottom"><span class="composer-hint">↵ 发送 <span>· Shift + Enter 换行</span></span><button class="send-button" :disabled="!canSend" aria-label="发送消息">{{busy?'◌':'↑'}}</button></div></form><p class="composer-note">会话自动保存 · 证据不足时调整查询，最多检索 3 轮</p></div>
      </section>
      <section v-else class="page-content"><div class="page-heading"><span class="eyebrow">WORKSPACE SETTINGS</span><h1>{{view==='settings'?'模型与连接':view==='tools'?'工具':'接口文档'}}</h1><p>{{view==='settings'?'配置连接，在对话区自由选择可用模型。':'通用能力，清晰的调用边界。'}}</p></div><template v-if="view==='settings'"><ModelSettings v-if="user.is_admin" :base-u-r-l="apiBase" @saved="saved"/><div v-else class="card settings-card"><h2>当前模型服务</h2><p>服务由管理员 user1 配置。你可以在对话框选择该服务的可用模型。</p><p>当前可用 {{models.length}} 个模型。</p></div><div class="card settings-card"><h2>后端连接</h2><label>API 地址<input v-model="apiBase" placeholder="同源留空；开发模式 /api"></label><p>更换后端后请重新登录对应服务。</p><button class="button" @click="clearSession">应用连接并重新登录</button></div></template><div v-else-if="view==='tools'" class="card settings-card"><span class="tool-symbol">⌕</span><h2>web_search</h2><p>{{tool?.description}}</p><div class="tool-facts"><span>{{runtime?.search_provider||'ddgs'}} · 无需搜索 Key</span><span>最多 3 轮证据核验</span><span>超时 {{tool?.timeout_seconds}}s</span></div><details><summary>输入 Schema</summary><pre>{{JSON.stringify(tool?.input_schema,null,2)}}</pre></details></div><div v-else class="card settings-card"><h2>接口文档</h2><p>登录和会话持久化、按用户隔离的历史、模型选择与增量响应。</p><a :href="apiBase.replace(/\/$/,'')+'/docs'" target="_blank" rel="noopener">打开 Swagger UI ↗</a><pre>POST /auth/login
GET /auth/me
GET /conversations
GET /conversations/{id}
GET /models
POST /chat/stream</pre><p>详细说明见仓库 docs/api.md 与 docs/frontend.md。</p></div></section>
    </main>
    <Transition name="drawer"><div v-if="inspection" class="inspector-backdrop" @click.self="inspection=undefined"><aside class="inspector"><header><div><span class="eyebrow">RUN DETAILS</span><h2>执行详情</h2></div><button aria-label="关闭执行详情" @click="inspection=undefined">×</button></header><div class="inspector-body"><span class="result-badge">{{inspection.success?'已完成':'执行失败'}}</span><p class="trace">{{inspection.trace_id}}</p><p>模型 {{inspection.model}} · {{inspection.latency_ms}}ms</p><p v-if="inspection.error" class="error-text">{{inspection.error}}</p><section v-for="round in inspection.rounds" :key="round.round" class="round-card"><div><strong>第 {{round.round}} 轮检索</strong><span :class="round.sufficient?'sufficient':'insufficient'">{{round.sufficient?'证据充分':'证据不足'}}</span></div><p>{{round.query}}</p><small>{{round.reason}}</small></section><details v-for="(step,index) in inspection.steps" :key="index" class="step"><summary>{{step.step_id}} · {{step.tool_name}} · {{step.status}}</summary><pre>{{JSON.stringify({params:step.params,result:step.result,error:step.error},null,2)}}</pre></details></div></aside></div></Transition>
    <Transition name="toast"><div v-if="toast" class="toast-message">✓ {{toast}}</div></Transition>
  </div>
</template>
