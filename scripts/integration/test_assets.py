#!/usr/bin/env python3
"""集成测试带的**静态**校验（不需要 docker）。

本机没有 Docker 守护进程时，真机链路只能以 77 跳过；但"脚本写错了"不该等到
有 Docker 的机器上才发现。这里把三类容易漂移的东西钉住：

1. `docker-compose.integration.yml` 的结构：服务齐不齐、端口是否互相冲突、
   密钥是否三处同值、启动顺序（depends_on / healthcheck）是否还在；
2. `run.sh` 的 bash 语法（`bash -n`）与关键断言是否还在（防止被误删）；
3. JWT 自签与假 LLM 的流式形状 —— 这两段是链路里唯一纯 Python 且可离线跑的部分，
   直接起一个临时端口验证。

用法：
    python scripts/integration/test_assets.py
    make integration-static
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.integration.yml"
RUN_SH = ROOT / "scripts" / "integration" / "run.sh"
MINT = ROOT / "scripts" / "integration" / "mint_token.py"
FAKE_LLM = ROOT / "scripts" / "integration" / "fake_llm" / "fake_llm.py"
CI_SMOKE = ROOT / "scripts" / "ci-smoke.sh"
MAKEFILE = ROOT / "Makefile"

try:
    import yaml  # type: ignore
except Exception:  # noqa: BLE001 — 缺 PyYAML 时跳过结构断言，不判失败
    yaml = None


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(yaml is None, "PyYAML 未安装")
class ComposeStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        cls.services = cls.doc.get("services", {})

    def test_required_services_present(self) -> None:
        for name in (
            "postgres",
            "redis",
            "migrate",
            "gateway",
            "python-engine",
            "pageindex-service",
            "pageindex-worker",
            "fake-llm",
        ):
            self.assertIn(name, self.services, f"缺少服务 {name}")

    def test_images_are_pinned(self) -> None:
        self.assertEqual(self.services["postgres"]["image"], "postgres:16")
        self.assertEqual(self.services["redis"]["image"], "redis:7")

    def test_host_ports_are_isolated_and_unique(self) -> None:
        hosts: list[str] = []
        for name, svc in self.services.items():
            for mapping in svc.get("ports", []) or []:
                # "127.0.0.1:15432:5432"
                host_port = str(mapping).split(":")[-2]
                hosts.append(host_port)
                self.assertNotIn(host_port, ("5432", "6379", "8080", "8000"), f"{name} 占用了开发环境端口")
        self.assertEqual(len(hosts), len(set(hosts)), f"宿主机端口重复：{hosts}")

    def test_all_long_running_services_have_healthchecks(self) -> None:
        for name in ("postgres", "redis", "gateway", "python-engine", "pageindex-service", "fake-llm"):
            self.assertIn("healthcheck", self.services[name], f"{name} 缺少 healthcheck")

    def test_startup_order_is_health_gated(self) -> None:
        gw = self.services["gateway"]["depends_on"]
        self.assertEqual(gw["migrate"]["condition"], "service_completed_successfully")
        self.assertEqual(gw["postgres"]["condition"], "service_healthy")
        self.assertEqual(gw["redis"]["condition"], "service_healthy")

        eng = self.services["python-engine"]["depends_on"]
        # 引擎启动时要拉 /v1/internal/engine-config，网关必须先就绪。
        self.assertEqual(eng["gateway"]["condition"], "service_healthy")
        self.assertEqual(eng["fake-llm"]["condition"], "service_healthy")

    def test_secrets_are_consistent(self) -> None:
        env_of = {name: (svc.get("environment") or {}) for name, svc in self.services.items()}
        for key in ("APP_SECRET", "JWT_SECRET", "INTERNAL_TOKEN", "POSTGRES_DSN"):
            values = {name: env.get(key) for name, env in env_of.items() if key in env}
            self.assertGreaterEqual(len(values), 2, f"{key} 只在少数服务里出现")
            self.assertEqual(len(set(values.values())), 1, f"{key} 在不同服务里不一致：{values}")

    def test_engine_points_at_fake_llm(self) -> None:
        env = self.services["python-engine"]["environment"]
        self.assertIn("fake-llm:8081", env["DEEPSEEK_BASE_URL"])
        self.assertTrue(env["DEEPSEEK_API_KEY"])
        # 网关默认模型是 deepseek-chat（internal/api/session_runtime.go 的 fallbackModel），
        # 假 LLM 必须能被 deepseek 前缀路由命中。
        self.assertEqual(env["LLM_MODEL"], "deepseek-chat")

    def test_project_volume_is_shared_by_gateway_and_engine(self) -> None:
        for name in ("gateway", "python-engine"):
            mounts = [str(v) for v in self.services[name].get("volumes", [])]
            self.assertTrue(any("/srv/chiron/projects" in m for m in mounts), f"{name} 未挂载 projects 卷")

    def test_app_secret_matches_gateway_doc_default(self) -> None:
        # run.sh 自签 JWT 用的是这个值；改 compose 忘了改脚本会让断言在第 2 步就红。
        env = self.services["gateway"]["environment"]
        text = RUN_SH.read_text(encoding="utf-8")
        self.assertIn(f'JWT_SECRET="{env["JWT_SECRET"]}"', text)


class ScriptWiringTest(unittest.TestCase):
    def _bash(self) -> str:
        """定位一个**真正的** bash（Git Bash / MSYS），不要 WSL 启动器。

        Windows 的 PATH 上 `bash` 常先解析到 System32\\bash.exe（WSL），没有发行版时
        它对任何参数都返回 1 且 stderr 为空 —— 测试会以"语法错误"的假象失败。
        """
        candidate = shutil.which("bash")
        if candidate and "system32" not in candidate.lower():
            return candidate
        for path in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ):
            if os.path.exists(path):
                return path
        return candidate or "bash"

    def test_run_sh_syntax(self) -> None:
        proc = subprocess.run(
            [self._bash(), "-n", str(RUN_SH)], capture_output=True, check=False
        )
        stderr = proc.stderr.decode("utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, stderr)

    def test_ci_smoke_syntax(self) -> None:
        proc = subprocess.run(
            [self._bash(), "-n", str(CI_SMOKE)], capture_output=True, check=False
        )
        stderr = proc.stderr.decode("utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, stderr)

    def test_run_sh_assertion_anchors(self) -> None:
        text = RUN_SH.read_text(encoding="utf-8")
        for anchor in (
            "/v1/agent/submit",
            "turn_done",
            "usage_events",
            "project_checkpoints",
            "/v1/checkpoints/$CP_ID/restore",
            "/v1/share/$SHARE_ID",
            "/v1/kb/$kb_id/query",
            "/v1/admin/deps/list",
            "/v1/system/isolation",
            "exit 77",
        ):
            self.assertIn(anchor, text, f"run.sh 缺少关键断言锚点：{anchor}")

    def test_run_sh_skips_without_docker(self) -> None:
        text = RUN_SH.read_text(encoding="utf-8")
        self.assertIn("docker info", text)
        self.assertIn("skipped_exit", text)

    def test_ci_smoke_has_opt_in_integration(self) -> None:
        text = CI_SMOKE.read_text(encoding="utf-8")
        self.assertIn("--integration", text)
        self.assertIn("scripts/integration/run.sh", text)

    def test_makefile_target(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^integration:")
        self.assertIn("scripts/integration/run.sh", text)


class TokenTest(unittest.TestCase):
    def test_mint_matches_go_claim_shape(self) -> None:
        mint = _load_module(MINT, "integration_mint_token")
        secret = "integration-jwt-secret-0123456789abcdef"
        token = mint.mint(secret, now=1_700_000_000, ttl_seconds=600)
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(mint.base64.urlsafe_b64decode(header_b64 + "=="))
        payload = json.loads(mint.base64.urlsafe_b64decode(payload_b64 + "=="))
        self.assertEqual(header["alg"], "HS256")
        self.assertEqual(payload["role"], "owner")
        self.assertEqual(payload["tenant_id"], 1)
        self.assertEqual(payload["uid"], mint.DEFAULT_USER_ID)
        self.assertEqual(payload["exp"] - payload["iat"], 600)
        # 签名可复算（与 Go 侧 HS256 同一口径）。
        import hashlib
        import hmac

        expected = hmac.new(
            secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
        ).digest()
        self.assertEqual(
            mint.base64.urlsafe_b64encode(expected).decode().rstrip("="), sig_b64
        )
        self.assertNotIn("perms", payload, "owner 的 Perms 必须是 nil 才会回退角色默认权限")

    def test_mint_rejects_empty_secret(self) -> None:
        mint = _load_module(MINT, "integration_mint_token_empty")
        with self.assertRaises(ValueError):
            mint.mint("")


class FakeLLMTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module(FAKE_LLM, "integration_fake_llm")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, payload: dict) -> str:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        return body

    def test_stream_carries_final_usage_chunk(self) -> None:
        body = self._post({"model": "deepseek-chat", "stream": True, "messages": []})
        self.assertIn("data: [DONE]", body)
        self.assertIn('"usage"', body)
        self.assertIn('"prompt_tokens"', body)
        self.assertIn(self.mod.ANSWER_TEXT, body)

    def test_non_stream_shape(self) -> None:
        body = self._post({"model": "deepseek-chat", "stream": False, "messages": []})
        data = json.loads(body)
        self.assertEqual(data["choices"][0]["message"]["role"], "assistant")
        self.assertGreater(data["usage"]["prompt_tokens"], 0)


if __name__ == "__main__":
    os.chdir(ROOT)
    unittest.main(verbosity=2)
