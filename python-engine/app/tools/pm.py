"""PM tools (prd_generate / tech_design / task_decompose / requirement_validate) 注册到本地工具注册表。

实现对标 Go `internal/pm/tools.go`，通过 GatewayRouter.chat 调用 LLM：
- LLM 不可用时返回 fallback 模板，保持工具可用性。
"""

from __future__ import annotations

from typing import Any

from app.gateway.provider import ChatMessage
from app.gateway.router import GatewayRouter
from app.tools.registry import registry

_gateway: GatewayRouter | None = None
_default_model = "gpt-4o-mini"


def bind_gateway(gw: GatewayRouter, model: str = "") -> None:
    global _gateway, _default_model
    _gateway = gw
    if model:
        _default_model = model


async def _call_llm(system: str, user: str) -> str:
    if _gateway is None:
        return f"[LLM unavailable]\n\n{user}"
    resp = await _gateway.chat(
        messages=[
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ],
        model=_default_model,
        max_tokens=2048,
    )
    if resp.finish_reason == "error" or not resp.content:
        return f"[LLM error]\n\n{user}"
    return resp.content


async def prd_generate(description: str, context: str = "") -> dict[str, Any]:
    if not description:
        return {"error": "description is required"}
    system = (
        "You are a senior product manager. Generate a comprehensive Product Requirements Document (PRD) in Markdown.\n\n"
        "Include these sections:\n"
        "1. **Overview**\n2. **Goals & Objectives**\n3. **User Stories**\n4. **Functional Requirements**\n"
        "5. **Non-Functional Requirements**\n6. **Success Metrics**\n\n"
        "Be specific, actionable, and well-structured. Output only the PRD content in Markdown."
    )
    user = f"Product: {description}" + (f"\nContext: {context}" if context else "")
    out = await _call_llm(system, user)
    return {"output": out, "prd": out}


async def tech_design(prd: str) -> dict[str, Any]:
    if not prd:
        return {"error": "prd content is required"}
    system = (
        "You are a senior software architect. Generate a detailed Technical Design Document in Markdown based on the provided PRD.\n\n"
        "Include:\n1. **Architecture Overview**\n2. **Technology Stack**\n3. **API Design**\n"
        "4. **Data Model**\n5. **Module Breakdown**\n6. **Security Considerations**\n7. **Deployment Strategy**\n\n"
        "Output only the technical design in Markdown."
    )
    out = await _call_llm(
        system, f"Based on this PRD, generate a technical design:\n\n{prd}"
    )
    return {"output": out}


async def task_decompose(prd: str) -> dict[str, Any]:
    if not prd:
        return {"error": "prd content is required"}
    system = (
        "You are a senior engineering manager. Decompose the given PRD into a structured set of development tasks.\n\n"
        "For each task include:\n- Task ID and Title\n- Description\n- Priority\n- Dependencies\n"
        "- Estimated Effort\n- Acceptance Criteria\n\nGroup tasks by phase or milestone. Output in Markdown."
    )
    out = await _call_llm(
        system, f"Decompose this PRD into development tasks:\n\n{prd}"
    )
    return {"output": out}


async def requirement_validate(requirements: str) -> dict[str, Any]:
    if not requirements:
        return {"error": "requirements content is required"}
    system = (
        "You are a requirements analyst. Validate the given requirements for completeness and consistency.\n\n"
        "Check for:\n1. Clarity\n2. Completeness\n3. Consistency\n4. Feasibility\n5. Testability\n\n"
        "For each issue provide Severity / Description / Recommendation. Output a Markdown validation report with summary score (PASS/CONDITIONAL/FAIL)."
    )
    out = await _call_llm(system, f"Validate these requirements:\n\n{requirements}")
    return {"output": out}


registry.register(
    name="prd_generate",
    description="Generate a structured Product Requirements Document from a natural language description.",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "context": {"type": "string", "default": ""},
        },
        "required": ["description"],
    },
    handler=prd_generate,
)

registry.register(
    name="tech_design",
    description="Generate architecture, API design, and data models from PRD.",
    parameters={
        "type": "object",
        "properties": {"prd": {"type": "string"}},
        "required": ["prd"],
    },
    handler=tech_design,
)

registry.register(
    name="task_decompose",
    description="Break down PRD into a graph of executable development tasks.",
    parameters={
        "type": "object",
        "properties": {"prd": {"type": "string"}},
        "required": ["prd"],
    },
    handler=task_decompose,
)

registry.register(
    name="requirement_validate",
    description="Validate requirements for completeness and consistency.",
    parameters={
        "type": "object",
        "properties": {"requirements": {"type": "string"}},
        "required": ["requirements"],
    },
    handler=requirement_validate,
)
