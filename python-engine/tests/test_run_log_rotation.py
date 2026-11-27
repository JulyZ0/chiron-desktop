"""L27：Windows 生产托管的日志必须追加 + 轮转（旧实现每次启动截断崩溃现场）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run  # noqa: E402


def test_append_keeps_previous_content(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run, "LOG_MAX_BYTES", 1024)
    log = tmp_path / "gateway.stdout.log"
    log.write_text("crash trace\n", encoding="utf-8")

    handle = run.open_log_append(log)
    handle.write("next line\n")
    handle.close()

    text = log.read_text(encoding="utf-8")
    assert "crash trace" in text, "重启不能截断上一次崩溃现场"
    assert "next line" in text


def test_rotation_keeps_bounded_backups(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run, "LOG_MAX_BYTES", 8)
    monkeypatch.setattr(run, "LOG_BACKUPS", 2)
    log = tmp_path / "engine.stdout.log"
    log.write_text("x" * 16, encoding="utf-8")

    run.open_log_append(log).close()
    assert (tmp_path / "engine.stdout.log.1").exists()
    assert log.stat().st_size == 0, "轮转后新文件应从空开始"

    log.write_text("y" * 16, encoding="utf-8")
    run.open_log_append(log).close()
    assert (tmp_path / "engine.stdout.log.2").exists()

    log.write_text("z" * 16, encoding="utf-8")
    run.open_log_append(log).close()
    # 超过保留份数时丢最旧的那份，而不是无限增长
    assert not (tmp_path / "engine.stdout.log.3").exists()
    assert (tmp_path / "engine.stdout.log.2").exists()
