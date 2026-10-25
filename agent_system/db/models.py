"""
数据库模型定义
"""
from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, Text, JSON, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
import uuid

Base = declarative_base()


def generate_uuid():
    return str(uuid.uuid4())


# ==================== 意图配置（SQLite） ====================

class Intent(Base):
    """意图元数据"""
    __tablename__ = "intents"
    
    intent_id = Column(String(100), primary_key=True)
    description = Column(Text, nullable=False)  # 用于语义匹配
    complexity = Column(String(20), nullable=False)  # simple / complex
    
    # Simple通道专用
    tool_name = Column(String(100), nullable=True)
    param_patterns = Column(JSON, nullable=True)  # 正则提取模式
    
    # Complex通道专用
    few_shot_examples = Column(JSON, nullable=True)  # 骨架示例
    
    # 通用
    required_params = Column(JSON, nullable=True)
    risk_level = Column(String(20), default="low")  # low / medium / high
    status = Column(String(20), default="active")
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class IntentSkeleton(Base):
    """成功任务骨架（Few-shot来源）"""
    __tablename__ = "intent_skeletons"
    
    skeleton_id = Column(String(64), primary_key=True, default=generate_uuid)
    intent_id = Column(String(100), ForeignKey("intents.intent_id"), nullable=False)
    original_input = Column(Text, nullable=False)
    skeleton_steps = Column(JSON, nullable=False)  # ["step1", "step2", ...]
    success_count = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())


class MultiIntentRule(Base):
    """多意图拆分规则"""
    __tablename__ = "multi_intent_rules"
    
    rule_id = Column(String(64), primary_key=True, default=generate_uuid)
    connector = Column(String(20), nullable=False)  # "并", "然后", "再"
    intent_types = Column(JSON, nullable=False)  # ["notify", "ingest"]
    dependency_type = Column(String(20), nullable=False)  # parallel / serial
    created_at = Column(DateTime, server_default=func.now())


# ==================== 工作记忆（Checkpoint） ====================

class TaskCheckpoint(Base):
    """任务检查点（工作记忆持久化）"""
    __tablename__ = "task_checkpoints"
    
    checkpoint_id = Column(String(64), primary_key=True, default=generate_uuid)
    thread_id = Column(String(100), nullable=False, index=True)  # 任务/子任务ID
    parent_thread_id = Column(String(100), nullable=True)  # 父任务ID（多意图时）
    state = Column(JSON, nullable=False)  # AgentState序列化
    step_index = Column(Integer, default=0)  # 当前执行到第几步
    status = Column(String(20), default="executing")  # executing / completed / failed
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ==================== 审计日志（预留接口） ====================

class AuditLog(Base):
    """审计日志（预留）"""
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, index=True)
    step_id = Column(Integer, nullable=True)
    tool_name = Column(String(100), nullable=True)
    input_summary = Column(Text, nullable=True)
    output_summary = Column(Text, nullable=True)
    status = Column(String(20), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class IntentRecognitionLog(Base):
    """意图识别日志（预留）"""
    __tablename__ = "intent_recognition_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, index=True)
    user_input = Column(Text, nullable=False)
    matched_intent = Column(String(100), nullable=True)
    match_score = Column(Float, nullable=True)
    routing_path = Column(String(20), nullable=True)  # simple / complex / fallback / multi_intent
    extracted_slots = Column(JSON, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = 'users'
    id = Column(String(64), primary_key=True, default=generate_uuid)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)

class LoginSession(Base):
    __tablename__ = 'login_sessions'
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    expires_at = Column(Float, nullable=False)

class Conversation(Base):
    __tablename__ = 'conversations'
    id = Column(String(64), primary_key=True, default=generate_uuid)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    title = Column(String(200), nullable=False, default='新会话')
    model = Column(String(500), nullable=True)
    active_run = Column(String(64), nullable=True)
    updated_at = Column(Float, nullable=False)

class ConversationMessage(Base):
    __tablename__ = 'conversation_messages'
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(64), ForeignKey('conversations.id'), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False, default='')
    model = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default='completed')
    result = Column(JSON, nullable=True)
    created_at = Column(Float, nullable=False)


class AgentRun(Base):
    """Durable lifecycle metadata; request payload/history stays in owner-scoped messages."""
    __tablename__ = 'agent_runs'
    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey('users.id'), nullable=False, index=True)
    conversation_id = Column(String(64), ForeignKey('conversations.id'), nullable=False, index=True)
    message_id = Column(Integer, ForeignKey('conversation_messages.id'), nullable=False)
    state = Column(String(20), nullable=False, default='queued', index=True)
    created_at = Column(Float, nullable=False)
    started_at = Column(Float, nullable=True)
    finished_at = Column(Float, nullable=True)
    error_code = Column(String(64), nullable=True)


class RequestDedup(Base):
    __tablename__ = 'request_dedup'
    user_id = Column(String(64), ForeignKey('users.id'), primary_key=True)
    request_id = Column(String(128), primary_key=True)
    fingerprint = Column(String(64), nullable=False)
    run_id = Column(String(64), ForeignKey('agent_runs.id'), nullable=False, index=True)
    expires_at = Column(Float, nullable=False, index=True)
