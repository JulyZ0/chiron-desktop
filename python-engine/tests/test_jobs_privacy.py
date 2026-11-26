"""L05：no_retention 下后台任务必须拒绝，而不是"输出写 24h 再声称没留存"。"""

from __future__ import annotations

import pytest

from app.middleware.privacy_middleware import NO_RETENTION, privacy_mode_var
from app.tools import jobs


@pytest.fixture(autouse=True)
def _reset_privacy():
    token = privacy_mode_var.set("")
    yield
    privacy_mode_var.reset(token)


@pytest.mark.asyncio
async def test_background_job_rejected_under_no_retention(monkeypatch) -> None:
    privacy_mode_var.set(NO_RETENTION)

    async def _boom() -> None:
        raise AssertionError("no_retention 下不应尝试连接 Redis / 入队")

    monkeypatch.setattr(jobs, "_get_redis", _boom)
    out = await jobs.run_in_background("echo hi")

    assert "error" in out
    assert "no_retention" in out["error"]
    assert "job_id" not in out
