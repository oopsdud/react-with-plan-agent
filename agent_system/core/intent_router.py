"""
意图识别模块 - 语义路由 + LLM兜底
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
import time
import uuid

from agent_system.config.settings import get_settings
from agent_system.utils.embedding_client import get_embedding_client, BaseEmbeddingClient
from agent_system.utils.llm_client import get_llm_client, BaseLLMClient

settings = get_settings()


class IntentDecision(BaseModel):
    """意图识别输出"""
    intent_id: str = Field(..., description="主意图ID")
    complexity: str = Field(..., description="复杂度: simple / complex")
    confidence: float = Field(..., ge=0, le=1, description="置信度 [0, 1]")
    is_multi_intent: bool = Field(default=False, description="是否多意图")
    secondary_intents: List[str] = Field(default_factory=list, description="次要意图列表")
    extracted_slots: Dict[str, Any] = Field(default_factory=dict, description="提取的槽位")
    reasoning: str = Field(default="", description="决策理由")
    
    # 路由路径（调试用）
    routing_path: str = Field(default="", description="路由路径: semantic / llm_fallback / multi_intent")


class SemanticRouter:
    """语义路由器"""
    
    def __init__(self, embedding_client: Optional[BaseEmbeddingClient] = None):
        self.embedding = embedding_client or get_embedding_client()
        self.threshold = settings.INTENT_SEMANTIC_THRESHOLD
        self.multi_threshold = settings.INTENT_MULTI_THRESHOLD
        
        # 意图描述缓存
        self._intent_descriptions: Dict[str, str] = {}
        self._intent_embeddings: Dict[str, List[float]] = {}
    
    def register_intent(self, intent_id: str, description: str):
        """注册意图描述"""
        self._intent_descriptions[intent_id] = description
        # 预计算embedding（实际应异步）
        # self._intent_embeddings[intent_id] = await self.embedding.encode([description])
    
    async def route(self, user_input: str, session_history: Optional[List[Dict]] = None) -> IntentDecision:
        """
        语义路由
        仅单意图识别使用语义识别，多意图或低置信度转LLM
        """
        if not self._intent_descriptions:
            return IntentDecision(
                intent_id="general",
                complexity="complex",
                confidence=0.0,
                routing_path="semantic_no_intents"
            )
        
        # 编码输入
        input_embedding = await self.embedding.encode([user_input])
        
        # 计算与所有意图的相似度
        similarities = {}
        for intent_id, description in self._intent_descriptions.items():
            if intent_id not in self._intent_embeddings:
                desc_emb = await self.embedding.encode([description])
                self._intent_embeddings[intent_id] = desc_emb[0]
            
            sim = self.embedding.cosine_similarity(input_embedding[0], self._intent_embeddings[intent_id])
            similarities[intent_id] = sim
        
        # 排序
        sorted_intents = sorted(similarities.items(), key=lambda x: x[1], reverse=True)
        
        top_intent, top_score = sorted_intents[0]
        second_intent, second_score = sorted_intents[1] if len(sorted_intents) > 1 else (None, 0)
        
        # 判定多意图：top-2分数都高且分差小
        is_multi = (
            top_score > self.threshold and 
            second_score > self.threshold and 
            (top_score - second_score) < self.multi_threshold
        )
        
        if is_multi:
            return IntentDecision(
                intent_id=top_intent,
                secondary_intents=[second_intent],
                is_multi_intent=True,
                complexity="complex",
                confidence=top_score,
                routing_path="semantic_multi_detected"
            )
        
        # 单意图高置信度
        if top_score > self.threshold:
            # 从配置获取complexity（静态设置）
            complexity = self._get_intent_complexity(top_intent)
            return IntentDecision(
                intent_id=top_intent,
                complexity=complexity,
                confidence=top_score,
                is_multi_intent=False,
                routing_path="semantic"
            )
        
        # 低置信度，需要LLM兜底
        return IntentDecision(
            intent_id=top_intent,
            complexity="complex",
            confidence=top_score,
            is_multi_intent=False,
            routing_path="semantic_low_confidence"
        )
    
    def _get_intent_complexity(self, intent_id: str) -> str:
        """获取意图复杂度（静态配置）"""
        # 应从数据库读取，这里简化
        simple_intents = {"query_weather", "set_reminder", "query_employee", "query_todo"}
        return "simple" if intent_id in simple_intents else "complex"


class LLMFallbackRouter:
    """LLM兜底路由器"""
    
    def __init__(self, llm_client: Optional[BaseLLMClient] = None):
        self.llm = llm_client or get_llm_client()
    
    async def route(self, user_input: str, candidate_intents: List[str], 
                    session_history: Optional[List[Dict]] = None) -> IntentDecision:
        """
        LLM兜底识别
        当语义路由置信度低或检测到多意图时使用
        """
        # 构建提示词
        history_text = ""
        if session_history:
            recent = session_history[-3:]  # 最近3轮
            history_text = "\n".join([f"{t.get('role', 'user')}: {t.get('content', '')}" for t in recent])
        
        prompt = f"""分析用户输入的意图。

