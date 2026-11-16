"""项目指令文件加载（G-P2）的回归测试。

背景：chiron 此前只认 `CLAUDE.md`，
而 `AGENTS.md` 是跨工具的事实约定（Codex / Cursor / pi 都读它）。用户为别的工具写好的
项目约束，换到 chiron 上等于不存在。

这里守住四条契约：
1. **优先级**：`AGENTS.override.md` > `AGENTS.md` > `CLAUDE.md`
2. **覆盖是覆盖**：`AGENTS.override.md` 存在时同目录其余候选名都不读
3. **同目录只取一份**：`AGENTS.md` 与 `CLAUDE.md` 并存时不重复注入同一段约束
4. **按 realpath 去重**：软链 / worktree 让同一文件以不同路径被发现时只注入一次
"""


import pytest

import app.context.claude_md as mod
from app.context.claude_md import PROJECT_INSTRUCTION_NAMES, ClaudeMdLoader


@pytest.fixture(autouse=True)
def _no_global_instructions(monkeypatch, tmp_path_factory):
    """隔离全局 `~/.claude/`。

    真实环境里这个目录**确实存在**（开发者自己的全局指令），不隔离会让"我只放了一个
    文件"的断言被宿主机上的全局文件污染。把 `_HOME_DIR` 指向一个空的临时目录，
    `_global_instruction_file()` 从它的父目录找，自然找不到任何东西。
    """
    empty_home = tmp_path_factory.mktemp("fake-home")
    monkeypatch.setattr(mod, "_HOME_DIR", empty_home / "CLAUDE.md")


# ── 优先级 ───────────────────────────────────────────────────────────


def test_agents_md_wins_over_claude_md(tmp_path):
    """同目录并存时 `AGENTS.md` 优先 —— 它是跨工具约定，不该被旧名压住。"""
    (tmp_path / "AGENTS.md").write_text("AGENTS rules", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE rules", encoding="utf-8")

    result = ClaudeMdLoader().load(str(tmp_path))

    assert "AGENTS rules" in result
    assert "CLAUDE rules" not in result


def test_claude_md_still_works_alone(tmp_path):
    """保留 `CLAUDE.md` 是为了不打断既有用户 —— 只放它时行为与以前一致。"""
    (tmp_path / "CLAUDE.md").write_text("CLAUDE rules", encoding="utf-8")

    result = ClaudeMdLoader().load(str(tmp_path))

    assert "CLAUDE rules" in result


def test_lowercase_variant_is_recognised(tmp_path):
    """Linux 上文件名大小写敏感，用户写 `agents.md` 也该被认出来。"""
    (tmp_path / "agents.md").write_text("lowercase rules", encoding="utf-8")

    assert "lowercase rules" in ClaudeMdLoader().load(str(tmp_path))


def test_override_beats_agents_md(tmp_path):
    """`AGENTS.override.md` 是**覆盖**：它存在时 `AGENTS.md` 不再读。

    三个都拼进去会让"我只想临时盖掉一段"变成"又叠加了一段"，语义就不对了。
    """
    (tmp_path / "AGENTS.override.md").write_text("override rules", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("AGENTS rules", encoding="utf-8")

    result = ClaudeMdLoader().load(str(tmp_path))

    assert "override rules" in result
    assert "AGENTS rules" not in result


def test_override_also_suppresses_claude_md(tmp_path):
    (tmp_path / "AGENTS.override.md").write_text("override rules", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE rules", encoding="utf-8")

    result = ClaudeMdLoader().load(str(tmp_path))

    assert "override rules" in result
    assert "CLAUDE rules" not in result


def test_candidate_order_is_documented_priority():
    """优先级清单本身是契约：改顺序等于改行为。"""
    assert PROJECT_INSTRUCTION_NAMES[0] == "AGENTS.override.md"
    assert PROJECT_INSTRUCTION_NAMES.index("AGENTS.md") < PROJECT_INSTRUCTION_NAMES.index(
        "CLAUDE.md"
    )


# ── 层级加载 ─────────────────────────────────────────────────────────


def test_hierarchy_mixes_file_names(tmp_path):
    """不同层级可以用不同的候选名 —— 每层独立选一份。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "CLAUDE.md").write_text("Root rules", encoding="utf-8")
    (sub / "AGENTS.md").write_text("Sub rules", encoding="utf-8")

    result = ClaudeMdLoader().load(str(sub))

    assert "Root rules" in result
    assert "Sub rules" in result
    # 近的（sub）排在前面，优先级更高
    assert result.index("Sub rules") < result.index("Root rules")


# ── header 标签 ──────────────────────────────────────────────────────


def test_header_uses_the_real_file_name(tmp_path):
    """标签必须反映真实文件名。

    写死 `CLAUDE.md` 会让模型以为读的是另一个文件，排查"我改了 AGENTS.md 怎么没生效"
    时直接被误导。
    """
    (tmp_path / "AGENTS.md").write_text("AGENTS rules", encoding="utf-8")

    result = ClaudeMdLoader().load(str(tmp_path))

    assert "<!-- AGENTS.md:" in result
    assert "<!-- CLAUDE.md:" not in result


# ── realpath 去重 ────────────────────────────────────────────────────


def test_symlinked_file_is_not_injected_twice(tmp_path):
    """软链让同一份文件以两个路径被祖先链发现时，只该注入一次。

    重复注入的后果不是省 token，而是让模型以为约束被强调了两次。
    """
    real = tmp_path / "real"
    real.mkdir()
    target = real / "AGENTS.md"
    target.write_text("shared rules", encoding="utf-8")

    # 子目录里的 AGENTS.md 是指向父目录同一份文件的软链
    sub = tmp_path / "sub"
    sub.mkdir()
    link = sub / "AGENTS.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover — 无符号链接权限的平台
        pytest.skip("symlink not supported on this platform")

    result = ClaudeMdLoader().load(str(sub))

    assert result.count("shared rules") == 1


# ── 缓存 ─────────────────────────────────────────────────────────────


def test_empty_when_nothing_found(tmp_path):
    assert ClaudeMdLoader().load(str(tmp_path)) == ""


def test_cache_is_per_root(tmp_path):
    """缓存 key 是 root，不同 root 不能互相污染。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "AGENTS.md").write_text("A rules", encoding="utf-8")
    (b / "CLAUDE.md").write_text("B rules", encoding="utf-8")

    loader = ClaudeMdLoader()

    assert "A rules" in loader.load(str(a))
    assert "B rules" in loader.load(str(b))


# ── prompt_engine 复用同一份候选名 ──────────────────────────────────


def test_prompt_engine_reuses_the_same_candidates(tmp_path):
    """`PromptEngine._load_claude_md` 必须与层级加载认同一套名字。

    两处各写一份清单必然漂移，结果是"层级加载认 AGENTS.md，root 加载不认"。
    """
    from app.agent.prompt_engine import PromptEngine

    (tmp_path / "AGENTS.md").write_text("AGENTS rules", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE rules", encoding="utf-8")

    engine = PromptEngine.__new__(PromptEngine)  # 免去构造期的依赖注入
    assert engine._load_claude_md(str(tmp_path)) == "AGENTS rules"
