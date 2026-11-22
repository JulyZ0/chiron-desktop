"""``app.runtime.rkey`` 的前缀口径。

与引擎 ``app/redis_keys.py`` / Go ``internal/db/redis_key.go`` 必须逐字一致：
前缀本身带尾冒号（``dev:``），拼接**不再补冒号**。此前这里写成 ``f"{prefix}:{name}"``，
一旦配置前缀就得到 ``dev::engine:tasks:kb`` —— pageindex-worker 永远消费不到索引任务，
界面表现为"索引一直停在 indexing"（只在配置了 REDIS_KEY_PREFIX 的部署里发作）。
"""

from __future__ import annotations

from app.runtime import rkey


def test_rkey_without_prefix(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_KEY_PREFIX", raising=False)
    assert rkey("engine:tasks:kb") == "engine:tasks:kb"


def test_rkey_prefix_is_concatenated_not_colon_separated(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_KEY_PREFIX", "dev:")
    assert rkey("engine:tasks:kb") == "dev:engine:tasks:kb"
