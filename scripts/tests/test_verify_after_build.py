#!/usr/bin/env python3
"""验收脚本的自测 —— 全程离线：HTTP 走注入的假 transport，不碰真机。

## 为什么用一个假 transport 而不是 mock HTTP 客户端

`verify_after_build_lib.run_checks` 的签名是 `(transport, ...)`，transport 就是
`(Request, timeout) -> HttpResult`。注入一个字典驱动的假网关，就等于让**生产代码
路径**（请求组装 → 判定 → 汇总 → 渲染）整条跑一遍，只是把网线拔了。这比逐函数
断言更接近"脚本真的能跑"，也比起一个本地 HTTP 服务更快、更可控。

## 覆盖的重点

* **判据的每一侧**：PASS / FAIL / SKIP 都必须有反例，否则"永远绿"的断言和
  "永远红"的断言一样没用。特别是 `total_input=0`、`tool=metadata`、
  `effective != requested` 这三个**真实踩过的**坑。
* **token 不出现在任何输出里**：文本视图与 `--json` 都扫一遍。
* **退出码语义**：0=无 FAIL，1=有 FAIL，2=环境不具备（状态文件缺失 / 栈未就绪 /
  换不到会话）。三者混淆会让 CI 把"没启动应用"当成"重建坏了"。

跑法：

    python -m unittest discover -s scripts/tests -p 'test_*.py' -v
    python scripts/tests/test_verify_after_build.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import verify_after_build_lib as vab  # noqa: E402  （导入路径要在 sys.path 调整之后）

GOOD_TOKEN = "header.payload.signature-with-enough-length-to-look-like-a-real-jwt"


def state_fixture(**over) -> dict:
    """一份最小可用状态（只带判定要读的字段）。"""
    base = {
        "schema": 1,
        "pid": 4321,
        "startedAt": "2026-11-20T08:20:23.434Z",
        "gatewayPort": 54834,
        "enginePort": 54848,
        "raw": {},
        "path": "C:/fake/runtime-state.json",
    }
    base.update(over)
    return base


def http(status=None, payload=None, text="", error="") -> vab.HttpResult:
    return vab.HttpResult(status=status, payload=payload, text=text, error=error)


def ok(data) -> vab.HttpResult:
    return http(200, {"success": True, "data": data}, json.dumps(data, ensure_ascii=False))


class FakeGateway:
    """按路径路由的假网关。未注册的路径回 404（与真网关一致）。"""

    def __init__(self, routes: dict, token: str = GOOD_TOKEN) -> None:
        self.routes = routes
        self.token = token
        self.calls: list[vab.Request] = []

    def __call__(self, request: vab.Request, timeout: float) -> vab.HttpResult:
        self.calls.append(request)
        path = request.url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        for prefix, handler in self.routes.items():
            if path == prefix or path.startswith(prefix + "?") or path.startswith(prefix + "/"):
                result = handler(request) if callable(handler) else handler
                return result
        return http(404, {"success": False, "error": "not found"}, '{"error":"not found"}')

    def find(self, path: str) -> list[vab.Request]:
        return [r for r in self.calls if r.url.endswith(path) or path in r.url]


def full_routes(**over) -> dict:
    """一份"全部 PASS"的路由表，逐项可覆盖。"""
    routes = {
        "/ready": ok({"postgres": "up", "redis": "up"}),
        "/v1/auth/session": ok({"token": GOOD_TOKEN, "user": {"email": "owner@chiron.local", "role": "owner"}}),
        "/v1/admin/deps/list": ok(
            {"tool": "uv", "packages": [{"name": "fastapi", "version": "0.115.0"}], "python": "3.12.7", "degraded": False}
        ),
        "/v1/system/isolation": ok(
            {
                "requested": "appcontainer",
                "effective": "appcontainer",
                "floor": "low_il",
                "degraded": False,
                "below_floor": False,
                "selectable": ["none", "job", "low_il", "appcontainer", "restricted"],
                "unavailable_reasons": {},
            }
        ),
        "/v1/billing/usage": ok(
            {"total_input": 1234, "total_output": 567, "total_calls": 9, "zero_token_events": 0, "period_days": 90}
        ),
        "/v1/admin/billing/price-coverage": ok(
            {"coverage": 1.0, "total_models": 1, "unpriced_models": 0, "unpriced": [], "prices_entry_url": "/billing/prices"}
        ),
        "/v1/shares": ok({"items": [{"share_id": "shr1", "title": "demo", "message_count": 2}]}),
        "/s": http(200, None, '<html><body><div id="app"></div></body></html>'),
        "/v1/subagent/runs": ok({"runs": [], "source": "db"}),
        "/v1/models": ok({"models": [{"provider": "opencode-go", "name": "deepseek-v4.1-flash", "context_window": 1000000}]}),
        "/v1/admin/settings": ok({"category": "python", "config": {"default_model": "deepseek-v4.1-flash"}}),
        "/v1/admin/diagnostics/summary": ok(
            {
                "status": "ok",
                "counts": {"ok": 1},
                "checks": [{"key": "schema", "title": "Schema 版本", "status": "ok"}],
            }
        ),
    }
    routes.update(over)
    return routes


class RuntimeStateTests(unittest.TestCase):
    def test_default_path_uses_appdata(self) -> None:
        env = {"APPDATA": r"C:\Users\x\AppData\Roaming"}
        path = vab.default_runtime_state_path(env)
        self.assertTrue(path.endswith(os.path.join("gh_Athenavi_chiron", "local", "runtime-state.json")))
        self.assertIn("AppData", path)

    def test_desktop_user_data_override_wins(self) -> None:
        env = {"APPDATA": r"C:\a", "CHIRON_DESKTOP_USER_DATA": r"D:\portable"}
        self.assertTrue(vab.default_runtime_state_path(env).startswith(r"D:\portable"))

    def test_resolve_priority_cli_then_env(self) -> None:
        env = {"APPDATA": r"C:\a", "CHIRON_VERIFY_RUNTIME_STATE": "from-env.json"}
        self.assertEqual(vab.resolve_runtime_state_path("from-cli.json", env), "from-cli.json")
        self.assertEqual(vab.resolve_runtime_state_path(None, env), "from-env.json")
        self.assertTrue(vab.resolve_runtime_state_path(None, {"APPDATA": "C:/a"}).endswith("runtime-state.json"))

    def test_no_appdata_and_no_override_is_empty(self) -> None:
        self.assertEqual(vab.default_runtime_state_path({}), "")

    def test_parse_ok(self) -> None:
        parsed = vab.parse_runtime_state(
            json.dumps({"schema": 1, "gatewayPort": 54834, "enginePort": 54848, "pid": 7})
        )
        self.assertEqual(parsed["gatewayPort"], 54834)
        self.assertEqual(parsed["enginePort"], 54848)
        self.assertEqual(parsed["pid"], 7)

    def test_engine_port_optional(self) -> None:
        parsed = vab.parse_runtime_state(json.dumps({"schema": 1, "gatewayPort": 8080}))
        self.assertIsNone(parsed["enginePort"])

    def test_bad_json_raises_state_error(self) -> None:
        with self.assertRaises(vab.StateError):
            vab.parse_runtime_state("{oops")

    def test_schema_mismatch_raises(self) -> None:
        with self.assertRaises(vab.StateError) as ctx:
            vab.parse_runtime_state(json.dumps({"schema": 2, "gatewayPort": 1}))
        self.assertIn("schema", str(ctx.exception))

    def test_missing_or_bad_port_raises(self) -> None:
        for payload in ({"schema": 1}, {"schema": 1, "gatewayPort": 0}, {"schema": 1, "gatewayPort": True},
                        {"schema": 1, "gatewayPort": "abc"}, {"schema": 1, "gatewayPort": 70000}):
            with self.assertRaises(vab.StateError, msg=payload):
                vab.parse_runtime_state(json.dumps(payload))

    def test_missing_file_names_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "runtime-state.json")
            with self.assertRaises(vab.StateError) as ctx:
                vab.load_runtime_state(missing)
            self.assertIn("不存在", str(ctx.exception))

    def test_bom_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            Path(path).write_bytes(b"\xef\xbb\xbf" + json.dumps({"schema": 1, "gatewayPort": 1234}).encode())
            self.assertEqual(vab.load_runtime_state(path)["gatewayPort"], 1234)


class RequestBuildingTests(unittest.TestCase):
    def test_base_url_is_loopback(self) -> None:
        self.assertEqual(vab.build_base_url(54834), "http://127.0.0.1:54834")

    def test_authed_request_carries_bearer(self) -> None:
        req = vab.build_request(vab.build_base_url(1), "/v1/models", token="tok")
        self.assertEqual(req.headers.get("Authorization"), "Bearer tok")
        self.assertIsNone(req.body)

    def test_session_request_is_post_without_auth(self) -> None:
        req = vab.build_session_request(vab.build_base_url(1))
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/auth/session"))
        self.assertNotIn("Authorization", req.headers)
        self.assertEqual(req.body, b"{}")

    def test_page_request_is_anonymous(self) -> None:
        req = vab.build_page_request(vab.build_base_url(1), "shr1")
        self.assertTrue(req.url.endswith("/s/shr1"))
        self.assertNotIn("Authorization", req.headers)

    def test_body_is_json_encoded(self) -> None:
        req = vab.build_request(vab.build_base_url(1), "/x", method="POST", body={"a": 1})
        self.assertEqual(req.headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(req.body.decode()), {"a": 1})

    def test_envelope_and_pickers(self) -> None:
        payload = {"success": True, "data": {"token": " t ", "config": {"default_model": "m"}}}
        self.assertEqual(vab.envelope_data(payload), {"token": " t ", "config": {"default_model": "m"}})
        self.assertEqual(vab.pick_token(payload), "t")
        self.assertEqual(vab.pick_default_model({"data": {"config": {"default_model": " m "}}}), "m")
        self.assertEqual(vab.pick_token({"data": {}}), "")
        self.assertEqual(vab.pick_default_model(None), "")

    def test_redact_hides_secret_and_jwt(self) -> None:
        text = "token=" + GOOD_TOKEN + " other=eyJhbGciOi.eyJzdWIiOi.sig-part-long-enough"
        cleaned = vab.redact(text, (GOOD_TOKEN,))
        self.assertNotIn(GOOD_TOKEN, cleaned)
        self.assertNotIn("eyJhbGciOi", cleaned)
        self.assertIn("token=***", cleaned)

    def test_redact_is_safe_on_empty(self) -> None:
        self.assertEqual(vab.redact(None), "")


class DepsJudgementTests(unittest.TestCase):
    def test_uv_or_pip_pass(self) -> None:
        for tool in ("uv", "pip"):
            check = vab.judge_deps(ok({"tool": tool, "packages": [{"name": "a", "version": "1"}]}))
            self.assertEqual(check.status, vab.PASS, tool)
            self.assertIn(tool, check.detail)

    def test_metadata_is_fail_not_pass(self) -> None:
        check = vab.judge_deps(ok({"tool": "metadata", "packages": [{"name": "a", "version": "1"}], "degraded": True}))
        self.assertEqual(check.status, vab.FAIL)
        self.assertIn("uv", check.hint)

    def test_empty_packages_fail(self) -> None:
        check = vab.judge_deps(ok({"tool": "uv", "packages": []}))
        self.assertEqual(check.status, vab.FAIL)

    def test_engine_error_fails_with_location(self) -> None:
        check = vab.judge_deps(http(502, text='{"error":"pip list exited with code 1"}'))
        self.assertEqual(check.status, vab.FAIL)
        self.assertIn("deps_admin.go", check.hint)


class IsolationJudgementTests(unittest.TestCase):
    def _payload(self, **over) -> dict:
        base = {
            "requested": "appcontainer",
            "effective": "appcontainer",
            "floor": "low_il",
            "degraded": False,
            "below_floor": False,
            "selectable": ["appcontainer"],
            "unavailable_reasons": {},
        }
        base.update(over)
        return base

    def test_consistent_isolation_passes(self) -> None:
        check = vab.judge_isolation(ok(self._payload()))
        self.assertEqual(check.status, vab.PASS)
        self.assertIn("effective=appcontainer", check.detail)

    def test_downgrade_fails(self) -> None:
        check = vab.judge_isolation(ok(self._payload(effective="job", degraded=True)))
        self.assertEqual(check.status, vab.FAIL)
        self.assertIn("降级", check.detail)

    def test_below_floor_fails(self) -> None:
        check = vab.judge_isolation(ok(self._payload(effective="none", below_floor=True)))
        self.assertEqual(check.status, vab.FAIL)

    def test_restricted_unavailable_fails_by_default(self) -> None:
        check = vab.judge_isolation(
            ok(self._payload(unavailable_reasons={"restricted": "缺 SeIncreaseQuotaPrivilege"}))
        )
        self.assertEqual(check.status, vab.FAIL)
        self.assertIn("restricted", check.detail)

    def test_restricted_unavailable_can_be_relaxed(self) -> None:
        check = vab.judge_isolation(
            ok(self._payload(unavailable_reasons={"restricted": "x"})), require_restricted=False
        )
        self.assertEqual(check.status, vab.PASS)
        self.assertTrue(any("restricted" in n for n in check.notes))

    def test_unknown_shape_fails_without_crashing(self) -> None:
        for payload in (None, {}, {"effective": ""}):
            check = vab.judge_isolation(ok(payload))
            self.assertEqual(check.status, vab.FAIL, payload)

    def test_transport_error_fails(self) -> None:
        check = vab.judge_isolation(http(None, error="URLError: refused"))
        self.assertEqual(check.status, vab.FAIL)


class BillingJudgementTests(unittest.TestCase):
    def test_usage_positive_passes(self) -> None:
        check = vab.judge_usage(ok({"total_input": 10, "total_output": 2, "total_calls": 3, "period_days": 90}))
        self.assertEqual(check.status, vab.PASS)

    def test_usage_zero_fails_with_sum_hint(self) -> None:
        check = vab.judge_usage(
            ok({"total_input": 0, "total_calls": 30, "zero_token_events": 0, "total_unpriced_calls": 28, "period_days": 90})
        )
        self.assertEqual(check.status, vab.FAIL)
        self.assertIn("SUM", check.hint)
        self.assertIn("total_calls=30", check.detail)

    def test_usage_missing_endpoint_fails(self) -> None:
        check = vab.judge_usage(http(404, text='{"error":"not found"}'))
        self.assertEqual(check.status, vab.FAIL)
        self.assertIn("billing_usage.go", check.hint)

    def test_price_coverage_never_fails(self) -> None:
        unpriced = ok({"coverage": 0.0, "total_models": 1, "unpriced_models": 1,
                       "unpriced": [{"model": "deepseek-v4.1-flash", "calls": 29}]})
        check = vab.judge_price_coverage(unpriced)
        self.assertEqual(check.status, vab.INFO)
        self.assertIn("deepseek-v4.1-flash", check.detail)
        self.assertEqual(vab.judge_price_coverage(http(503, text="{}")).status, vab.INFO)


class ShareJudgementTests(unittest.TestCase):
    def test_no_share_is_skip(self) -> None:
        check, token = vab.judge_shares(ok({"items": []}))
        self.assertEqual(check.status, vab.SKIP)
        self.assertEqual(token, "")

    def test_share_returns_token(self) -> None:
        check, token = vab.judge_shares(ok({"items": [{"share_id": "shr1"}]}))
        self.assertEqual(check.status, vab.PASS)
        self.assertEqual(token, "shr1")

    def test_list_error_fails(self) -> None:
        check, token = vab.judge_shares(http(500, text="{}"))
        self.assertEqual(check.status, vab.FAIL)
        self.assertEqual(token, "")

    def test_page_with_spa_marker_passes(self) -> None:
        self.assertEqual(vab.judge_share_page(http(200, None, '<div id="app"></div>')).status, vab.PASS)
        self.assertEqual(vab.judge_share_page(http(200, None, "<div id='app'>")).status, vab.FAIL)
        self.assertEqual(vab.judge_share_page(http(200, None, '<div id="app">')).status, vab.PASS)

    def test_page_without_marker_fails(self) -> None:
        check = vab.judge_share_page(http(200, None, "<html>directory listing</html>"))
        self.assertEqual(check.status, vab.FAIL)

    def test_page_404_fails_with_static_hint(self) -> None:
        check = vab.judge_share_page(http(404, text="not found"))
        self.assertEqual(check.status, vab.FAIL)
        self.assertIn("web_static.go", check.hint)


class SubagentAndModelJudgementTests(unittest.TestCase):
    def test_missing_route_fails(self) -> None:
        self.assertEqual(vab.judge_subagent_runs(http(404, text="{}")).status, vab.FAIL)

    def test_no_runs_is_skip(self) -> None:
        self.assertEqual(vab.judge_subagent_runs(ok({"runs": []})).status, vab.SKIP)
        self.assertEqual(vab.judge_subagent_runs(ok({"runs": [{"run_id": "r1"}]})).status, vab.PASS)

    def test_models_empty_fails(self) -> None:
        check = vab.judge_models(ok({"models": []}), default_model="m")
        self.assertEqual(check.status, vab.FAIL)

    def test_models_reports_default(self) -> None:
        check = vab.judge_models(ok({"models": [{"name": "m1"}]}), default_model="m1")
        self.assertEqual(check.status, vab.PASS)
        self.assertIn("default_model=m1", check.detail)
        self.assertIn("未设置", vab.judge_models(ok({"models": [{"name": "m1"}]}), "").detail)


class DiagnosticsJudgementTests(unittest.TestCase):
    def test_statuses_are_listed(self) -> None:
        payload = ok({
            "status": "warn",
            "counts": {"ok": 1, "warn": 1},
            "checks": [
                {"key": "schema", "title": "Schema 版本", "status": "ok"},
                {"key": "deps", "title": "Python 依赖通道", "status": "warn"},
            ],
        })
        check = vab.judge_diagnostics(payload)
        self.assertEqual(check.status, vab.INFO)
        self.assertIn("Schema 版本=ok", check.detail)
        self.assertIn("Python 依赖通道=warn", check.detail)
        self.assertTrue(any("warn" in n for n in check.notes))

    def test_masked_key_is_called_out(self) -> None:
        payload = ok({"status": "ok", "checks": [{"key": "********", "title": "Schema 版本", "status": "ok"}]})
        check = vab.judge_diagnostics(payload)
        self.assertTrue(any("sanitize_response.go" in n for n in check.notes))

    def test_unreadable_is_info_not_fail(self) -> None:
        self.assertEqual(vab.judge_diagnostics(http(500, text="{}")).status, vab.INFO)


class ReportTests(unittest.TestCase):
    def test_only_fail_drives_exit_code(self) -> None:
        checks = [
            vab.Check("a", "t", vab.PASS),
            vab.Check("b", "t", vab.SKIP),
            vab.Check("c", "t", vab.INFO),
        ]
        self.assertEqual(vab.exit_code(checks), 0)
        self.assertEqual(vab.summarize(checks), {vab.PASS: 1, vab.FAIL: 0, vab.SKIP: 1, vab.INFO: 1})
        checks.append(vab.Check("d", "t", vab.FAIL))
        self.assertEqual(vab.exit_code(checks), 1)

    def test_report_json_is_serialisable(self) -> None:
        report = vab.build_report([vab.Check("a", "标题", vab.PASS, "细节")], {"base_url": "http://127.0.0.1:1"})
        self.assertTrue(report["ok"])
        self.assertEqual(json.loads(json.dumps(report, ensure_ascii=False))["checks"][0]["title"], "标题")

    def test_text_view_shows_counts_and_hint(self) -> None:
        report = vab.build_report(
            [vab.Check("a", "标题", vab.FAIL, "坏了", hint="看这里")], {"base_url": "http://127.0.0.1:1"}
        )
        text = vab.render_text(report)
        self.assertIn("PASS=0", text)
        self.assertIn("FAIL=1", text)
        self.assertIn("next: 看这里", text)


class RunChecksTests(unittest.TestCase):
    def test_all_green_run(self) -> None:
        gw = FakeGateway(full_routes())
        checks, meta = vab.run_checks(gw, state_fixture(), "http://127.0.0.1:54834")
        self.assertNotIn("fatal", meta)
        self.assertEqual(vab.exit_code(checks), 0, [c.as_dict() for c in checks])
        ids = [c.id for c in checks]
        for expected in ("0a", "0b", "0b2", "a", "b", "c", "c2", "d", "d2", "e", "e2", "f"):
            self.assertIn(expected, ids)

    def test_token_never_leaks_into_report(self) -> None:
        gw = FakeGateway(full_routes())
        checks, meta = vab.run_checks(gw, state_fixture(), "http://127.0.0.1:54834")
        report = vab.build_report(checks, meta)
        blob = json.dumps(report, ensure_ascii=False) + vab.render_text(report)
        self.assertNotIn(GOOD_TOKEN, blob)

    def test_authed_requests_use_bearer(self) -> None:
        gw = FakeGateway(full_routes())
        vab.run_checks(gw, state_fixture(), "http://127.0.0.1:54834")
        models = gw.find("/v1/models")
        self.assertTrue(models)
        self.assertEqual(models[0].headers.get("Authorization"), "Bearer " + GOOD_TOKEN)

    def test_share_page_is_fetched_anonymously(self) -> None:
        gw = FakeGateway(full_routes())
        vab.run_checks(gw, state_fixture(), "http://127.0.0.1:54834")
        page = [r for r in gw.calls if "/s/" in r.url]
        self.assertEqual(len(page), 1)
        self.assertNotIn("Authorization", page[0].headers)

    def test_not_ready_is_fatal(self) -> None:
        gw = FakeGateway(full_routes(**{"/ready": http(503, text="down")}))
        checks, meta = vab.run_checks(gw, state_fixture(), "http://127.0.0.1:54834")
        self.assertIn("fatal", meta)
        self.assertEqual(checks[-1].status, vab.FAIL)
        # 没就绪就不该继续打业务端点
        self.assertFalse([r for r in gw.calls if "/v1/admin/deps/list" in r.url])

    def test_session_failure_is_fatal(self) -> None:
        gw = FakeGateway(full_routes(**{"/v1/auth/session": http(403, text='{"error":"loopback only"}')}))
        checks, meta = vab.run_checks(gw, state_fixture(), "http://127.0.0.1:54834")
        self.assertIn("fatal", meta)
        self.assertEqual([c.id for c in checks], ["0a", "0b", "0b2"])
        self.assertIn("loopback", checks[-1].hint)

    def test_masked_token_is_reported_not_accepted(self) -> None:
        gw = FakeGateway(full_routes(**{"/v1/auth/session": ok({"token": "********", "user": {}})}))
        checks, meta = vab.run_checks(
            gw, state_fixture(), "http://127.0.0.1:54834"
        )
        self.assertIn("fatal", meta)
        self.assertIn("********", checks[-1].detail)

    def test_restricted_unavailable_surfaces_as_fail(self) -> None:
        gw = FakeGateway(full_routes(**{
            "/v1/system/isolation": ok({
                "requested": "appcontainer", "effective": "appcontainer", "floor": "low_il",
                "degraded": False, "below_floor": False, "selectable": ["appcontainer"],
                "unavailable_reasons": {"restricted": "CreateProcessAsUserW 失败 err=5"},
            })
        }))
        checks, _ = vab.run_checks(gw, state_fixture(), "http://127.0.0.1:54834")
        isolation = next(c for c in checks if c.id == "b")
        self.assertEqual(isolation.status, vab.FAIL)
        self.assertEqual(vab.exit_code(checks), 1)


class MainCliTests(unittest.TestCase):
    def _write_state(self, tmp: str) -> str:
        path = os.path.join(tmp, "runtime-state.json")
        Path(path).write_text(json.dumps({"schema": 1, "gatewayPort": 54834, "enginePort": 54848}), encoding="utf-8")
        return path

    def _run(self, argv: list[str], transport) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = vab.main(argv, transport=transport)
        return code, buf.getvalue()

    def test_exit_zero_and_text_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp)
            code, out = self._run(["--runtime-state", path], FakeGateway(full_routes()))
        self.assertEqual(code, 0)
        self.assertIn("汇总：PASS=", out)
        self.assertIn("通过（无 FAIL）", out)

    def test_json_mode_emits_only_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp)
            code, out = self._run(["--runtime-state", path, "--json"], FakeGateway(full_routes()))
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertNotIn(GOOD_TOKEN, out)

    def test_missing_state_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.json")
            code, out = self._run(["--runtime-state", missing, "--json"], FakeGateway(full_routes()))
        report = json.loads(out)
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertIn("fatal", report["meta"])

    def test_unhealthy_stack_exits_one(self) -> None:
        # 端口通、/ready 5xx：这是这一版重建的缺陷，不该被当成"应用没启动"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp)
            code, _ = self._run(["--runtime-state", path], FakeGateway(full_routes(**{"/ready": http(503, text="down")})))
        self.assertEqual(code, 1)

    def test_unreachable_stack_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp)
            code, out = self._run(
                ["--runtime-state", path, "--json"],
                FakeGateway(full_routes(**{"/ready": http(None, error="URLError: [Errno 111] refused")})),
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["meta"]["fatal_kind"], "unreachable")

    def test_fail_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp)
            code, out = self._run(
                ["--runtime-state", path],
                FakeGateway(full_routes(**{"/v1/billing/usage": ok({"total_input": 0, "total_calls": 30})})),
            )
        self.assertEqual(code, 1)
        self.assertIn("存在 FAIL", out)

    def test_base_url_override_skips_port_from_state(self) -> None:
        gw = FakeGateway(full_routes())
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp)
            code, _ = self._run(["--runtime-state", path, "--base-url", "http://127.0.0.1:9999"], gw)
        self.assertEqual(code, 0)
        self.assertTrue(all(r.url.startswith("http://127.0.0.1:9999/") for r in gw.calls))

    def test_ignore_restricted_flag_reaches_judgement(self) -> None:
        routes = full_routes(**{
            "/v1/system/isolation": ok({
                "requested": "appcontainer", "effective": "appcontainer", "floor": "low_il",
                "degraded": False, "below_floor": False, "selectable": ["appcontainer"],
                "unavailable_reasons": {"restricted": "helper 未启用"},
            })
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_state(tmp)
            strict_code, _ = self._run(["--runtime-state", path, "--json"], FakeGateway(routes))
            relaxed_code, _ = self._run(["--runtime-state", path, "--json", "--ignore-restricted"], FakeGateway(routes))
        self.assertEqual(strict_code, 1)
        self.assertEqual(relaxed_code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
