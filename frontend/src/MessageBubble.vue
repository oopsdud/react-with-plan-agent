<script setup lang="ts">
import { computed, ref } from 'vue'
import { safeURL } from './api'
import type { StoredMessage } from './types'
const props=defineProps<{message:StoredMessage}>()
const emit=defineEmits<{inspect:[]}>()
const copied=ref(false)
const blocks=computed(()=>props.message.content.split(/(```[\s\S]*?(?:```|$))/g).filter(Boolean).map(text=>text.startsWith('```')?{code:true,text:text.replace(/^```[^\n]*\n?/,'').replace(/```$/,'')}:{code:false,text}))
async function copy(){try{await navigator.clipboard.writeText(props.message.content);copied.value=true;setTimeout(()=>copied.value=false,1500)}catch{copied.value=false}}
</script>
<template>
  <article class="chat-message" :class="[message.role,{failed:message.status==='failed'}]">
    <div class="message-avatar">{{message.role==='user'?'你':'✳'}}</div>
    <div class="bubble-content">
      <div class="bubble-heading"><strong>{{message.role==='user'?'你':'Agent'}}</strong><span v-if="message.model&&message.role==='assistant'">{{message.model}}</span></div>
      <div class="message-text"><template v-for="(block,i) in blocks" :key="i"><pre v-if="block.code" class="code-block"><code>{{block.text}}</code></pre><span v-else>{{block.text}}</span></template><span v-if="message.status==='pending'" class="stream-cursor"/></div>
      <div v-if="message.result?.sources.length" class="sources-grid"><a v-for="(source,i) in message.result.sources" :key="source.url" :href="safeURL(source.url)||undefined" target="_blank" rel="noopener noreferrer"><small>来源 {{i+1}} ↗</small><span>{{source.title}}</span></a></div>
      <div v-if="message.role==='assistant'&&message.status!=='pending'" class="message-actions"><button @click="copy">{{copied?'已复制':'复制回复'}}</button><button v-if="message.result" @click="emit('inspect')">执行详情 · {{message.result.rounds?.length||0}} 轮检索</button><span v-if="message.result">{{(message.result.latency_ms/1000).toFixed(1)}}s</span></div>
    </div>
  </article>
</template>
