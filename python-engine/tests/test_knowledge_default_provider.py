# 内置检索服务（pageindex-service）可用时，知识库默认走自托管 provider ——
# 用户不必先进「知识库设置」手动选择，否则桌面端开箱即用变成"必须先配一次"。
#
# 守住的另一条：没有内置服务时**不猜**（返回空串），由调用方给出可操作提示；
# 用户显式保存过的 provider 永远优先于默认值。
#
# 先 import app.tools：settings_store 在模块级依赖 app.tools.context，而 app.tools
# 的包初始化会反过来拉取 knowledge.providers.registry（它要 UserKnowledgeSettings）。
# 真实启动顺序里 app.tools 总是先就绪，测试按同一顺序导入即可避免误报的循环导入。
import app.tools  # noqa: F401
from app.knowledge import settings_store as ss
from app.knowledge.providers.base import PROVIDER_CLOUD, PROVIDER_SELFHOSTED


def test_default_provider_follows_builtin_service(monkeypatch):
    monkeypatch.setattr(ss.settings, "knowledge_service_url", "http://127.0.0.1:49321", raising=False)
    assert ss.builtin_service_url() == "http://127.0.0.1:49321"
    assert ss.default_provider() == PROVIDER_SELFHOSTED
    assert ss.effective_provider_name(None) == PROVIDER_SELFHOSTED


def test_no_builtin_service_means_no_guess(monkeypatch):
    monkeypatch.setattr(ss.settings, "knowledge_service_url", "", raising=False)
    assert ss.default_provider() == ""
    assert ss.effective_provider_name(None) == ""


def test_explicit_provider_beats_default(monkeypatch):
    monkeypatch.setattr(ss.settings, "knowledge_service_url", "http://127.0.0.1:1", raising=False)
    stored = ss.UserKnowledgeSettings(user_id="u1", tenant_id=1, provider=PROVIDER_CLOUD)
    assert ss.effective_provider_name(stored) == PROVIDER_CLOUD


def test_blank_service_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setattr(ss.settings, "knowledge_service_url", "   ", raising=False)
    assert ss.builtin_service_url() == ""
    assert ss.default_provider() == ""