用户输入: {user_input}

历史对话:
{history_text if history_text else "无"}

候选意图:
{chr(10).join([f"- {intent}" for intent in candidate_intents])}

请输出JSON格式:
{{
    "intent_id": "意图ID",
    "complexity": "simple或complex",
    "confidence": 0.0到1.0之间的置信度,
    "is_multi_intent": true或false,
    "secondary_intents": ["次要意图ID"],
    "reasoning": "决策理由",
    "extracted_slots": {{"person": ["明确提到的姓名"], "query": "查询内容"}}
}}"""
        
        schema = {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
                "complexity": {"type": "string", "enum": ["simple", "complex"]},
                "confidence": {"type": "number"},
                "is_multi_intent": {"type": "boolean"},
                "secondary_intents": {"type": "array", "items": {"type": "string"}},
                "reasoning": {"type": "string"},
                "extracted_slots": {"type": "object"}
            },
            "required": ["intent_id", "complexity", "confidence", "is_multi_intent"]
        }
        
        try:
            result = await self.llm.extract_structured(prompt, schema)
            
            return IntentDecision(
                intent_id=result.get("intent_id", "general"),
                complexity=result.get("complexity", "complex"),
                confidence=result.get("confidence", 0.7),
                is_multi_intent=result.get("is_multi_intent", False),
                secondary_intents=result.get("secondary_intents", []),
                extracted_slots=result.get("extracted_slots", {}),
                reasoning=result.get("reasoning", "LLM兜底识别"),
                routing_path="llm_fallback"
            )
        except Exception as e:
            # LLM失败，返回通用意图
            return IntentDecision(
                intent_id="general",
                complexity="complex",
                confidence=0.5,
                reasoning=f"LLM fallback failed: {str(e)}",
                routing_path="llm_fallback_error"
            )


class IntentRouter:
    """意图路由器 - 主入口"""
    
    def __init__(self, 
                 embedding_client: Optional[BaseEmbeddingClient] = None,
                 llm_client: Optional[BaseLLMClient] = None):
        self.semantic_router = SemanticRouter(embedding_client)
        self.llm_fallback = LLMFallbackRouter(llm_client)
        self.embedding = embedding_client or get_embedding_client()
    
    def load_intents_from_db(self, db_session):
        """从数据库加载意图"""
        from agent_system.db.models import Intent
        
        intents = db_session.query(Intent).filter(Intent.status == "active").all()
        for intent in intents:
            self.semantic_router.register_intent(intent.intent_id, intent.description)
    
    async def route(self, user_input: str, session_history: Optional[List[Dict]] = None) -> IntentDecision:
        """
        路由主入口
        1. 语义路由（单意图高置信度直接返回）
        2. 语义路由检测到低置信度或多意图 → LLM兜底
        """
        start_time = time.time()
        
        # Step 1: 语义路由
        semantic_result = await self.semantic_router.route(user_input, session_history)
        
        # 高置信度单意图，直接返回
        if (semantic_result.routing_path == "semantic" and 
            semantic_result.confidence > self.semantic_router.threshold and
            not semantic_result.is_multi_intent):
            semantic_result.reasoning = f"语义匹配，相似度{semantic_result.confidence:.2f}"
            return semantic_result
        
        # Step 2: LLM兜底（低置信度或多意图）
        candidate_intents = list(self.semantic_router._intent_descriptions.keys())
        
        llm_result = await self.llm_fallback.route(user_input, candidate_intents, session_history)
        
        # 合并语义路由的槽位提取（如果有）
        if semantic_result.extracted_slots:
            llm_result.extracted_slots = semantic_result.extracted_slots
        
        # 记录延迟
        latency_ms = int((time.time() - start_time) * 1000)
        
        return llm_result


# ==================== 便捷函数 ====================

async def recognize_intent(user_input: str, 
                          session_history: Optional[List[Dict]] = None,
                          db_session = None) -> IntentDecision:
    """
    意图识别便捷函数
    """
    router = IntentRouter()
    
    if db_session:
        router.load_intents_from_db(db_session)
    
    return await router.route(user_input, session_history)
