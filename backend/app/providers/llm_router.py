"""LLM 模型路由：fallback 链 + 健康度熔断 + query 特征分档(L2)。

直击痛点：deepseek-v4-flash 实测会返空 answer，原 get_llm_provider 只字符串映射、
无 fallback，直接把空结果返给用户。本模块在 provider 外包一层 FallbackLLMProvider：
- L0：主 provider 异常/空输出 → 按 fallback 链切备；
- L1：后台周期探活 + 熔断冷却（复用 main._refresh_component_health_loop 范式）；
- L2：query 特征分档(turbo/plus, opt-in)，复用 routing.query_classifier.extract_features。

健康态进程内（单 worker 够用）；多 worker 共享列为后续（复用 config_service Redis）。
"""
import asyncio
import time

from app.config import settings
from app.core.obs import degraded


# ===== L1 熔断状态（进程内；后台 _refresh_llm_health_loop 写，resolve_chain 读）=====
# {provider: [fail_count, cooldown_until_ts]}
_PROVIDER_HEALTH: dict[str, list] = {}


def is_healthy(p: str) -> bool:
    """provider 是否健康（未在熔断冷却中）。未知 provider 视为健康（初始化前不阻塞主链路）。"""
    rec = _PROVIDER_HEALTH.get(p)
    if not rec:
        return True
    _, cooldown_until = rec
    return not (cooldown_until and time.time() < cooldown_until)


def record_fail(p: str) -> None:
    """记录一次失败；连续达 LLM_CIRCUIT_FAIL_N → 进冷却。"""
    rec = _PROVIDER_HEALTH.setdefault(p, [0, 0.0])
    rec[0] += 1
    if rec[0] >= settings.LLM_CIRCUIT_FAIL_N:
        rec[1] = time.time() + settings.LLM_CIRCUIT_COOLDOWN
        degraded("llm_circuit_open", Exception(f"provider={p} 连续失败{rec[0]}次"),
                 f"熔断冷却{settings.LLM_CIRCUIT_COOLDOWN}s")


def record_ok(p: str) -> None:
    """成功 → 清零失败计数 + 解除冷却。"""
    _PROVIDER_HEALTH[p] = [0, 0.0]


# ===== L2 query 特征分档 =====
def classify_llm(query: str) -> tuple[str, str]:
    """query 特征 → (tier, reason)。tier: turbo(快/简单) | plus(强/复杂)。

    复用 routing.query_classifier.extract_features 的 6 维特征。LLM_TIER_ENABLE 关 → 恒 plus。
    """
    if not getattr(settings, "LLM_TIER_ENABLE", False):
        return "plus", "tier_disabled"
    try:
        from app.routing.query_classifier import extract_features
        f = extract_features(query)
        if f.has_standard_reference:
            return "turbo", "标准引用·精确查询"
        if f.query_type == "keyword" and f.query_length <= 8:
            return "turbo", "短关键词·精确查询"
        if f.query_length <= 12 and f.term_density >= 0.25 and not f.has_numeric_param:
            return "turbo", "术语密集·简短查询"
        if f.query_type == "fault":
            return "plus", "故障诊断·需推理"
        # FACT_FAST（opt-in，关=现状）：natural 低推理短问走 turbo。
        # 语义：查"是什么/有哪些"的事实清单类问题，检索质量决定答案质量、生成只需组织语言。
        # 密度阈值 0.05 = ≤20 字时至少命中 1 个 _TERM_DICT 领域词（实测样本密度 0.071）。
        if (getattr(settings, "LLM_TIER_FACT_FAST_ENABLE", False)
                and f.query_type == "natural"
                and f.query_length <= 20
                and f.term_density >= 0.05
                and not f.has_numeric_param):
            return "turbo", f"事实短问·低推理(密度{f.term_density:.2f})"
        return "plus", "默认质量档"
    except Exception as e:
        degraded("llm_classify", e)
        return "plus", "classify_fallback"


