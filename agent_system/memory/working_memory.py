"""
工作记忆 - 任务内上下文（SQLite/PostgreSQL持久化）
"""
from typing import Dict, Any, Optional
from datetime import datetime
import json

from agent_system.db.session import db_session
from agent_system.db.models import TaskCheckpoint


class WorkingMemory:
    """工作记忆管理器"""
    
    def __init__(self, thread_id: str, parent_thread_id: Optional[str] = None):
        self.thread_id = thread_id
        self.parent_thread_id = parent_thread_id
        self._context: Dict[str, Any] = {}
        self._step_index: int = 0
        self._status: str = "executing"
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取上下文值"""
        return self._context.get(key, default)
    
    def set(self, key: str, value: Any):
        """设置上下文值"""
        self._context[key] = value
    
    def get_step_result(self, step_id: int) -> Any:
        """获取步骤结果"""
        return self._context.get(f"step_{step_id}_result")
    
    def set_step_result(self, step_id: int, result: Any, status: str = "succeeded"):
        """设置步骤结果"""
        self._context[f"step_{step_id}_result"] = result
        self._context[f"step_{step_id}_status"] = status
    
    def get_step_status(self, step_id: int) -> str:
        """获取步骤状态"""
        return self._context.get(f"step_{step_id}_status", "pending")
    
    @property
    def context(self) -> Dict[str, Any]:
        """获取完整上下文"""
        return self._context.copy()
    
    @property
    def step_index(self) -> int:
        return self._step_index
    
    @step_index.setter
    def step_index(self, value: int):
        self._step_index = value
    
    @property
    def status(self) -> str:
        return self._status
    
    @status.setter
    def status(self, value: str):
        self._status = value
    
    def save_checkpoint(self):
        """保存检查点到数据库"""
        with db_session() as db:
            checkpoint = db.query(TaskCheckpoint).filter(
                TaskCheckpoint.thread_id == self.thread_id
            ).first()
            
            state = {
                "context": self._context,
                "step_index": self._step_index,
                "status": self._status,
            }
            
            if checkpoint:
                checkpoint.state = state
                checkpoint.step_index = self._step_index
                checkpoint.status = self._status
                checkpoint.updated_at = datetime.now()
            else:
                checkpoint = TaskCheckpoint(
                    thread_id=self.thread_id,
                    parent_thread_id=self.parent_thread_id,
                    state=state,
                    step_index=self._step_index,
                    status=self._status
                )
                db.add(checkpoint)
    
    def load_checkpoint(self) -> bool:
        """从数据库加载检查点"""
        with db_session() as db:
            checkpoint = db.query(TaskCheckpoint).filter(
                TaskCheckpoint.thread_id == self.thread_id
            ).first()
            
            if checkpoint:
                state = checkpoint.state
                self._context = state.get("context", {})
                self._step_index = state.get("step_index", 0)
                self._status = state.get("status", "executing")
                return True
            return False
    
    def delete_checkpoint(self):
        """删除检查点"""
        with db_session() as db:
            db.query(TaskCheckpoint).filter(
                TaskCheckpoint.thread_id == self.thread_id
            ).delete()
    
    def compress_context(self, keep_last_n: int = 2):
        """
        上下文压缩
        保留最近N步完整数据，早期步骤压缩为摘要
        """
        step_keys = sorted([k for k in self._context.keys() 
                          if k.startswith("step_") and k.endswith("_result")],
                         key=lambda x: int(x.split("_")[1]))
        
        if len(step_keys) <= keep_last_n:
            return
        
        to_compress = step_keys[:-keep_last_n]
        
        for step_key in to_compress:
            step_id = step_key.split("_")[1]
            original = self._context.get(step_key)
            
            # 压缩为摘要（保留关键信息）
            summary = {
                "compressed": True,
                "original_step": step_id,
                "summary": str(original)[:500],  # 截断
                "compressed_at": datetime.now().isoformat()
            }
            
            self._context[f"summary_{step_key}"] = summary
            del self._context[step_key]
            
            # 同时删除状态
            status_key = f"step_{step_id}_status"
            if status_key in self._context:
                del self._context[status_key]


def create_working_memory(thread_id: str, parent_thread_id: Optional[str] = None) -> WorkingMemory:
    """创建工作记忆"""
    return WorkingMemory(thread_id, parent_thread_id)


def restore_working_memory(thread_id: str) -> Optional[WorkingMemory]:
    """恢复工作记忆"""
    memory = WorkingMemory(thread_id)
    if memory.load_checkpoint():
        return memory
    return None
