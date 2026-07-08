"""通用 Agent 引擎：Tool/ToolRegistry/Persona/run_agent/AgentResult。

把诊断专用 agent 抽象成 persona 驱动的通用 ReAct 引擎。
循环 / per-tool 异常隔离 / 降级 / 指标沿用 diagnose_agent_service 既有实现。
工具集中定义在 agent_tools.py（run_agent 内 lazy import 避免循环）。
"""
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.providers.factory import get_llm_provider

MAX_ITER_DEFAULT = 6


@dataclass
class Tool:
    """一个工具 = OpenAI schema + handler，绑成单一对象避免 schema/handler 不一致。"""
    name: str
    description: str
    parameters: dict
    handler: Callable[[AsyncSession, Optional[str], dict], Awaitable[str]]

    @property
    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


class ToolRegistry:
    """工具注册表：register/get/schemas_for/run。run 内含 per-tool 异常隔离。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas_for(self, names: list[str]) -> list[dict]:
        out = []
        for n in names:
            t = self._tools.get(n)
            if t:
                out.append(t.schema)
        return out

    async def run(self, db, model_type, name: str, args: dict) -> tuple[str, bool]:
        """执行工具，返回 (result_text, error)。失败不抛，循环不崩。"""
        t = self._tools.get(name)
        if not t:
            return f"未知工具: {name}", True
        try:
            result = await t.handler(db, model_type, **(args or {}))
            return result, False
        except Exception as e:
            degraded(f"agent_tool_{name}", e)
            return f"工具 {name} 执行失败: {type(e).__name__}: {e}", True


@dataclass
class Persona:
    """场景配置：system prompt + 工具子集 + 参数 + 输出格式 + 降级目标。"""
    name: str
    system_prompt: str
    allowed_tools: list[str]
    max_iter: int = MAX_ITER_DEFAULT
    temperature: float = 0.2
    max_tokens: int = 1500
    output_format: str = "text"          # "json" | "text"
    fallback: Optional[Callable[[AsyncSession, str, Optional[str]], Awaitable[dict]]] = None
    config_source: str = "code"          # 预留 S5："code" | "db"


@dataclass
class AgentResult:
    answer: object                        # str（text）| dict（json）
    steps: list[dict]                     # [{iter, thought, tool, args, result, error}]
    iterations: int
    degraded: bool
    degrade_reason: Optional[str]
    latency_ms: int
    persona: str
    tools_used: list[str]


def _extract_json(ans: str) -> Optional[dict | list]:
    """从 LLM 输出中正则提取 JSON（自包含副本，与 domain_service 一致，不引入跨服务依赖）。"""
    m = re.search(r"(\{.*\}|\[.*\])", ans or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _to_openai_tool_calls(tool_calls):
    """内部 dict 形式 tool_calls → openai assistant 消息需要的结构。"""
    return [{"id": tc["id"], "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in tool_calls]