# ===== 链解析 =====
def _fallback_chain() -> list[str]:
    """读取 fallback 链（热配置优先，回落 settings）；ollama 禁用时从链中剔除（管理员热开关）。"""
    try:
        from app.services.config_service import rt_llm_fallback_chain, rt_ollama_enable
        chain = rt_llm_fallback_chain()
        if not rt_ollama_enable():
            chain = [p for p in chain if p != "ollama"]
        return chain
    except Exception:
        pass
    return [s.strip() for s in settings.LLM_FALLBACK_CHAIN.split(",") if s.strip()]


def resolve_chain(model_type: str | None = None) -> list[str]:
    """返回有序 provider 名列表。用户显式选优先；去重保序（健康度过滤由 factory 调 is_healthy）。"""
    chain: list[str] = []
    seen: set[str] = set()

    def add(p: str | None) -> None:
        if p and p not in ("default", "auto") and p not in seen:
            seen.add(p)
            chain.append(p)

    add(model_type)              # 1) 用户显式选优先
    for p in _fallback_chain():  # 2) fallback 链
        add(p)
    return chain


# ===== L0 FallbackLLMProvider =====
class FallbackLLMProvider:
    """包一组 provider，主失败/空 → 按链切备。duck-typing 实现 LLMProvider 四方法契约。

    不继承 LLMProvider（避免 ABC 约束 + base 默认实现干扰），但对外暴露同名四方法，
    调用方(get_llm_provider 返回它)透明使用。tier 在构造时定（一次问答一个实例）。
    """

    def __init__(self, providers: list, names: list[str], tier: str = "plus"):
        self._providers = providers
        self._names = names
        self._tier = tier
        self.last_used_name = names[0] if names else "unknown"  # 实际命中的 provider（切备后更新，供 trace mark）
        self.degraded = False       # 是否没用上第一顺位 provider（前端据此展示"备用模型"提示）
        self.degrade_reason = ""    # 切换前最后一次失败原因（前端 badge title）

    def _tier_model(self, idx: int) -> str | None:
        """档位 → 该 provider 的 model override（None=用 provider 默认）。"""
        try:
            from app.services.config_service import rt_tier_models
            tier_models = rt_tier_models() or {}
        except Exception:
            return None
        name = self._names[idx] if idx < len(self._names) else None
        if name and isinstance(tier_models.get(name), dict):
            return tier_models[name].get(self._tier)
        return None

    @staticmethod
    def _is_empty(method: str, res) -> bool:
        if method == "chat":
            return not (res or "").strip()
        if method == "chat_with_usage":
            return not (res[0] or "").strip()
        return False  # chat_with_tools：纯 tool_calls 也算成功

    @staticmethod
    def _fb_metric(frm: str, to: str, reason: str) -> None:
        try:
            from app.core import metrics
            metrics.LLM_FALLBACK_TOTAL.labels(frm, to, reason).inc()
        except Exception:
            pass

    async def _try_providers(self, method: str, check_empty: bool, **kw):
        """非流式：按链试，异常/空输出→切备。切备成功后置 degraded/degrade_reason，
        供上层（qa_service）透出"本次答案由备用/本地模型生成"提示给前端。"""
        last_exc: Exception | None = None
        last_res = None
        last_reason = ""
        for i, prov in enumerate(self._providers):
            name = self._names[i]
            nxt = self._names[i + 1] if i + 1 < len(self._names) else "-"
            model = self._tier_model(i)
            try:
                if method == "chat":
                    res = await prov.chat(model=model, **kw)
                elif method == "chat_with_usage":
                    res = await prov.chat_with_usage(model=model, **kw)
                else:
                    res = await prov.chat_with_tools(model=model, **kw)
                if check_empty and settings.LLM_FALLBACK_ON_EMPTY and self._is_empty(method, res):
                    record_fail(name)
                    self._fb_metric(name, nxt, "empty")
                    degraded("llm_fallback", Exception(f"{name} 空输出"), f"{method} 切备 {nxt}")
                    last_res = res
                    last_reason = f"{name} 返回空输出，已切换至 {nxt}"
                    continue
                record_ok(name)
                self.last_used_name = name
                self.degraded = i > 0
                self.degrade_reason = (
                    "云端模型全部不可用，已使用本地应急模型" if (self.degraded and name == "ollama")
                    else (last_reason if self.degraded else "")
                )
                return res
            except Exception as e:
                last_exc = e
                record_fail(name)
                self._fb_metric(name, nxt, "exception")
                degraded("llm_fallback", e, f"{name} {method} 异常切备 {nxt}")
                last_reason = f"{name} 不可用({type(e).__name__})，已切换至 {nxt}"
        if last_exc is not None:
            raise last_exc
        return last_res  # 全空：返回最后空结果（上层走兜底）

    async def chat(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> str:
        return await self._try_providers("chat", True, messages=messages,
                                         temperature=temperature, max_tokens=max_tokens, **kw)

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        return await self._try_providers("chat_with_usage", True, messages=messages,
                                         temperature=temperature, max_tokens=max_tokens, **kw)

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2,
                              max_tokens=2048, model=None, **kw):
        return await self._try_providers("chat_with_tools", False, messages=messages, tools=tools,
                                         tool_choice=tool_choice, temperature=temperature,
                                         max_tokens=max_tokens, **kw)

    async def stream(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        """流式：首 token 前切备（启动异常/空流→切下家）；首 token 后不切（避免重复吐字）。
        首 token 出现时置 degraded/degrade_reason（同 _try_providers 语义）。"""
        last_exc: Exception | None = None
        last_reason = ""
        for i, prov in enumerate(self._providers):
            name = self._names[i]
            nxt = self._names[i + 1] if i + 1 < len(self._names) else "-"
            tier_model = self._tier_model(i)
            yielded = False
            try:
                gen = prov.stream(messages, temperature=temperature, max_tokens=max_tokens,
                                  model=tier_model, **kw)
                async for tok in gen:
                    if not yielded:
                        record_ok(name)
                        self.last_used_name = name
                        self.degraded = i > 0
                        self.degrade_reason = (
                            "云端模型全部不可用，已使用本地应急模型" if (self.degraded and name == "ollama")
                            else (last_reason if self.degraded else "")
                        )
                    yielded = True
                    yield tok
                if yielded:
                    return  # 正常结束
                # 空流（首 token 都没出）
                if settings.LLM_FALLBACK_ON_EMPTY:
                    record_fail(name)
                    self._fb_metric(name, nxt, "empty_stream")
                    degraded("llm_fallback_stream", Exception(f"{name} 空流"), f"切备 {nxt}")
                    last_reason = f"{name} 空流，已切换至 {nxt}"
                    continue
                return
            except Exception as e:
                if yielded:
                    raise  # 已吐 token，中途异常不切（避免重复），抛给上层 degraded
                last_exc = e
                record_fail(name)
                self._fb_metric(name, nxt, "stream_exception")
                degraded("llm_fallback_stream", e, f"{name} 流启动失败切备 {nxt}")
                last_reason = f"{name} 不可用({type(e).__name__})，已切换至 {nxt}"
        if last_exc is not None:
            raise last_exc


def _should_actively_probe(name: str) -> bool:
    """本地应急模型(ollama)不参与周期主动探活——避免平时空闲时每个探活周期都被迫做一次
    CPU 推理。它的健康状态完全靠真实调用失败时的被动熔断(record_fail/record_ok)判定。"""
    return name != "ollama"


# ===== L1 后台探活 loop（克隆 main._refresh_component_health_loop 范式）=====
async def _refresh_llm_health_loop() -> None:
    """周期探活 fallback 链内各 provider → 写 LLM_PROVIDER_HEALTH Gauge + 熔断状态。

    探活用 factory.check_llm_health（原始 provider，不包 fallback，避免掩盖故障）。
    """
    if not getattr(settings, "LLM_HEALTH_PROBE_ENABLE", False):
        return
    await asyncio.sleep(15)  # 启动后等 15s 让其他组件就绪
    while True:
        try:
            from app.providers.factory import check_llm_health
            from app.core import metrics
            for p in _fallback_chain():
                if not _should_actively_probe(p):
                    continue
                try:
                    res = await check_llm_health(p)
                    ok = res.get("status") == "ok"
                    try:
                        metrics.LLM_PROVIDER_HEALTH.labels(p).set(1 if ok else 0)
                    except Exception:
                        pass
                    if ok:
                        record_ok(p)
                    else:
                        record_fail(p)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(settings.LLM_PROBE_INTERVAL)
