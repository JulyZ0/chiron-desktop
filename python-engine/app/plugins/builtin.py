"""内置插件清单 —— 随应用分发的 MCP 插件，用户无需配置。

## 与「用户插件」的区别

| | 用户插件 | 内置插件 |
|---|---|---|
| 配置来源 | `data/plugins/{user_id}/plugins.json`（Go 网关写入） | 本模块的静态清单 |
| 归属 | 按用户隔离（`owner=user_id`） | 全局（`owner=""`，所有用户可用） |
| 生命周期 | 随用户活跃度增删（pool 25s 轮询） | 随引擎进程启停 |
| 可用性 | 用户不活跃即回收 | 只要二进制存在就常驻 |
| 信任级别 | 第三方代码，凭据不出引擎 | **部署者受信**，且仅限本机只读 |

内置插件仍然受两条既有边界约束，不因为"内置"而放宽：

1. **命令白名单**：`command_allowed_builtin()` 只放行本清单登记的 basename，
   且要求命令是裸 basename 或绝对路径（拒绝相对穿越 / UNC，复用 `_command_path_ok`）。
2. **只读声明**：`read_only=True` 才允许引擎直连（见 `assert_server_connectable`）。

## 为什么 XERJ 需要 `deny_tools`

`--disable-feedback` **只关闭启动横幅，不移除外发工具**（实测：加与不加都是 22 个工具）。
`field_report` / `corpus_feedback` 的 `confirm` 参数默认 `true`，即省略参数就会把内容发往
`https://xerj.org/api/field-report`（GitHub 不可用时）或开 PR —— 这是**出站写操作**，
与"只读检索插件"的定位不符。因此在内置清单里显式拒绝，注册阶段直接不装载。

这比依赖 `use_capability` 隐藏更可靠：隐藏只是"模型看不见"，工具本身仍可被调用；
`deny_tools` 是**根本不注册**。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

#: 内置插件工具名的命名空间前缀标记（供前端/日志区分来源，不参与 MCP 协议）。
BUILTIN_SOURCE = "builtin"

#: XERJ 默认的 ES-compat 端口。与 `xerj.toml` 的 `es_compat_port` 一致，
#: 且**刻意避开** 8080/8081（Go 网关 / gRPC 占用，xerj 的 native REST/gRPC 默认值）。
XERJ_DEFAULT_URL = "http://127.0.0.1:9200"


@dataclass(frozen=True)
class BuiltinPlugin:
    """一个内置 MCP 插件的静态定义。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    version: str = ""
    #: 引擎只直连只读 server；内置插件必须为 True 才会被连接（fail-closed 同样适用）。
    read_only: bool = True
    #: 拒绝装载的工具（MCP **本地名**，不是命名空间后的名字）。
    #: 用于剔除出站/写操作工具 —— 见模块 docstring 关于 XERJ feedback 的说明。
    deny_tools: frozenset[str] = frozenset()
    #: 二进制不存在时展示给用户的安装提示。
    install_hint: str = ""

    def resolve_command(self) -> str | None:
        """解析可执行文件绝对路径；不存在返回 None（内置插件**缺失即跳过**）。"""
        return shutil.which(self.command)

    def is_available(self) -> bool:
        return self.resolve_command() is not None


def _xerj_plugins() -> list[BuiltinPlugin]:
    """XERJ 只读代码检索节点（MCP stdio 客户端 → 本机 ES-compat 接口）。"""
    url = (getattr(settings, "xerj_mcp_url", "") or "").strip() or XERJ_DEFAULT_URL
    return [
        BuiltinPlugin(
            name="xerj",
            command="xerj",
            # `--disable-feedback` 只压横幅；真正的出站工具由 deny_tools 拦截。
            args=("mcp", "--disable-feedback"),
            env={"XERJ_URL": url, "XERJ_DISABLE_FEEDBACK": "true"},
            description="XERJ 只读代码检索（符号级搜索 / 跨项目参考实现 / 语义检索）",
            version="",
            read_only=True,
            # 出站写操作：默认 confirm=true，省略即发送。只读插件不应持有。
            deny_tools=frozenset({"field_report", "corpus_feedback"}),
            install_hint="https://xerj.org/docs/install （或 `xerj` 需在 PATH 上）",
        )
    ]


def builtin_plugins() -> list[BuiltinPlugin]:
    """全部内置插件定义（**不**做可用性过滤）。"""
    return [*_xerj_plugins()]


def available_builtin_plugins() -> list[BuiltinPlugin]:
    """过滤出可执行文件确实存在的内置插件。

    缺失即跳过（而非报错）：内置插件是检索增强，不是引擎启动前提 —— 与
    `run.py` 里 xerj 服务的 `optional=True` 语义保持一致。
    """
    out: list[BuiltinPlugin] = []
    for plugin in builtin_plugins():
        if plugin.is_available():
            out.append(plugin)
        else:
            logger.info(
                "内置插件 %s 跳过：命令 %r 不在 PATH 上（安装：%s）",
                plugin.name,
                plugin.command,
                plugin.install_hint or "见插件文档",
            )
    return out


def builtin_commands() -> frozenset[str]:
    """内置插件允许拉起的命令 basename 集合（供 `command_allowed_builtin` 校验）。"""
    return frozenset(p.command for p in builtin_plugins() if p.command)


def builtin_names() -> frozenset[str]:
    """内置插件名集合（用于 pool 去重：用户配置里同名时以内置为准）。"""
    return frozenset(p.name for p in builtin_plugins())
