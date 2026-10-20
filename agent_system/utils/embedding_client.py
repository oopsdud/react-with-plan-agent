"""
Embedding 客户端 - 预留接口
"""
from abc import ABC, abstractmethod
from typing import List, Optional
import hashlib
import numpy as np

from agent_system.config.settings import get_settings

settings = get_settings()


class BaseEmbeddingClient(ABC):
    """Embedding客户端基类"""
    
    @abstractmethod
    async def encode(self, texts: List[str]) -> List[List[float]]:
        """编码文本为向量"""
        pass
    
    @abstractmethod
    def cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        pass


class LocalEmbeddingClient(BaseEmbeddingClient):
    """本地Embedding客户端（sentence-transformers）"""
    
    def __init__(self, model_name: str = "BAAI/bge-m3"):
        self.model_name = model_name
        self._model = None
    
    def _load_model(self):
        """延迟加载模型"""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
            except ImportError:
                raise EmbeddingError(
                    "sentence-transformers not installed. "
                    "Install with: pip install sentence-transformers"
                )
        return self._model
    
    async def encode(self, texts: List[str]) -> List[List[float]]:
        """编码"""
        model = self._load_model()
        embeddings = model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()
    
    def cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """余弦相似度"""
        v1 = np.array(vec1)
        v2 = np.array(vec2)
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))


class APIEmbeddingClient(BaseEmbeddingClient):
    """API Embedding客户端（OpenAI / 其他）"""
    
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.base_url = base_url or settings.EMBEDDING_BASE_URL
    
    async def encode(self, texts: List[str]) -> List[List[float]]:
        """API编码"""
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            
            response = await client.embeddings.create(
                model=settings.EMBEDDING_MODEL,
                input=texts
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            raise EmbeddingError(f"API embedding failed: {str(e)}")
    
    def cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """余弦相似度"""
        v1 = np.array(vec1)
        v2 = np.array(vec2)
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))


class MockEmbeddingClient(BaseEmbeddingClient):
    """Mock Embedding - 用于无模型时验证流程"""
    
    def __init__(self, dim: int = 768):
        self.dim = dim
    
    async def encode(self, texts: List[str]) -> List[List[float]]:
        """Mock编码 - 基于文本哈希生成确定性向量"""
        embeddings = []
        for text in texts:
            # 使用文本哈希生成伪随机但确定性的向量
            hash_obj = hashlib.sha256(text.encode())
            hash_bytes = hash_obj.digest()
            
            # 扩展到目标维度
            vector = []
            for i in range(self.dim):
                byte_idx = i % len(hash_bytes)
                vector.append((hash_bytes[byte_idx] / 255.0) * 2 - 1)  # [-1, 1]
            
            # 归一化
            vec = np.array(vector)
            vec = vec / np.linalg.norm(vec)
            embeddings.append(vec.tolist())
        
        return embeddings
    
    def cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """余弦相似度"""
        v1 = np.array(vec1)
        v2 = np.array(vec2)
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))


class EmbeddingError(Exception):
    """Embedding错误"""
    pass


def get_embedding_client() -> BaseEmbeddingClient:
    """获取Embedding客户端"""
    provider = settings.EMBEDDING_PROVIDER.lower()
    
    if provider == "api":
        try:
            return APIEmbeddingClient()
        except Exception:
            return MockEmbeddingClient()
    elif provider == "local":
        # 本地模式：尝试加载sentence-transformers，失败则使用Mock
        try:
            client = LocalEmbeddingClient(settings.EMBEDDING_MODEL)
            # 测试加载
            client._load_model()
            return client
        except Exception:
            return MockEmbeddingClient()
    else:
        return MockEmbeddingClient()
