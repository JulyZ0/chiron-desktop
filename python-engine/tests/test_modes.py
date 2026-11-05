"""Tests for agent modes — ModeConfig 配置表与回退行为。"""
from __future__ import annotations

import dataclasses
import json

import pytest

from app.agent.modes import (
    CORE_TOOL_NAMES,
    AgentMode,
    ModeConfig,
    get_mode_config,
)


class TestGetModeConfig:
    def test_unknown_mode_falls_back_to_normal(self):
        cfg = get_mode_config("bogus-mode")
        assert cfg.mode is AgentMode.NORMAL
        assert cfg.include_context is True
        assert cfg.enable_compaction is True

    def test_empty_mode_falls_back_to_normal(self):
        cfg = get_mode_config(None)
        assert cfg.mode is AgentMode.NORMAL

    def test_normal_mode_defaults(self):
        cfg = get_mode_config("normal")
        assert cfg.persona is None
        assert cfg.include_context is True
        assert cfg.include_tools == CORE_TOOL_NAMES
        assert cfg.extra_tools == frozenset()
        assert cfg.enable_compaction is True

    def test_minimal_mode(self):
        cfg = get_mode_config("minimal")
        assert cfg.persona == "You are a helpful software engineer assistant."
        assert cfg.include_context is False
        assert cfg.enable_compaction is False
        # 极简：仅 3 个工具，无 extra
        assert cfg.include_tools == frozenset({"read_file", "edit_file", "shell_exec"})
        assert cfg.extra_tools == frozenset()

    def test_ptc_mode_is_gone(self):
        # PTC 已移除：旧客户端仍可能传 "ptc"，必须安静回退到 normal 而不是报错。
        cfg = get_mode_config("ptc")
        assert cfg.mode is AgentMode.NORMAL

    def test_creative_mode_is_gone(self):
        cfg = get_mode_config("creative")
        assert cfg.mode is AgentMode.NORMAL
        assert cfg.extra_tools == frozenset()

    def test_overrides_are_applied(self, tmp_path, monkeypatch):
        overrides = {
            "minimal": {
                "include_tools": ["read_file", "edit_file", "shell_exec", "grep_files"],
                "enable_compaction": True,
            }
        }
        p = tmp_path / "mode_overrides.json"
        p.write_text(json.dumps(overrides), encoding="utf-8")
        monkeypatch.setattr("app.agent.modes._overrides_path", lambda: p)

        cfg = get_mode_config("minimal")
        assert "grep_files" in cfg.include_tools
        assert cfg.enable_compaction is True
        # 未覆盖字段保持基值
        assert cfg.persona == "You are a helpful software engineer assistant."

    def test_broken_overrides_ignore(self, tmp_path, monkeypatch):
        p = tmp_path / "mode_overrides.json"
        p.write_text("{invalid json", encoding="utf-8")
        monkeypatch.setattr("app.agent.modes._overrides_path", lambda: p)

        cfg = get_mode_config("minimal")
        assert cfg.enable_compaction is False  # 回退基值

    def test_configs_are_frozen(self):
        cfg = get_mode_config("normal")
        assert isinstance(cfg, ModeConfig)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.include_tools = frozenset()  # frozen dataclass 禁止赋值


class TestCodeMode:
    """编码模式：改代码 → 跑验证的最小闭环。

    这组断言的目的是防止「切了没变化」：若 code 与 normal 的工具集/上下文/压缩
    完全相同，用户界面上就感知不到差异，模式选择器等于虚设。
    """

    def test_mode_is_resolved(self):
        cfg = get_mode_config("code")
        assert cfg.mode is AgentMode.CODE

    def test_has_coding_loop_tools(self):
        cfg = get_mode_config("code")
        # 定位 → 读 → 改 → 跑 → 看差异，缺任何一环都退化成"盲改"
        for name in (
            "grep_files",  # 按内容定位（minimal 缺这个，改多文件只能全量重读）
            "glob_files",  # 按文件名定位
            "read_file",
            "edit_file",
            "write_file",
            "shell_exec",
            "persistent_shell",  # cwd/环境跨调用保持，跑连续命令不必重复 cd
            "run_code",  # 批量机械改动一次跑完，省往返
            "git_status",
            "git_diff",  # 改完要能自己看到改了什么
        ):
            assert name in cfg.include_tools, f"code 模式缺少 {name}"

    def test_excludes_delete_file(self):
        """删除不可逆，是编码模式里最容易被"清理"名义误用的操作。

        需要删除时应显式切到 normal 走人工确认，而不是让编码模式顺手删掉。
        """
        assert "delete_file" not in get_mode_config("code").include_tools

    def test_keeps_context_and_compaction(self):
        """编码任务必须知道项目约定与历史教训；长任务还必须有压缩，否则中途爆上下文。"""
        cfg = get_mode_config("code")
        assert cfg.include_context is True
        assert cfg.enable_compaction is True

    def test_has_persona(self):
        cfg = get_mode_config("code")
        assert cfg.persona
        # persona 要真的指导行为，不是占位符
        assert "edit_file" in cfg.persona or "sandbox" in cfg.persona.lower()

    def test_differs_from_other_modes(self):
        """三种模式两两不同 —— 否则"切了看不出区别"。"""
        normal = get_mode_config("normal")
        minimal = get_mode_config("minimal")
        code = get_mode_config("code")
        assert code.include_tools != normal.include_tools
        assert code.include_tools != minimal.include_tools
        assert code.persona != minimal.persona
