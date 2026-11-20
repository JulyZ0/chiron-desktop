#!/usr/bin/env python3
"""tiny 假 LLM —— OpenAI Chat Completions 兼容，返回**带 usage 的流式响应**。

存在理由：集成链路里"usage_events 非零"必须由真实上游用量驱动，而不是测试脚本
直接往表里插数。引擎 `OpenAIProvider.chat_stream` 只在收到 `stream_options.
include_usage` 的最终 usage chunk 时才把 token 数带出来（见
python-engine/app/providers/openai.py），所以这里必须**按 OpenAI 的流式形状**发：

    data: {"choices":[{"delta":{"role":"assistant","content":"..."}}]}
    data: {"choices":[{"delta":{},"finish_reason":"stop"}]}
    data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":7,...}}
    data: [DONE]

同时支持非流式（pageindex/litellm 建树时用），以及 /v1/models（引擎的模型发现）。

只依赖标准库：这个容器越薄，构建越快，也越不容易成为链路里最脆的一环。
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 固定用量：数值不重要，"非零且可预测"才重要（run.sh 会断言 >= 这些下限）。
PROMPT_TOKENS = 42
COMPLETION_TOKENS = 7

#: 固定回答。知识库"命中"断言依赖它是非空的（见 run.sh 的 KB 段）。
ANSWER_TEXT = "pong from the integration fake LLM; the fixture codeword is ORANGE-COMPASS."


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── 基础 ────────────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
        sys.stderr.write("[fake-llm] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, frames: list[str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in frames:
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
        self.close_connection = True

    # ── 路由 ────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", "/health"):
            self._send_json(200, {"status": "ok"})
            return
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "deepseek-chat", "object": "model", "owned_by": "integration"},
                        {"id": "mock-model", "object": "model", "owned_by": "integration"},
                    ],
                },
            )
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid json"}})
            return

        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/chat/completions"):
            if bool(body.get("stream")):
                self._stream_completion(body)
            else:
                self._json_completion(body)
            return
        # Responses API 不走这里（引擎用独立 provider），但给出可诊断的 404 而非静默挂住。
        self._send_json(404, {"error": {"message": f"unsupported path: {self.path}"}})

    def _json_completion(self, body: dict) -> None:
        model = str(body.get("model") or "deepseek-chat")
        self._send_json(
            200,
            {
                "id": "chatcmpl-integration",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ANSWER_TEXT},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": PROMPT_TOKENS,
                    "completion_tokens": COMPLETION_TOKENS,
                    "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                },
            },
        )

    def _stream_completion(self, body: dict) -> None:
        model = str(body.get("model") or "deepseek-chat")
        chunk_id = "chatcmpl-integration"
        created = int(time.time())

        def frame(payload: dict) -> str:
            return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"

        base = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model}
        frames = [
            frame({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ANSWER_TEXT}, "finish_reason": None}]}),
            frame({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            # 只带 usage 的最终 chunk（OpenAI 在 stream_options.include_usage 下的形状）。
            frame(
                {
                    **base,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": PROMPT_TOKENS,
                        "completion_tokens": COMPLETION_TOKENS,
                        "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                    },
                }
            ),
            "data: [DONE]\n\n",
        ]
        self._send_sse(frames)


def main() -> None:
    port = 8081
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[fake-llm] listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
