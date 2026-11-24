#!/usr/bin/env python
"""团队主机验收统一入口：环境 → R2 基准 → Web E2E/SSE → R6 说明/结果摄入。

一次跑完 PRD §8 的可自动部分，并输出**机器可读 JSON + 人类摘要**。没有真实主机
或缺少挂载点时，相关阶段明确记 SKIP（附原因），绝不拿 SKIP 冒充 PASS。

    # 只做预检（没有主机也能跑，结论是 SKIP + 退出码 77）
    python scripts/team_acceptance.py

    # 团队主机全量：域名 + 挂载点 + 模型可用（SSE）
    set CHIRON_OWNER_PASSWORD=owner 密码
    python scripts/team_acceptance.py ^
        --base-url https://chiron.example.com --email owner@company.com ^
        --r2-root R:\\project --sse --strict-web --json-out .artifacts/team-acceptance.json

    # 已有 R6 压测结果时摄入并过门禁
    python scripts/team_acceptance.py --base-url ... --email ... --r6-report r6.json

退出码：0 = 无 FAIL 且至少一项 PASS；1 = 有 FAIL；77 = 全部 SKIP（缺主机/缺挂载点/
缺凭据，属于“环境不具备”而不是通过；与 scripts/integration/run.sh 同口径）。

门禁（gates）是稳定 id，可直接贴进验收记录：
* env.tls / env.health / env.ready / env.owner_credentials / env.owner_identity
* r2.mount / r2.read_p95 / r2.write_p95 / r2.git / r2.io_errors / r2.rename_delete
* web.login / web.must_change_password / web.first_login_password_change /
  web.invite / web.force_logout / web.share_create / web.share_anonymous /
  web.share_page_html / web.share_revoked / web.sse_first_token / web.sse_turn_done
* r6.report_schema / r6.pool_coverage / r6.metrics_present / r6.idle_reclaimed /
  r6.metrics_endpoint / r6.first_token_p95

仍只能人工/联调确认的项（脚本会列在 pending_real_host）：
R2 的 fs/git/run_code 全链路、rename/delete 业务侧生效、10 分钟稳定性；R6 的
P=2/4 压测与 /metrics 引擎池核对；真实域名/TLS 与反代不缓冲 SSE。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import r2_mount_bench as r2  # noqa: E402  （导入路径要在 sys.path 调整之后）
import team_smoke  # noqa: E402

STAGE_ORDER = ("env", "r2", "web", "r6")
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV_MISSING = 77

# 步骤名 → 稳定门禁 id。多个步骤可映射到同一个门禁（任一 FAIL 即 FAIL）。
WEB_GATE_MAP = {
    "owner 登录": "web.login",
    "读取用户列表": "web.admin_read",
    "创建用户": "web.create_user",
    "返回一次性初始密码": "web.initial_password",
    "首登标记 must_change_password": "web.must_change_password",
    "未改密访问业务接口被拦": "web.must_change_gate",
    "首次改密": "web.first_login_password_change",
    "改密后业务接口可用": "web.after_change_usable",
    "SSE 首 token 到达": "web.sse_first_token",
    "SSE 回合完成": "web.sse_turn_done",
    "创建分享链接": "web.share_create",
    "匿名分享页": "web.share_anonymous",
    "匿名读取分享内容": "web.share_anonymous",
    "匿名分享页 HTML 可访问": "web.share_page_html",
    "撤销分享": "web.share_revoke",
    "撤销后匿名访问返回 410": "web.share_revoked",
    "创建受邀用户": "web.invite",
    "生成一次性邀请": "web.invite",
    "受邀人设置密码": "web.invite",
    "邀请只能用一次": "web.invite",
    "受邀人登录": "web.invite",
    "受邀人无需再改密": "web.invite",
    "受邀人业务接口可用": "web.invite",
    "禁用后旧会话失效": "web.disable_revokes_session",
    "强制下线": "web.force_logout",
    "强制下线后旧会话失效": "web.force_logout",
    "清理测试数据": "web.cleanup",
    "运行异常": "web.request_error",
}

REQUIRED_WEB_GATES = (
    "web.login",
    "web.must_change_password",
    "web.first_login_password_change",
    "web.invite",
    "web.force_logout",
)


def _status_counts(gates: list[dict]) -> dict:
    return {key: sum(1 for g in gates if g["status"] == key) for key in ("PASS", "FAIL", "SKIP")}


def make_check(name: str, status: str, detail: str = "", *, value=None, threshold=None) -> dict:
    if status not in ("PASS", "FAIL", "SKIP"):
        raise ValueError(f"未知状态: {status}")
    return {"name": name, "status": status, "detail": detail, "value": value, "threshold": threshold}


def make_stage(name: str, status: str, detail: str, gates: list[dict], data: dict | None = None) -> dict:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "gates": gates,
        "data": data or {},
    }


def mask_email(email: str) -> str:
    """报告里不留完整身份：owner@company.com → o***@company.com。"""
    email = (email or "").strip()
    if not email:
        return ""
    name, sep, domain = email.partition("@")
    head = (name[:1] + "***") if name else "***"
    return head + sep + domain if sep else head


def _base_url(opts) -> str:
    return (opts.base_url or os.environ.get("CHIRON_TEAM_BASE_URL", "") or "").rstrip("/")


def _owner_email(opts) -> str:
    return (opts.email or os.environ.get("CHIRON_TEAM_EMAIL", "") or "").strip()


def stage_env(opts, log=print) -> dict:
    """环境检查：TLS、/health、/ready、凭据是否齐备（缺凭据只 SKIP 不 FAIL）。"""
    gates: list[dict] = []
    base = _base_url(opts)
    if not base:
        gates.append(make_check("env.base_url", "SKIP", "未提供 --base-url，主机相关阶段全部跳过"))
        return make_stage("env", "SKIP", "未提供 --base-url", gates)

    if base.startswith("https://"):
        gates.append(make_check("env.tls", "PASS", "HTTPS"))
    elif base.startswith("http://"):
        if opts.allow_http:
            gates.append(make_check("env.tls", "PASS", "已显式 --allow-http（内网联调）"))
        else:
            gates.append(
                make_check(
                    "env.tls",
                    "FAIL",
                    "团队模式要求 HTTPS；确为内网联调请显式加 --allow-http",
                )
            )
    else:
        gates.append(make_check("env.tls", "FAIL", f"base-url 必须是 http(s):// 开头：{base}"))
        return make_stage("env", "FAIL", "base-url 形态非法", gates)

    client = team_smoke.Client(base)
    for gate_id, path in (("env.health", "/health"), ("env.ready", "/ready")):
        try:
            code, _ = client.call("GET", path)
        except Exception as exc:  # noqa: BLE001 - 连不上就是主机不可达
            gates.append(make_check(gate_id, "FAIL", f"{type(exc).__name__}: {exc}"))
            continue
        if code == 200:
            gates.append(make_check(gate_id, "PASS", f"HTTP {code}"))
        elif code == 503:
            gates.append(make_check(gate_id, "FAIL", "HTTP 503（依赖未就绪）"))
        else:
            gates.append(make_check(gate_id, "FAIL", f"HTTP {code}"))

    if os.environ.get("CHIRON_OWNER_PASSWORD"):
        gates.append(make_check("env.owner_credentials", "PASS", "CHIRON_OWNER_PASSWORD 已提供"))
    else:
        gates.append(make_check("env.owner_credentials", "SKIP", "缺少 CHIRON_OWNER_PASSWORD，Web E2E 跳过"))
    if _owner_email(opts):
        gates.append(make_check("env.owner_identity", "PASS", mask_email(_owner_email(opts))))
    else:
        gates.append(make_check("env.owner_identity", "SKIP", "缺少 --email，Web E2E 跳过"))

    status = "FAIL" if any(g["status"] == "FAIL" for g in gates) else "PASS"
    detail = f"{base}（{_status_counts(gates)['PASS']}/{len(gates)} 项通过）"
    return make_stage("env", status, detail, gates)


def stage_r2(opts, log=print) -> dict:
    """R2 基准：读/写 P95 门限 + git 可用性 + rename/delete 实际生效。"""
    gates: list[dict] = []
    root = (opts.r2_root or os.environ.get("CHIRON_R2_ROOT", "") or "").strip()
    if not root:
        gates.append(make_check("r2.mount", "SKIP", "未提供 --r2-root（团队主机 rclone/WinFsp 挂载点）"))
        return make_stage("r2", "SKIP", "未提供挂载点", gates)
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        gates.append(make_check("r2.mount", "FAIL", f"挂载点不存在或不是目录：{root}"))
        return make_stage("r2", "FAIL", "挂载点不可用", gates)
    gates.append(make_check("r2.mount", "PASS", root))

    report = r2.run_bench(
        root,
        files=opts.r2_files,
        size_kb=opts.r2_size_kb,
        rounds=opts.r2_rounds,
        skip_git=opts.r2_skip_git,
    )
    verdict = r2.evaluate(report, read_p95_ms=opts.r2_read_p95_ms, write_p95_ms=opts.r2_write_p95_ms)
    log("  info " + r2.format_summary(report, verdict).replace("\n", "\n  info "))

    read, write, git = report["read"], report["write"], report["git"]
    read_bad = not read["samples"] or read["p95_ms"] > opts.r2_read_p95_ms
    write_bad = not write["samples"] or write["p95_ms"] > opts.r2_write_p95_ms
    gates.append(
        make_check(
            "r2.read_p95",
            "FAIL" if read_bad else "PASS",
            f"P50 {read['p50_ms']}ms / P95 {read['p95_ms']}ms（{read['samples']} 样本）",
            value=read["p95_ms"],
            threshold=opts.r2_read_p95_ms,
        )
    )
    gates.append(
        make_check(
            "r2.write_p95",
            "FAIL" if write_bad else "PASS",
            f"P50 {write['p50_ms']}ms / P95 {write['p95_ms']}ms（{write['samples']} 样本）",
            value=write["p95_ms"],
            threshold=opts.r2_write_p95_ms,
        )
    )
    if git.get("skipped"):
        gates.append(make_check("r2.git", "SKIP", "--skip-git：git 可用性未验证，不能算通过"))
    else:
        gates.append(
            make_check(
                "r2.git",
                "PASS" if git.get("available") else "FAIL",
                f"可用，{git.get('ms')}ms" if git.get("available") else (git.get("error") or "不可用"),
            )
        )
    gates.append(
        make_check(
            "r2.io_errors",
            "PASS" if not report.get("errors") else "FAIL",
            "无" if not report.get("errors") else "; ".join(report["errors"]),
        )
    )
    rename_ok = report["rename"]["samples"] > 0 and report["delete"]["samples"] > 0
    gates.append(
        make_check(
            "r2.rename_delete",
            "PASS" if rename_ok and not report.get("errors") else "FAIL",
            f"rename {report['rename']['p95_ms']}ms / delete {report['delete']['p95_ms']}ms",
        )
    )

    status = "FAIL" if any(g["status"] == "FAIL" for g in gates) else "PASS"
    detail = f"{root}（{'通过' if verdict['passed'] else '未通过'}）"
    return make_stage(
        "r2",
        status,
        detail,
        gates,
        data={
            "root": root,
            "read": read,
            "write": write,
            "git": git,
            "errors": report.get("errors") or [],
            "verdict": verdict,
        },
    )


def _merge_web_gates(steps: list[dict]) -> list[dict]:
    """把 suite 步骤折成稳定门禁；同 id 合并，FAIL 优先于 PASS，SKIP 最低。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    rank = {"FAIL": 2, "PASS": 1, "SKIP": 0}
    for step in steps:
        gate_id = WEB_GATE_MAP.get(step["name"])
        if not gate_id:
            continue
        if gate_id not in merged:
            merged[gate_id] = make_check(gate_id, step["status"], step["detail"])
            order.append(gate_id)
            continue
        current = merged[gate_id]
        if rank[step["status"]] > rank[current["status"]]:
            current["status"] = step["status"]
            current["detail"] = step["detail"]
    return [merged[gid] for gid in order]


