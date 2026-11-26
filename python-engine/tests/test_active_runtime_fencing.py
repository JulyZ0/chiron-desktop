"""L16：会话级 run 注册表只允许"谁注册谁注销"。

修前 `_ACTIVE_RUNTIMES.pop(session_id)` 无条件删除：同会话出现第二个 run 时
（互斥失效、快速重跑），先结束的那个会把后者的注册项删掉，审批端点随即回
"no active agent"，用户点了批准却只能等到超时。
"""

from __future__ import annotations

from app import main


def _seed(session: str, token: str) -> None:
    main._ACTIVE_RUNTIMES[session] = (object(), "u-1", token)  # type: ignore[assignment]


def test_finishing_old_run_does_not_remove_new_registration() -> None:
    main._ACTIVE_RUNTIMES.clear()
    _seed("s-1", "new-token")

    main._drop_active_runtime("s-1", "old-token")

    assert "s-1" in main._ACTIVE_RUNTIMES
    assert main._ACTIVE_RUNTIMES["s-1"][2] == "new-token"


def test_owner_token_removes_registration() -> None:
    main._ACTIVE_RUNTIMES.clear()
    _seed("s-1", "new-token")

    main._drop_active_runtime("s-1", "new-token")

    assert "s-1" not in main._ACTIVE_RUNTIMES


def test_missing_registration_is_idempotent() -> None:
    main._ACTIVE_RUNTIMES.clear()
    main._drop_active_runtime("s-x", "token")  # 不应抛错
    assert "s-x" not in main._ACTIVE_RUNTIMES
