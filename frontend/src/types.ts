export interface Turn { role: 'user' | 'assistant'; content: string }
export interface Intent {
  intent_id: string; complexity: string; confidence: number; is_multi_intent: boolean
  secondary_intents: string[]; extracted_slots: Record<string, unknown>; reasoning: string; routing_path: string
}
export interface StepResult {
  step_id: number; tool_name: string; status: 'succeeded' | 'failed' | 'skipped' | 'pending'
  params: Record<string, unknown>; result: unknown; error: string | null; retry_count: number; latency_ms: number
}
export interface Source { title: string; url: string; content: string; score: number | null }
export interface ChatResult {
  run_id?:string; run_state?:string; degraded?:boolean; queue_wait_ms?:number; error_info?:{code:string;message:string;retryable:boolean}; conversation_id?:string; message_id?:number; model?:string; rounds?:ResearchRound[]; evidence_sufficient?:boolean|null; trace_id: string; success: boolean; answer: string | null; intent: Intent | null
  steps: StepResult[]; latency_ms: number; error: string | null; sources: Source[]
}
export interface RuntimeInfo {
  version: string; llm_mode: string; llm_model: string; llm_configured: boolean
  search_provider: string; search_configured: boolean; tools_mode: string; streaming: boolean
}
export interface ToolInfo {
  name: string; description: string; timeout_seconds: number; input_schema: Record<string, unknown>; configured: boolean
}
export interface Settings { baseURL: string; userId: string }

export interface ModelConfig { base_url: string; model: string; api_key_configured: boolean; json_mode: boolean }
export interface ModelConnection { base_url: string; api_key?: string }
export interface ModelSave extends ModelConnection { model: string; json_mode: boolean }

export interface User { id:string; username:string; is_admin:boolean }
export interface Conversation { id:string; title:string; model:string|null; updated_at:number; running:boolean; run_id?:string|null }
export interface StoredMessage { id:number; role:'user'|'assistant'; content:string; model:string|null; status:string; result:ChatResult|null; created_at:number }
export interface ConversationDetail extends Conversation { messages:StoredMessage[]; has_more?:boolean; next_before_id?:number|null }
export interface ResearchRound { round:number; query:string; sufficient:boolean; reason:string; next_query:string }
export interface StreamEvent {
  run_id?:string; code?:string; type:string; content?:string; message?:string; phase?:string; round?:number; query?:string; sufficient?:boolean; reason?:string
  conversation_id?:string; message_id?:number; result?:ChatResult; step?:StepResult; intent?:Intent
}