def stage_web(opts, log=print) -> dict:
    """Web E2E：登录、首登改密、邀请、强制下线、匿名分享页（+ 可选 SSE）。"""
    base = _base_url(opts)
    if not base:
        return make_stage("web", "SKIP", "未提供 --base-url", [make_check("web.e2e", "SKIP", "缺少目标主机")])
    email = _owner_email(opts)
    if not email:
        return make_stage("web", "SKIP", "缺少 --email", [make_check("web.e2e", "SKIP", "缺少 owner 身份")])
    password = os.environ.get("CHIRON_OWNER_PASSWORD", "")
    if not password:
        return make_stage(
            "web",
            "SKIP",
            "缺少 CHIRON_OWNER_PASSWORD",
            [make_check("web.e2e", "SKIP", "缺少 owner 密码环境变量")],
        )

    report = team_smoke.run_suite(
        base,
        email,
        password,
        sse=opts.sse,
        sse_timeout=opts.sse_timeout,
        share=not opts.no_share,
        strict_web=opts.strict_web,
        log=lambda _msg: None,  # 摘要统一由本脚本输出，避免两套日志交错
    )
    gates = _merge_web_gates(report["steps"])
    seen = {g["name"] for g in gates}
    required = list(REQUIRED_WEB_GATES)
    if not opts.no_share:
        required.append("web.share_anonymous")
    if opts.sse:
        required.extend(["web.sse_first_token", "web.sse_turn_done"])
    for gate_id in required:
        if gate_id not in seen:
            gates.append(make_check(gate_id, "FAIL", "未执行到该步骤（前置步骤失败）"))

    status = "FAIL" if any(g["status"] == "FAIL" for g in gates) else "PASS"
    detail = "通过" if report["ok"] else f"失败于：{report.get('failed_step') or '未知步骤'}"
    return make_stage(
        "web",
        status,
        detail,
        gates,
        data={
            "steps": report["steps"],
            "metrics": report.get("metrics") or {},
            "cleanup": report.get("cleanup") or {},
            "failed_step": report.get("failed_step"),
        },
    )


