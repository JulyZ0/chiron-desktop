"""常驻 LSP（语言服务器）工具包（PRD G23）。

四个模块，各管一件事：

* :mod:`app.tools.lsp.config` —— 语言服务器命令**只**从环境变量读
  （`CHIRON_LSP_<LANG>`）；没配置就明确报错，不内置、不猜测第三方命令。
* :mod:`app.tools.lsp.client` —— JSON-RPC over stdio：`Content-Length` 分帧、
  id 配对、超时、错误归一。**不复用** `app/mcp/client.py`（那边是按行分帧）。
* :mod:`app.tools.lsp.session` —— 会话管理：按 (项目根, 语言) 复用、懒启动、
  串行查询（并发上限 1）、请求超时 SIGTERM→SIGKILL、卸载时先撤路由再关进程。
  进程经既有的隔离入口（`sandboxed_env` / `child_launch_argv` / `attach_job`）启动。
* :mod:`app.tools.lsp.tools` —— 注册 `lsp_definition` 与 `lsp_diagnostics`。

**工具注册在 `app.tools.lsp.tools` 里**（`import app.tools.lsp` 即触发），这与仓库
其它工具模块的约定一致：注册的唯一入口是 `app/tools/__init__.py` 的那串 import，
不要在别处再抄一份清单。
"""

from __future__ import annotations

from app.tools.lsp import tools  # noqa: F401 — 导入即注册（lsp_definition / lsp_diagnostics）

__all__ = ["tools"]
