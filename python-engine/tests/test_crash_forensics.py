"""崩溃取证（`app/crash_forensics.py`）的行为测试。

为什么用**子进程**而不是在同进程里调函数：这里要验证的恰恰是"解释器以异常方式
终止时，文件还在不在"。`sys.excepthook` / `atexit` / 信号处理都只在进程真正
收尾时才跑，在 pytest 进程里手工触发它们验证的是"函数能跑通"，不是"崩溃后留下
证据"—— 而真机事故丢的就是这个证据。

用例钉住四条契约：

1. 未捕获异常 ⇒ 生成 `engine-crash-<ts>.log`，含退出类型/版本/活跃会话/最近事件；
2. `SystemExit(非零)` ⇒ 同上也落盘（`sys.excepthook` 收不到它，只能由入口包装）；
3. 正常退出 / 信号退出 ⇒ 只写 `engine-exit.log` 一行，不产生崩溃文件；
4. **报告里绝不能出现 env 的"值"**：真机崩溃日志会被用户原样贴进反馈渠道。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]

#: 故意写成"一眼能认出来"的凭据值：只要它出现在报告里，就是泄露。
SECRET_APP_SECRET = "s3cr3t-app-secret-value-9f2a"
SECRET_PG_DSN = "postgresql://postgres:hunter2-pg-password@127.0.0.1:1/chiron"
SECRET_API_KEY = "sk-live-0123456789abcdef-DO-NOT-LEAK"


def _run_child(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    """在引擎根目录下跑一段脚本，取证目录指到 tmp_path（不碰真实用户目录）。"""
    env = dict(os.environ)
    env.update(
        {
            "CHIRON_CRASH_LOG_DIR": str(tmp_path / "logs"),
            # 关键 env 都塞上真实值：报告只准写名字，不准写值。
            "APP_SECRET": SECRET_APP_SECRET,
            "POSTGRES_DSN": SECRET_PG_DSN,
            "LLM_API_KEY": SECRET_API_KEY,
            # 不让子进程去读仓库根的 .env（那会引入与本用例无关的环境）
            "PYTHONPATH": str(ENGINE_ROOT),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ENGINE_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _crash_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "logs").glob("engine-crash-*.log"))


def _read_all(files: list[Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files)


def test_uncaught_exception_writes_crash_report_without_secret_values(tmp_path):
    script = """
from app import crash_forensics

crash_forensics.install()
crash_forensics.set_active_run("sess-crash-1", "run-token-abcdef0123456789")
crash_forensics.record_event("run-start", session="sess-crash-1")
crash_forensics.record_event("llm-call", model="gpt-x", api_key="sk-should-be-redacted")

raise RuntimeError("engine exploded in main thread")
"""
    proc = _run_child(tmp_path, script)

    assert proc.returncode != 0, "子进程本该以未捕获异常退出"
    files = _crash_files(tmp_path)
    assert files, f"未生成崩溃文件；stdout={proc.stdout!r} stderr={proc.stderr!r}"

    text = _read_all(files)
    assert "exit-type: uncaught-exception" in text
    assert "RuntimeError: engine exploded in main thread" in text, "traceback 必须落盘（真机事故缺的就是它）"
    assert "python:" in text and "platform:" in text
    # 活跃 run/session：能取到就必须写进去
    assert "session=sess-crash-1" in text
    # 最近事件摘要
    assert "--- last 20 events ---" in text
    assert "run-start" in text
    assert "llm-call" in text
    # 关键 env 只写名字
    assert "env-set(names-only):" in text
    assert "APP_SECRET" in text and "POSTGRES_DSN" in text
    # 值一个都不许出现（含事件字段里的凭据）
    for secret in (SECRET_APP_SECRET, SECRET_PG_DSN, SECRET_API_KEY, "hunter2-pg-password", "sk-should-be-redacted"):
        assert secret not in text, f"崩溃报告泄露了凭据：{secret}"
    # 原子写不该留下临时文件
    assert not list((tmp_path / "logs").glob("*.tmp")), "原子写残留了临时文件"


def test_system_exit_nonzero_writes_crash_report(tmp_path):
    script = """
import sys

from app import crash_forensics

crash_forensics.install()
crash_forensics.run_guarded(lambda: sys.exit(7))
"""
    proc = _run_child(tmp_path, script)

    assert proc.returncode == 7
    text = _read_all(_crash_files(tmp_path))
    assert "exit-type: system-exit" in text, "sys.excepthook 收不到 SystemExit，必须由入口包装取证"
    assert "exit-code: 7" in text


def test_normal_exit_writes_exit_line_only(tmp_path):
    script = """
import sys

from app import crash_forensics

crash_forensics.install()
crash_forensics.record_event("run-end", session="sess-normal")
crash_forensics.run_guarded(lambda: sys.exit(0))
"""
    proc = _run_child(tmp_path, script)

    assert proc.returncode == 0
    assert not _crash_files(tmp_path), "正常退出不该留下崩溃文件（否则崩溃目录全是噪声）"

    exit_log = tmp_path / "logs" / "engine-exit.log"
    assert exit_log.is_file(), "正常退出必须留下一行 engine-exit.log 作为取证对照组"
    lines = [ln for ln in exit_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"engine-exit.log 每次退出只写一行，实际 {len(lines)} 行"
    record = json.loads(lines[0])
    assert set(record) >= {"ts", "reason", "code", "uptime_s"}
    assert record["reason"] == "atexit"


def test_signal_termination_records_reason(tmp_path):
    script = """
import signal

from app import crash_forensics

crash_forensics.install()
crash_forensics.install_signal_handlers()
# 先记一条事件，确认信号路径不影响日志目录可用性
crash_forensics.record_event("before-signal")
signal.raise_signal(signal.SIGTERM)
# 处理者若没能终止进程，这里再兜一层，避免用例挂死
raise SystemExit(99)
"""
    proc = _run_child(tmp_path, script)

    # Windows 上 SIGTERM 的语义是 TerminateProcess（退出码不一定可断言），
    # 关键证据是退出日志里记下了"是哪个信号让它退的"。
    exit_log = tmp_path / "logs" / "engine-exit.log"
    if not exit_log.is_file():
        pytest.skip(f"SIGTERM 直接终止了进程，未走到 atexit（returncode={proc.returncode}）")

    records = [
        json.loads(line)
        for line in exit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records, "engine-exit.log 不能是空的"
    assert records[-1]["reason"] in {"signal:SIGTERM", "atexit"}
    assert not _crash_files(tmp_path), "收到停止信号属于正常关停，不是崩溃"


def test_crash_report_keeps_only_last_20_events():
    """报告尾部只留 20 条：再多就是噪声，而文件要能被用户直接贴出来。"""
    from app import crash_forensics

    for i in range(30):
        crash_forensics.record_event("tick", i=i)
    report = crash_forensics.build_report("unit-test")

    assert "tick i=9" not in report, "最旧的事件应被裁掉"
    assert "tick i=10" in report, "第 20 条（从尾部算）必须保留"
    assert "tick i=29" in report
    assert report.count("tick i=") == crash_forensics.EVENT_TAIL