def _load_r6_report(path: str) -> tuple[dict | None, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - 读不到就是证据缺失
        return None, f"读取失败：{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "顶层必须是 JSON 对象"
    return payload, ""


def stage_r6(opts, log=print) -> dict:
    """R6 压测：没有结果就 SKIP 并给清单；有结果则按 P=2/4、指标与空闲回收过门禁。"""
    checklist = [
        "P=2 与 P=4 各跑一轮真实回合，记录 P95 首 token",
        "记录逐引擎 RSS（/metrics 的引擎池指标）",
        "确认空闲回收（退出码 75 / 副本自停）",
        "确认 /metrics 暴露引擎池 active_tasks/cpu/memory",
    ]
    if not opts.r6_report:
        gates = [
            make_check(
                "r6.load_test",
                "SKIP",
                "未提供 --r6-report：R6 需在团队主机上以 P=2/4 跑真实回合（见 docs/prd-remote-team.md §8）",
            )
        ]
        return make_stage("r6", "SKIP", "未提供压测结果", gates, data={"checklist": checklist})

    payload, error = _load_r6_report(opts.r6_report)
    if payload is None:
        gates = [make_check("r6.report_schema", "FAIL", error)]
        return make_stage("r6", "FAIL", "R6 报告不可读", gates, data={"checklist": checklist})

    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        gates = [make_check("r6.report_schema", "FAIL", "缺少非空 runs 数组")]
        return make_stage("r6", "FAIL", "R6 报告结构不完整", gates, data={"checklist": checklist})

    gates = [make_check("r6.report_schema", "PASS", f"{len(runs)} 轮")]
    pools = sorted(
        {int(run["pool"]) for run in runs if isinstance(run, dict) and str(run.get("pool", "")).isdigit()}
    )
    missing_pools = [p for p in (2, 4) if p not in pools]
    gates.append(
        make_check(
            "r6.pool_coverage",
            "FAIL" if missing_pools else "PASS",
            f"实测 P={pools}；缺 {missing_pools}" if missing_pools else f"实测 P={pools}",
        )
    )

    missing_metrics = [
        run.get("pool") if isinstance(run, dict) else "?"
        for run in runs
        if not isinstance(run, dict)
        or run.get("first_token_p95_ms") is None
        or not run.get("engine_rss_mb")
    ]
    gates.append(
        make_check(
            "r6.metrics_present",
            "FAIL" if missing_metrics else "PASS",
            f"缺 first_token_p95_ms/engine_rss_mb 的轮次：{missing_metrics}" if missing_metrics else "齐全",
        )
    )
    idle_ok = all(isinstance(run, dict) and run.get("idle_reclaimed") is True for run in runs)
    gates.append(
        make_check(
            "r6.idle_reclaimed",
            "PASS" if idle_ok else "FAIL",
            "全部轮次确认空闲回收" if idle_ok else "存在未确认空闲回收的轮次",
        )
    )
    metrics_checked = payload.get("metrics_checked") is True
    gates.append(
        make_check(
            "r6.metrics_endpoint",
            "PASS" if metrics_checked else "FAIL",
            "已核对 /metrics 引擎池指标" if metrics_checked else "报告未声明 metrics_checked=true",
        )
    )

    threshold = float(opts.r6_first_token_p95_ms or 0)
    if threshold > 0:
        values = [float(run["first_token_p95_ms"]) for run in runs if run.get("first_token_p95_ms") is not None]
        if not values:
            gates.append(
                make_check("r6.first_token_p95", "FAIL", "报告里没有可用的 first_token_p95_ms")
            )
        else:
            worst = max(values)
            gates.append(
                make_check(
                    "r6.first_token_p95",
                    "PASS" if worst <= threshold else "FAIL",
                    f"最差 P95 {worst}ms",
                    value=worst,
                    threshold=threshold,
                )
            )
    else:
        gates.append(
            make_check(
                "r6.first_token_p95",
                "SKIP",
                "未设 --r6-first-token-p95-ms：仅记录数值，不判定（PRD 未给硬阈值）",
            )
        )

    status = "FAIL" if any(g["status"] == "FAIL" for g in gates) else "PASS"
    return make_stage(
        "r6",
        status,
        f"摄入 {opts.r6_report}",
        gates,
        data={"runs": runs, "metrics_checked": metrics_checked, "checklist": checklist},
    )


DEFAULT_STAGES = {"env": stage_env, "r2": stage_r2, "web": stage_web, "r6": stage_r6}


def run_acceptance(opts, stage_runners: dict | None = None, log=print) -> dict:
    """执行各阶段并汇总。stage_runners 可注入，便于离线测试编排语义。"""
    runners = dict(DEFAULT_STAGES)
    if stage_runners:
        runners.update(stage_runners)
    stages: list[dict] = []
    for name in STAGE_ORDER:
        try:
            stages.append(runners[name](opts, log))
        except Exception as exc:  # noqa: BLE001 - 单个阶段崩溃不能吞掉整轮结论
            stages.append(
                make_stage(
                    name,
                    "FAIL",
                    f"阶段内部异常：{type(exc).__name__}: {exc}",
                    [make_check(f"{name}.stage", "FAIL", f"{type(exc).__name__}: {exc}")],
                )
            )

    gates = [g for stage in stages for g in stage["gates"]]
    counts = _status_counts(gates)
    any_stage_fail = any(stage["status"] == "FAIL" for stage in stages)
    if counts["FAIL"] or any_stage_fail:
        exit_code = EXIT_FAIL
    elif counts["PASS"]:
        exit_code = EXIT_OK
    else:
        exit_code = EXIT_ENV_MISSING

    pending: list[str] = []
    for stage in stages:
        for g in stage["gates"]:
            if g["status"] == "SKIP":
                pending.append(f"{g['name']}：{g['detail']}")
    pending.extend(
        [
            "R2：fs/git/run_code 全链路联调、rename/delete 业务侧生效、10 分钟稳定性",
            "R6：P=2/4 真实回合压测与 /metrics 引擎池指标核对",
            "Web：真实域名/TLS、反代不缓冲 SSE（首 token 时延）",
        ]
    )

    return {
        "schema": 1,
        "kind": "team-acceptance",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": {
            "base_url": _base_url(opts),
            "email": mask_email(_owner_email(opts)),
            "r2_root": opts.r2_root or "",
            "r6_report": opts.r6_report or "",
        },
        "stages": stages,
        "gates": gates,
        "summary": {
            "pass": counts["PASS"],
            "fail": counts["FAIL"],
            "skip": counts["SKIP"],
            "exit_code": exit_code,
            "failed_gates": [g["name"] for g in gates if g["status"] == "FAIL"],
        },
        "pending_real_host": pending,
    }


