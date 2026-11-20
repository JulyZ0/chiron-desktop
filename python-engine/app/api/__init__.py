"""Python 工具服务 API（Phase 1）。"""

from fastapi import APIRouter

from app.api.agents import router as agents_router
from app.api.audio import router as audio_router
from app.api.capabilities import router as capabilities_router
from app.api.checkpoints import router as checkpoints_router
from app.api.context import router as context_router
from app.api.deps import router as deps_router
from app.api.desktop_infer import router as desktop_infer_router
from app.api.interactions import router as interactions_router
from app.api.internal_diagnostics import router as internal_diagnostics_router
from app.api.internal_knowledge import router as internal_knowledge_router
from app.api.internal_market import router as internal_market_router
from app.api.internal_subagent import router as internal_subagent_router
from app.api.isolation import router as isolation_router
from app.api.knowledge import router as knowledge_router
from app.api.memory import router as memory_router
from app.api.openai_compat import router as openai_compat_router
from app.api.output_controls import router as output_controls_router
from app.api.plugins import router as plugins_router
from app.api.skills import router as skills_router
from app.api.superagent import router as superagent_router
from app.api.system import router as system_router
from app.api.tools import router as tools_router
from app.api.unified_executor import router as chat_router

api_router = APIRouter()
api_router.include_router(tools_router)
api_router.include_router(agents_router)
api_router.include_router(internal_subagent_router)
api_router.include_router(internal_market_router)
api_router.include_router(internal_knowledge_router)
# 后台「自诊断」：必需表存在性（只读 information_schema，清单与启动自检同源）
api_router.include_router(internal_diagnostics_router)
api_router.include_router(interactions_router)
api_router.include_router(skills_router)
api_router.include_router(knowledge_router)
api_router.include_router(plugins_router)
api_router.include_router(chat_router)
api_router.include_router(capabilities_router)
api_router.include_router(context_router)
api_router.include_router(desktop_infer_router)
api_router.include_router(openai_compat_router)
api_router.include_router(memory_router)
api_router.include_router(output_controls_router)
api_router.include_router(system_router)
api_router.include_router(isolation_router)
api_router.include_router(checkpoints_router)
api_router.include_router(superagent_router)
# 本地语音转写（POST /v1/audio/transcribe，见 app/api/audio.py）
api_router.include_router(audio_router)
# 后台「Python 依赖管理」：pip list / PyPI 搜索 / 安装任务 / 卸载（内部 token 鉴权）
api_router.include_router(deps_router)
