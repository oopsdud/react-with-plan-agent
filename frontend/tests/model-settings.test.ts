import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ModelSettings from '../src/ModelSettings.vue'
afterEach(()=>vi.unstubAllGlobals())
describe('model configuration',()=>{
  it('reads models, chooses one, saves without returning or retaining the key',async()=>{
    const fetcher=vi.fn(async(url:string,options:RequestInit)=>({ok:true,json:async()=>url.endsWith('/models')?{models:['model-a','model-b']}:
      options.method==='PUT'?{base_url:'https://provider.example/v1',model:'model-b',api_key_configured:true,json_mode:true}:
      {base_url:'https://provider.example/v1',model:'',api_key_configured:false,json_mode:true}}))
    vi.stubGlobal('fetch',fetcher)
    const wrapper=mount(ModelSettings,{props:{baseURL:'/api'}})
    await flushPromises()
    await wrapper.get('#model-api-key').setValue('test-secret')
    await wrapper.get('button[type=button]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('#model-select option')).toHaveLength(3)
    await wrapper.get('#model-select').setValue('model-b')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const saved=fetcher.mock.calls.find(([,options])=>options.method==='PUT')
    expect(JSON.parse(String(saved?.[1].body))).toMatchObject({model:'model-b',api_key:'test-secret'})
    expect((wrapper.get('#model-api-key').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('无需重启')
    expect(wrapper.emitted('saved')).toHaveLength(1)
    wrapper.unmount()
  })
  it('automatically reads the saved configuration and model list',async()=>{
    const fetcher=vi.fn(async(url:string)=>({ok:true,json:async()=>url.endsWith('/models')?{models:['saved-model','other']}:
      {base_url:'https://provider.example/v1',model:'saved-model',api_key_configured:true,json_mode:true}}))
    vi.stubGlobal('fetch',fetcher)
    const wrapper=mount(ModelSettings,{props:{baseURL:'/api'}})
    await flushPromises()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect((wrapper.get('#model-select').element as HTMLSelectElement).value).toBe('saved-model')
    expect(wrapper.text()).toContain('已读取 2 个模型')
    await wrapper.get('#model-base-url').setValue('https://other.example/v1')
    expect(wrapper.findAll('#model-select option')).toHaveLength(1)
    wrapper.unmount()
  })
  it('shows discovery errors without saving',async()=>{
    vi.stubGlobal('fetch',vi.fn(async(url:string)=>({ok:!url.endsWith('/models'),status:502,json:async()=>url.endsWith('/models')?{detail:'读取模型失败（HTTP 401）'}:{base_url:'https://provider.example/v1',model:'',api_key_configured:false,json_mode:true}})))
    const wrapper=mount(ModelSettings,{props:{baseURL:'/api'}})
    await flushPromises()
    await wrapper.get('#model-api-key').setValue('invalid-key')
    await wrapper.get('button[type=button]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('HTTP 401')
    expect(wrapper.get('button.primary').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})
