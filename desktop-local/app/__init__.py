"""Chiron 桌面端本地数据栈引导器。

只负责把 embedded PostgreSQL 与 Redis 拉起来，把连接信息交给 Go 网关
（``cmd/chiron``）—— 业务路由一律在网关与 python-engine 里，这里不实现任何业务。
进程入口见 :mod:`app.launcher`。
"""

__version__ = "0.1.0"