def format_summary(report: dict) -> str:
    target = report["target"]
    lines = [
        f"团队主机验收预检  {target['base_url'] or '(未指定 --base-url)'}",
        "─" * 56,
    ]
    for stage in report["stages"]:
        lines.append(f"{stage['status']:<4} {stage['name']:<7} {stage['detail']}")
        for g in stage["gates"]:
            extra = ""
            if g.get("value") is not None and g.get("threshold") is not None:
                extra = f"（实测 {g['value']} / 门限 {g['threshold']}）"
            elif g.get("value") is not None:
                extra = f"（{g['value']}）"
            lines.append(f"       {g['status']:<4} {g['name']}{extra}  {g['detail']}".rstrip())

    metrics = {}
    for stage in report["stages"]:
        if stage["name"] == "web":
            metrics = (stage.get("data") or {}).get("metrics") or {}
            break
    if metrics.get("sse_first_token_ms") is not None:
        lines.append(f"\nSSE 首 token ≈ {int(metrics['sse_first_token_ms'])} ms")
    if metrics.get("share_messages") is not None:
        lines.append(f"匿名分享消息数 = {metrics['share_messages']}")

    summary = report["summary"]
    lines.extend(
        [
            "─" * 56,
            f"门禁：PASS {summary['pass']} / FAIL {summary['fail']} / SKIP {summary['skip']}"
            f"    退出码 {summary['exit_code']}",
        ]
    )
    if summary["exit_code"] == EXIT_OK:
        lines.append("结论：已执行的门禁全部通过；SKIP 项仍需真实主机补齐（见下）。")
    elif summary["exit_code"] == EXIT_FAIL:
        lines.append("结论：未通过 —— " + "、".join(summary["failed_gates"]))
    else:
        lines.append("结论：未验证（缺少真实主机/挂载点/凭据；这是 SKIP，不是 PASS）。")
    if report["pending_real_host"]:
        lines.append("待执行：")
        lines.extend(f"  - {item}" for item in report["pending_real_host"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="团队主机验收统一入口（环境/R2/Web E2E/R6）")
    parser.add_argument("--base-url", default=os.environ.get("CHIRON_TEAM_BASE_URL", ""))
    parser.add_argument("--email", default=os.environ.get("CHIRON_TEAM_EMAIL", ""), help="owner 邮箱或用户名")
    parser.add_argument("--allow-http", action="store_true", help="内网联调：允许 base-url 为 http")
    parser.add_argument("--r2-root", default=os.environ.get("CHIRON_R2_ROOT", ""), help="rclone/WinFsp 挂载点")
    parser.add_argument("--r2-files", type=int, default=50)
    parser.add_argument("--r2-size-kb", type=int, default=64)
    parser.add_argument("--r2-rounds", type=int, default=10)
    parser.add_argument("--r2-read-p95-ms", type=float, default=r2.DEFAULT_READ_P95_MS)
    parser.add_argument("--r2-write-p95-ms", type=float, default=r2.DEFAULT_WRITE_P95_MS)
    parser.add_argument("--r2-skip-git", action="store_true", help="跳过 git 门禁（结果会是 SKIP）")
    parser.add_argument("--sse", action="store_true", help="Web E2E 追加 SSE 首 token/turn_done")
    parser.add_argument("--sse-timeout", type=int, default=120)
    parser.add_argument("--no-share", action="store_true", help="跳过匿名分享页闭环")
    parser.add_argument("--strict-web", action="store_true", help="要求 /s/<token> 返回 HTML（团队 Web 托管 SPA）")
    parser.add_argument("--r6-report", default="", help="R6 压测结果 JSON（runs/metrics_checked）")
    parser.add_argument("--r6-first-token-p95-ms", type=float, default=0, help=">0 时对 R6 首 token P95 判定")
    parser.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    parser.add_argument("--json-out", default="", help="把 JSON 报告写到文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    opts = build_parser().parse_args(argv)
    log = (lambda _msg: None) if opts.json else print
    report = run_acceptance(opts, log=log)
    if opts.json_out:
        out_dir = os.path.dirname(os.path.abspath(opts.json_out))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(opts.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    if opts.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_summary(report))
    return int(report["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
