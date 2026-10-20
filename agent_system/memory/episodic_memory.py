"""
情景记忆 - 跨任务经验（ChromaDB，7天TTL，用户隔离，模式聚合）
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import hashlib
import json
import uuid

from agent_system.config.settings import get_settings
from agent_system.utils.embedding_client import get_embedding_client, BaseEmbeddingClient

settings = get_settings()


class EpisodePattern:
    """情景模式（聚合存储）"""
    
    def __init__(self, 
                 fingerprint: str,
                 intent_id: str,
                 user_id: str,
                 skeleton_hash: str,
                 param_signature: str,
                 skeleton_variants: List[Dict[str, Any]] = None,
                 success_count: int = 0,
                 failure_count: int = 0,
                 avg_duration_ms: float = 0.0,
                 first_seen: Optional[datetime] = None,
                 last_seen: Optional[datetime] = None):
        self.fingerprint = fingerprint
        self.intent_id = intent_id
        self.user_id = user_id
        self.skeleton_hash = skeleton_hash
        self.param_signature = param_signature
        self.skeleton_variants = skeleton_variants or []
        self.success_count = success_count
        self.failure_count = failure_count
        self.avg_duration_ms = avg_duration_ms
        self.first_seen = first_seen or datetime.now()
        self.last_seen = last_seen or datetime.now()
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "intent_id": self.intent_id,
            "user_id": self.user_id,
            "skeleton_hash": self.skeleton_hash,
            "param_signature": self.param_signature,
            "skeleton_variants": json.dumps(self.skeleton_variants),  # JSON序列化
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "avg_duration_ms": self.avg_duration_ms,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EpisodePattern":
        skeleton_variants = data.get("skeleton_variants", "[]")
        if isinstance(skeleton_variants, str):
            skeleton_variants = json.loads(skeleton_variants)
        
        return cls(
            fingerprint=data["fingerprint"],
            intent_id=data["intent_id"],
            user_id=data["user_id"],
            skeleton_hash=data["skeleton_hash"],
            param_signature=data["param_signature"],
            skeleton_variants=skeleton_variants,
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
            avg_duration_ms=data.get("avg_duration_ms", 0.0),
            first_seen=datetime.fromisoformat(data["first_seen"]) if data.get("first_seen") else None,
            last_seen=datetime.fromisoformat(data["last_seen"]) if data.get("last_seen") else None,
        )


class EpisodicMemory:
    """情景记忆管理器"""
    
    def __init__(self, 
                 embedding_client: Optional[BaseEmbeddingClient] = None,
                 persist_dir: Optional[str] = None):
        self.embedding = embedding_client or get_embedding_client()
        self.persist_dir = persist_dir or settings.CHROMA_PERSIST_DIR
        self.ttl_days = settings.EPISODIC_MEMORY_TTL_DAYS
        self.per_intent_limit = settings.EPISODIC_MEMORY_PER_INTENT_LIMIT
        
        self._client = None
        self._collection = None
    
    def _get_collection(self):
        """获取ChromaDB集合"""
        if self._collection is None:
            try:
                import chromadb
                self._client = chromadb.PersistentClient(path=self.persist_dir)
                self._collection = self._client.get_or_create_collection(
                    name="episodic_memory",
                    metadata={"hnsw:space": "cosine"}
                )
            except ImportError:
                raise RuntimeError("chromadb not installed. Install with: pip install chromadb")
        return self._collection
    
    def _generate_fingerprint(self, 
                             intent_id: str, 
                             skeleton: List[str], 
                             extracted_slots: Dict[str, Any]) -> str:
        """生成骨架指纹（三层结构）"""
        # 第一层：意图标识
        intent_prefix = intent_id
        
        # 第二层：骨架结构哈希
        skeleton_str = json.dumps(skeleton, sort_keys=True)
        skeleton_hash = hashlib.sha256(skeleton_str.encode()).hexdigest()[:16]
        
        # 第三层：参数特征摘要
        param_signature = self._generate_param_signature(extracted_slots)
        
        return f"{intent_prefix}:{skeleton_hash}:{param_signature}"
    
    def _generate_param_signature(self, extracted_slots: Dict[str, Any]) -> str:
        """生成参数特征摘要"""
        features = []
        
        # 人员数量
        person_count = len(extracted_slots.get("person", []))
        if person_count == 1:
            features.append("person_1")
        elif person_count <= 10:
            features.append("person_small")
        else:
            features.append("person_large")
        
        # 时间类型
        if extracted_slots.get("datetime") or extracted_slots.get("date"):
            features.append("timed")
        else:
            features.append("immediate")
        
        return "_".join(features) if features else "default"
    
    async def store_episode(self,
                           user_id: str,
                           intent_id: str,
                           skeleton: List[str],
                           extracted_slots: Dict[str, Any],
                           success: bool,
                           duration_ms: int,
                           failure_info: Optional[Dict[str, Any]] = None,
                           trace_id: Optional[str] = None):
        """
        存储情景记忆（模式聚合）
        """
        collection = self._get_collection()
        
        # 生成指纹
        fingerprint = self._generate_fingerprint(intent_id, skeleton, extracted_slots)
        pattern_id = f"pattern_{user_id}_{fingerprint}"
        
        # 查询是否已存在
        existing = collection.get(ids=[pattern_id])
        
        if existing and existing["ids"]:
            # 更新现有模式
            metadata = existing["metadatas"][0]
            
            metadata["success_count"] = metadata.get("success_count", 0) + (1 if success else 0)
            metadata["failure_count"] = metadata.get("failure_count", 0) + (0 if success else 1)
            
            # 更新平均耗时
            total = metadata["success_count"] + metadata["failure_count"]
            old_avg = metadata.get("avg_duration_ms", 0)
            metadata["avg_duration_ms"] = (old_avg * (total - 1) + duration_ms) / total
            
            metadata["last_seen"] = datetime.now().isoformat()
            
            # 更新骨架变体
            skeleton_variants_str = metadata.get("skeleton_variants", "[]")
            if isinstance(skeleton_variants_str, str):
                skeleton_variants = json.loads(skeleton_variants_str)
            else:
                skeleton_variants = skeleton_variants_str
            
            variant_found = False
            for variant in skeleton_variants:
                if variant.get("skeleton") == skeleton:
                    if success:
                        variant["success_count"] = variant.get("success_count", 0) + 1
                        if failure_info:
                            variant["is_fixed"] = True
                            variant["failure_context"] = failure_info
                    else:
                        variant["failure_count"] = variant.get("failure_count", 0) + 1
                        common_failures = variant.get("common_failures", [])
                        common_failures.append({
                            "reason": failure_info.get("reason") if failure_info else "unknown",
                            "count": 1,
                            "timestamp": datetime.now().isoformat()
                        })
                        variant["common_failures"] = common_failures
                    variant_found = True
                    break
            
            if not variant_found:
                new_variant = {
                    "skeleton": skeleton,
                    "success_count": 1 if success else 0,
                    "failure_count": 0 if success else 1,
                    "is_fixed": success and failure_info is not None,
                    "failure_context": failure_info if failure_info else {},
                    "common_failures": []
                }
                skeleton_variants.append(new_variant)
            
            metadata["skeleton_variants"] = json.dumps(skeleton_variants)
            
            collection.update(ids=[pattern_id], metadatas=[metadata])
        else:
            # 插入新模式
            pattern = EpisodePattern(
                fingerprint=fingerprint,
                intent_id=intent_id,
                user_id=user_id,
                skeleton_hash=fingerprint.split(":")[1],
                param_signature=fingerprint.split(":")[2],
                skeleton_variants=[{
                    "skeleton": skeleton,
                    "success_count": 1 if success else 0,
                    "failure_count": 0 if success else 1,
                    "is_fixed": success and failure_info is not None,
                    "failure_context": failure_info if failure_info else {},
                    "common_failures": []
                }],
                success_count=1 if success else 0,
                failure_count=0 if success else 1,
                avg_duration_ms=float(duration_ms),
                first_seen=datetime.now(),
                last_seen=datetime.now()
            )
            
            # 生成文档描述（用于语义检索）
            description = f"任务模式:{intent_id} 骨架:{'→'.join(skeleton)} 成功:{1 if success else 0}次"
            
            # 编码
            embedding = await self.embedding.encode([description])
            
            collection.add(
                ids=[pattern_id],
                embeddings=embedding,
                documents=[description],
                metadatas=[pattern.to_dict()]
            )
    
    async def retrieve_patterns(self,
                                user_id: str,
                                intent_id: str,
                                skeleton: Optional[List[str]] = None,
                                extracted_slots: Optional[Dict[str, Any]] = None,
                                top_k: int = 3) -> List[Dict[str, Any]]:
        """
        检索情景记忆（三级匹配）
        """
        collection = self._get_collection()
        
        # Level 1: 指纹精确匹配
        if skeleton and extracted_slots:
            fingerprint = self._generate_fingerprint(intent_id, skeleton, extracted_slots)
            pattern_id = f"pattern_{user_id}_{fingerprint}"
            
            exact = collection.get(ids=[pattern_id])
            if exact and exact["ids"]:
                return [{
                    "source": "fingerprint_exact",
                    "confidence": 1.0,
                    "pattern": exact["metadatas"][0]
                }]
        
        # Level 2: 指纹模糊匹配（骨架相同，参数特征不同）
        if skeleton:
            skeleton_str = json.dumps(skeleton, sort_keys=True)
            skeleton_hash = hashlib.sha256(skeleton_str.encode()).hexdigest()[:16]
            
            # 查询所有该用户的模式
            all_patterns = collection.get(
                where={"$and": [{"user_id": user_id}, {"intent_id": intent_id}]}
            )
            
            fuzzy_matches = []
            if all_patterns and all_patterns["ids"]:
                for i, metadata in enumerate(all_patterns["metadatas"]):
                    if metadata.get("skeleton_hash") == skeleton_hash:
                        fuzzy_matches.append({
                            "source": "fingerprint_fuzzy",
                            "confidence": 0.8,
                            "pattern": metadata
                        })
            
            if fuzzy_matches:
                return fuzzy_matches[:top_k]
        
        # Level 3: 语义向量检索
        query_text = f"任务模式:{intent_id}"
        query_embedding = await self.embedding.encode([query_text])
        
        # 先按用户限定检索范围，再筛选成功的同意图经验。
        semantic_results = collection.query(
            query_embeddings=query_embedding,
            n_results=top_k * 2,  # 多取一些，后面过滤
            where={"user_id": user_id}
        )
        
        results = []
        if semantic_results and semantic_results["ids"]:
            for i, metadata in enumerate(semantic_results["metadatas"][0]):
                # 过滤：intent_id匹配且success_count>0
                if (metadata.get("intent_id") == intent_id and 
                    metadata.get("success_count", 0) > 0):
                    results.append({
                        "source": "semantic_vector",
                        "confidence": 0.6,
                        "pattern": metadata,
                        "distance": semantic_results["distances"][0][i] if semantic_results.get("distances") else None
                    })
        
        return results[:top_k]
    
    async def cleanup_expired(self):
        """清理过期记忆（7天TTL）"""
        collection = self._get_collection()
        
        cutoff = (datetime.now() - timedelta(days=self.ttl_days)).isoformat()
        
        # ChromaDB不直接支持按时间删除，需要查询后删除
        # 这里简化实现
        pass
    
    async def get_recommended_skeleton(self,
                                       user_id: str,
                                       intent_id: str,
                                       extracted_slots: Optional[Dict[str, Any]] = None) -> Optional[List[str]]:
        """
        获取推荐骨架
        优先级：修复后的骨架 > 高成功率骨架 > 程序记忆模板
        """
        patterns = await self.retrieve_patterns(user_id, intent_id, extracted_slots=extracted_slots)
        
        if not patterns:
            return None
        
        # 按优先级排序
        best_pattern = None
        best_score = -1
        
        for p in patterns:
            pattern = p["pattern"]
            score = p["confidence"]
            
            # 修复后的骨架优先
            skeleton_variants = pattern.get("skeleton_variants", [])
            if isinstance(skeleton_variants, str):
                skeleton_variants = json.loads(skeleton_variants)
            
            for variant in skeleton_variants:
                if variant.get("is_fixed"):
                    score += 0.5
                    break
            
            # 高成功率优先
            total = pattern.get("success_count", 0) + pattern.get("failure_count", 0)
            if total > 0:
                success_rate = pattern.get("success_count", 0) / total
                score += success_rate * 0.3
            
            if score > best_score:
                best_score = score
                best_pattern = pattern
        
        if best_pattern:
            # 返回成功率最高的骨架变体
            variants = best_pattern.get("skeleton_variants", [])
            if isinstance(variants, str):
                variants = json.loads(variants)
            if variants:
                best_variant = max(variants, key=lambda v: v.get("success_count", 0) - v.get("failure_count", 0))
                return best_variant.get("skeleton")
        
        return None


# ==================== 便捷函数 ====================

async def store_episode(user_id: str,
                       intent_id: str,
                       skeleton: List[str],
                       extracted_slots: Dict[str, Any],
                       success: bool,
                       duration_ms: int,
                       failure_info: Optional[Dict[str, Any]] = None):
    """存储情景记忆便捷函数"""
    memory = EpisodicMemory()
    await memory.store_episode(user_id, intent_id, skeleton, extracted_slots, success, duration_ms, failure_info)


async def get_recommended_skeleton(user_id: str,
                                   intent_id: str,
                                   extracted_slots: Optional[Dict[str, Any]] = None) -> Optional[List[str]]:
    """获取推荐骨架便捷函数"""
    memory = EpisodicMemory()
    return await memory.get_recommended_skeleton(user_id, intent_id, extracted_slots)
