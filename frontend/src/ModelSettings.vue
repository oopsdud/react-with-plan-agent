<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { AgentAPI } from './api'
const props=defineProps<{baseURL:string}>()
const emit=defineEmits<{saved:[]}>()
const baseURL=ref(''),apiKey=ref(''),model=ref(''),jsonMode=ref(true),hasKey=ref(false)
const models=ref<string[]>([]),loading=ref(false),saving=ref(false),error=ref(''),notice=ref('')
const persistedURL=ref('')
let revision=0
function connection() {return {base_url:baseURL.value.trim(),...(apiKey.value.trim()?{api_key:apiKey.value.trim()}: {})}}
watch([baseURL,apiKey],()=>{revision++;models.value=[];notice.value=''})
async function fetchModels() {
  if(loading.value||saving.value)return
  const current=revision
  loading.value=true;error.value='';notice.value=''
  try {
    const result=await new AgentAPI(props.baseURL).models(connection())
    if(current!==revision)return
    models.value=result.models
    if(!models.value.includes(model.value))model.value=models.value[0]||''
    notice.value=`已读取 ${models.value.length} 个模型，请选择后保存。`
  } catch(e){error.value=e instanceof Error?e.message:'读取失败'}
  finally {loading.value=false}
}
async function loadConfig() {
  loading.value=true;error.value=''
  try {
    const config=await new AgentAPI(props.baseURL).modelConfig()
    baseURL.value=config.base_url;persistedURL.value=config.base_url
    model.value=config.model;jsonMode.value=config.json_mode;hasKey.value=config.api_key_configured
  } catch(e){error.value=e instanceof Error?e.message:'读取配置失败'}
  finally {loading.value=false}
  await nextTick()
  if(hasKey.value)await fetchModels()
}
async function save() {
  if(!models.value.includes(model.value)||loading.value||saving.value)return
  saving.value=true;error.value='';notice.value=''
  try {
    const config=await new AgentAPI(props.baseURL).saveModel({...connection(),model:model.value,json_mode:jsonMode.value})
    persistedURL.value=config.base_url;hasKey.value=config.api_key_configured;apiKey.value=''
    // Clearing the password invalidates discovery; the saved model remains visible.
    await nextTick()
    models.value=[config.model];notice.value='已保存到后端配置文件，新任务立即使用此模型，无需重启。'
    emit('saved')
  } catch(e){error.value=e instanceof Error?e.message:'保存失败'}
  finally {saving.value=false}
}
onMounted(loadConfig)
</script>
<template>
  <section class="card model-settings">
    <h2>模型配置</h2><p>连接 OpenAI 兼容服务，读取模型列表后选择模型。</p>
    <form @submit.prevent="save">
      <label for="model-base-url">API 基础地址</label>
      <input id="model-base-url" v-model="baseURL" type="url" required :disabled="loading||saving" placeholder="https://your-provider.example/v1" autocomplete="off">
      <label for="model-api-key">API Key <small>{{hasKey&&baseURL===persistedURL?'已保存，留空沿用；输入新值可替换':'请填写该服务的密钥'}}</small></label>
      <input id="model-api-key" v-model="apiKey" type="password" :disabled="loading||saving" autocomplete="new-password" placeholder="密钥不会回显">
      <button type="button" class="button" :disabled="loading||saving||!baseURL.trim()" @click="fetchModels">{{loading?'读取中…':'读取模型列表'}}</button>
      <label for="model-select">模型 <small v-if="model&&!models.length">已保存：{{model}}</small></label>
      <select id="model-select" v-model="model" required :disabled="loading||saving||!models.length"><option value="" disabled>请先读取模型列表</option><option v-for="id in models" :key="id" :value="id">{{id}}</option></select>
      <label class="checkbox-label"><input v-model="jsonMode" type="checkbox" :disabled="loading||saving">启用 JSON mode（服务需支持）</label>
      <p v-if="error" class="error-text" role="alert">{{error}}</p><p v-if="notice" class="runtime-banner" role="status">{{notice}}</p>
      <button class="button primary" :disabled="loading||saving||!models.includes(model)">{{saving?'保存中…':'保存并应用'}}</button>
    </form>
    <p>配置保存在本机后端 data/model-config.json，优先于环境变量及 .env。API Key 不会回显或写入浏览器存储。</p>
  </section>
</template>
