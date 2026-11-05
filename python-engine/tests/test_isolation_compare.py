"""`restricted` vs `low_il` 的写入边界对比测试。

为什么需要这组对比
------------------
`low_il`（Low 完整性级别）与 `restricted`（受限令牌 + restricting SID）都能拒绝
**写工作区之外**，但它们拒绝的**机制不同**，且失败模式也不同：

* `low_il` 靠完整性级别：低 IL 进程写中 IL 对象被强制策略拦下（`ERROR_ACCESS_DENIED`）。
  代价是它要动用户资产 —— 工作区目录必须打 Low 标签，否则连自己的文件都写不了。
* `restricted` 靠 restricting SID：**只有** restricting SID 之一满足的写访问才放行。
  它不改文件标签，只需给工作区加一条 ACE。

只测"拒绝"是不够的：一个根本写不了任何地方（包括工作区）的档位也会"拒绝写主目录"，
那种测试是假绿。所以每组都必须配上"工作区内可写"的正向对照。

运行条件
--------
`low_il` 侧**任何** Windows 账户都能跑（降级发生在子进程内部）。
`restricted` 侧必须提权：`CreateProcessAsUserW` 需要 `SeIncreaseQuotaPrivilege`，
普通账户实测稳定 `err=5`。没有提权时用 `pytest.skip` 明确跳过，而不是让断言
以"进程起不来"的形式误判成"写被拒绝"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.isolation as iso
from app.isolation import Level, appcontainer_workspace, win_launch, win_token
from app.isolation import win as win_mod
from app.tools import sandbox

pytestmark = pytest.mark.skipif(not win_mod.IS_WINDOWS, reason="Windows 专有隔离机制")


# ── 辅助 ────────────────────────────────────────────────────────


def _probe_name() -> str:
    return f"chiron_iso_probe_{os.getpid()}_{os.urandom(3).hex()}.txt"


def _profile_root() -> Path:
    """用户主目录：`low_il` 与 `restricted` 都必须拒写的位置。

    刻意用主目录**根**而不是某个子目录：子目录可能被单独授予了写权限
    （`%USERPROFILE%\\AppData\\Local` 一类），落在那里测试会假绿。
    """
    return Path(os.environ.get("USERPROFILE") or str(Path.home()))


def _worker_source(target: Path, level: str) -> str:
    """构造在**子进程内**先降级、再试着写 `target` 的探针源码。

    降级必须发生在子进程里：`set_integrity_level` 改的是当前进程令牌，且没有撤销
    API（设计如此，保证降级不可逆）。父进程调用会把 pytest 自己降级。
    """
    return (
        "import pathlib\n"
        "from app.isolation import Level, apply_in_child\n"
        f"apply_in_child(Level.{level})\n"
        f"t = pathlib.Path(r'{target}')\n"
        "try:\n"
        "    t.write_text('probe', encoding='utf-8')\n"
        "    print('WROTE')\n"
        "except OSError as exc:\n"
        "    print('DENIED', type(exc).__name__)\n"
    )


def _run_low_il_worker(target: Path, level: str = "LOW_IL") -> subprocess.CompletedProcess[str]:
    """在 `low_il` 子进程里写 `target`，返回子进程结果。

    `cwd` 设为 python-engine 根，使子进程能 `import app`（`-c` 模式下
    `sys.path[0]` 就是当前目录）。
    """
    engine_root = Path(__file__).resolve().parent.parent
    return subprocess.run(  # noqa: S603 — 固定 argv，无 shell，目标路径由本测试生成
        [sys.executable, "-c", _worker_source(target, level)],
        cwd=str(engine_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture
def workspace_probe() -> Path:
    """工作区内的探针文件路径，测试结束清理。"""
    target = sandbox.workspace_dir() / _probe_name()
    yield target
    _cleanup(target)


# ── low_il 组：无需提权，当前环境即可验证 ────────────────────────


class TestLowIlWriteBoundary:
    """`low_il` 应当：工作区内可写、主目录根不可写。"""

    def test_workspace_write_allowed(self, workspace_probe: Path):
        """正向对照：打完 Low 标签的工作区必须可写。

        这条是下面"主目录被拒"断言的对照组 —— 没有它，一个彻底瘫痪的档位
        也能让"被拒"测试通过。
        """
        labeled, err = iso._ensure_workspace_low_label(str(sandbox.workspace_dir()))  # noqa: SLF001
        if not labeled:
            pytest.skip(f"无法给工作区打 Low 标签（{err}）；本条依赖该前置")

        result = _run_low_il_worker(workspace_probe)

        assert "WROTE" in result.stdout, (
            f"low_il 子进程无法写工作区 —— 降级把档位降到了自己都写不了的地步。\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr[-800:]!r}"
        )

    def test_profile_root_write_denied(self):
        """核心断言：`low_il` 写主目录根必须被拒。"""
        target = _profile_root() / _probe_name()
        if target.exists():
            pytest.skip("主目录根不可写（环境已受限），无法构成对比基线")

        result = _run_low_il_worker(target)
        try:
            assert "DENIED" in result.stdout, (
                f"low_il 子进程竟然写进了用户主目录 —— 隔离没有生效。\n"
                f"stdout={result.stdout!r}\nstderr={result.stderr[-800:]!r}"
            )
            assert not target.exists(), "子进程报 DENIED 却留下了文件，判定逻辑有误"
        finally:
            _cleanup(target)


# ── restricted 组：需要提权 ────────────────────────────────────


def _require_restricted() -> None:
    """`restricted` 档不可用时跳过，并说明**为什么**不可用。"""
    if not win_launch.is_admin():
        pytest.skip(
            "需要以管理员身份运行 pytest：CreateProcessAsUserW 依赖 "
            "SeIncreaseQuotaPrivilege，普通账户实测 err=5"
        )
    if not win_launch.exec_ready():
        pytest.skip(f"受限令牌执行层不可用：{win_launch.exec_error() or '原因未知'}")


class TestRestrictedWriteBoundary:
    """`restricted` 应当：授权后的工作区可写、主目录根不可写。"""

    def test_profile_root_write_denied(self):
        """核心断言：`restricted` 写主目录根必须被拒。

        断言的是 `WRITE_RESTRICTED` 的语义 —— 写访问必须由 restricting SID
        之一满足，而主目录的 ACE 属于用户 SID，不在 restricting 列表里。
        """
        _require_restricted()
        target = _profile_root() / _probe_name()
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")

        result = win_launch.launch_restricted(
            [comspec, "/d", "/c", f'echo probe>"%USERPROFILE%\\{target.name}"'],
            cwd=r"C:\Windows",
            env={"PATH": r"C:\Windows\System32", "SystemRoot": r"C:\Windows"},
            timeout=30,
        )
        try:
            assert result.started, f"受限进程未能启动：{result.spawn_error}"
            assert not target.exists(), (
                f"restricted 进程写进了用户主目录 —— restricting SID 未生效。\n"
                f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        finally:
            _cleanup(target)

    def test_workspace_write_allowed_after_grant(self, workspace_probe: Path):
        """正向对照：`grant_path` 授权后，工作区内必须可写。

        不授权就测"工作区也不可写"，等于证明 `restricted` 是个废档 —— 引擎连
        读不到自己的输入。所以这条先授权再断言，结束后撤销那条 ACE。
        """
        _require_restricted()
        ws = appcontainer_workspace()
        granted, gerr = win_token.grant_path(ws)
        if not granted:
            pytest.skip(f"无法给工作区授权合成 SID：{gerr}")

        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        try:
            result = win_launch.launch_restricted(
                [
                    comspec, "/d", "/c",
                    f'echo probe>"{workspace_probe}"&&type "{workspace_probe}"',
                ],
                cwd=ws,
                env={
                    "PATH": r"C:\Windows\System32",
                    "SystemRoot": r"C:\Windows",
                    "USERPROFILE": os.environ.get("USERPROFILE", r"C:\Windows"),
                },
                timeout=30,
            )
            assert result.started, f"受限进程未能启动：{result.spawn_error}"
            assert workspace_probe.exists(), (
                f"restricted 进程无法写已授权的工作区 —— 降级到这一步等于不可用。\n"
                f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        finally:
            win_token.revoke_path(ws)
            _cleanup(workspace_probe)


# ── 两档的强度差异（机制层面，不需要真起进程） ────────────────


class TestMechanismDifference:
    """两档为什么不能互相替代 —— 用机制事实固定住，避免日后被"简化"掉。"""

    def test_restricted_does_not_require_disk_relabeling(self):
        """`restricted` 只加一条 ACE，`low_il` 要改整个工作区标签。

        `_ensure_workspace_low_label` 的 docstring 已写明 `restricted` **不是**
        `low_il` 的前置。这条断言把该结论钉在测试里：一旦有人把判断写回
        `>= LOW_IL`，`restricted` 就会被迫走代价更大的那条路。
        """
        assert Level.RESTRICTED.rank > Level.LOW_IL.rank

    def test_synthetic_sid_is_well_known_null(self):
        """restricting SID 用的是 `S-1-0-0`（Nobody）。

        它不匹配任何真实用户/组的 ACE —— 这正是"写主目录被拒"的根因；
        工作区能写**只因为**我们显式给它加了这条 ACE。
        """
        assert win_token.SYNTHETIC_SID == "S-1-0-0"


# ── 档位切换的残留物清理（回归） ──────────────────────────────


class TestStaleAppContainerAceCleanup:
    """`appcontainer → low_il` 必须清掉上一档留下的包 SID ACE。

    这是本机实测出来的真实缺陷，不是假想：

    * 工作区带着 `*(S-1-15-2-…):(OI)(CI)(M)` 时，Low IL 子进程创建与修改文件
      **一律** `PermissionError`；
    * `icacls <dir> /remove:g *<该SID>` 之后立即恢复可写；
    * 把原 SDDL 写回去，又变回 `PermissionError`（可复现，非偶发）。

    危害在于**静默**：配置写着 `low_il`、`/v1/system/isolation` 也报 `low_il`，
    但实际连自己的工作区都写不了。旧实现里 `_labeled_workspaces` 还会缓存
    "已验证"，让切换后的清理被整个跳过。
    """

    #: 合成一个格式合法、不对应任何真实 profile 的 AppContainer SID。
    #: `icacls /grant` 接受任意合法 SID 字符串，不需要包真的存在。
    _FAKE_SID = "S-1-15-2-1111111111-2222222222-3333333333"

    def test_removes_only_appcontainer_aces(self, tmp_path: Path):
        """只删 `S-1-15-2-*`，用户/系统 ACE 必须原样保留。"""
        target = tmp_path / "ws"
        target.mkdir()

        r = subprocess.run(
            ["icacls", str(target), "/grant", f"*{self._FAKE_SID}:(OI)(CI)(M)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0, f"无法预置测试 ACE: {r.stdout or r.stderr}"

        before = subprocess.run(
            ["icacls", str(target)], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
        assert self._FAKE_SID in before, "预置的 ACE 未生效，测试前提不成立"

        ok, sids, err = win_mod.remove_appcontainer_aces(str(target))
        assert ok, f"清理失败: {err}"
        assert self._FAKE_SID in sids, f"未报告移除的 SID: {sids}"

        after = subprocess.run(
            ["icacls", str(target)], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
        assert self._FAKE_SID not in after, "包 SID ACE 仍在"
        # 反向断言：清理不能把当前用户的 ACE 一起搬走，否则工作区会失去所有者权限
        assert os.environ.get("USERNAME", "") in after or "Administrators" in after, (
            f"清理过度，权限被抹掉了:\n{after}"
        )

    def test_noop_when_clean(self, tmp_path: Path):
        """干净目录上不应改动任何东西（幂等，可在每次 resolve 时安全调用）。"""
        target = tmp_path / "clean"
        target.mkdir()
        ok, sids, err = win_mod.remove_appcontainer_aces(str(target))
        assert ok and sids == [] and not err

    def test_low_label_cleans_before_labeling(self, tmp_path: Path):
        """`_ensure_workspace_low_label()` 必须顺带清掉残留 ACE。

        直接测集成点而不是只测工具函数：真正的修复在调用链里，只测
        `remove_appcontainer_aces()` 会让"忘了接线"这种回归溜过去。
        """
        target = tmp_path / "ws2"
        target.mkdir()
        r = subprocess.run(
            ["icacls", str(target), "/grant", f"*{self._FAKE_SID}:(OI)(CI)(M)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0

        ok, err = iso._ensure_workspace_low_label(str(target))  # noqa: SLF001
        assert ok, f"打标签失败: {err}"

        after = subprocess.run(
            ["icacls", str(target)], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
        assert self._FAKE_SID not in after, (
            "`_ensure_workspace_low_label` 没有清理残留的 AppContainer ACE —— "
            "档位从 appcontainer 切回 low_il 时工作区会静默变成不可写"
        )
        assert "Low Mandatory Level" in after, "Low 完整性标签未打上"
