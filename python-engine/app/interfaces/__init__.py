"""
接口定义层 — Protocol-based 依赖反转
"""

from app.interfaces.cache import CacheClient
from app.interfaces.llm import LLMProvider, LLMResponse
from app.interfaces.vectorstore import VectorStore

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "VectorStore",
    "CacheClient",
]
