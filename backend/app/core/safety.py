"""安全合规：入站 prompt injection 防护 + 出站敏感信息脱敏 + 高风险操作识别（D4）。

电网运维是强监管行业：查询注入可能诱导 LLM 绕过规程编造危险操作，答案泄漏内部信息。
- detect_injection：识别常见越狱/指令注入模式（命中计数 + 告警，保守不误杀技术问题）
- mask_pii：答案敏感信息脱敏（手机/身份证/密码，PII_MASK_ENABLE 开启）
- extract_high_risk：提取答案中的高风险操作词，供前端风险 badge 展示
"""
import re

from app.config import settings

# 入站注入模式（中英文常见越狱 / 指令注入 / 脚本）
_INJECTION_PATTERNS = [
    r"忽略\s*(以上|上文|前面|上述|上面)\s*.{0,8}(指令|规则|要求|提示|设定)",
    r"ignore\s+.{0,20}instructions",
    r"disregard\s+.{0,20}(rules|instructions)",
    r"你\s*(现在|从现在起|必须|要)?\s*(是|扮演|充当|进入)\s*(一个)?\s*(DAN|越狱|无限制|开发者模式|jailbreak)",
    r"<\s*script[^>]*>",
    r"\bsystem\s*[:：]\s*",
    r"\b(DAN|jailbreak|越狱模式)\b",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

# 出站脱敏模式
_PII_PATTERNS = [
    (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{15}(?!\d)|(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证]"),
    (re.compile(r"(?i)(password|passwd|密码|口令)\s*[:：=]\s*\S+"), "密码=[已脱敏]"),
]


def detect_injection(text: str) -> tuple[bool, str]:
    """检测 prompt injection。返回 (是否疑似命中, 命中片段)。空串不报。"""
    if not text:
        return False, ""
    m = _INJECTION_RE.search(text)
    if m:
        return True, m.group(0)
    return False, ""


def mask_pii(text: str) -> str:
    """答案敏感信息脱敏（PII_MASK_ENABLE 开启时由 qa 后处理调用）。"""
    if not text:
        return text
    for pat, rep in _PII_PATTERNS:
        text = pat.sub(rep, text)
    return text


def extract_high_risk(answer: str) -> list[str]:
    """提取答案中出现的高风险操作关键词（前端展示风险 badge，安全前置提示）。"""
    if not answer:
        return []
    kws = [k.strip() for k in (getattr(settings, "HIGH_RISK_KEYWORDS", "") or "").split(",") if k.strip()]
    return sorted({k for k in kws if k and k in answer})


def safe_answer(answer: str) -> str:
    """答案安全后处理：按开关脱敏（入口在 qa_service 生成答案后调用）。"""
    if getattr(settings, "PII_MASK_ENABLE", False):
        return mask_pii(answer)
    return answer


def guard_query(text: str) -> None:
    """入站 prompt injection 告警（计数 + 日志，不阻断，避免误杀正常技术问题）。

    保守策略：仅记录可疑请求到 SAFETY_BLOCK 指标（Grafana 可见）+ 警告日志，
    不直接拒绝——电网技术问题里也可能含"忽略某步"等表述，硬拦会误伤。
    """
    if not getattr(settings, "SAFETY_FILTER_ENABLE", False) or not text:
        return
    flagged, hit = detect_injection(text)
    if not flagged:
        return
    try:
        from app.core import metrics
        metrics.SAFETY_BLOCK.labels("injection").inc()
    except Exception:
        pass
    try:
        from loguru import logger
        logger.warning(f"[安全:prompt_injection] 命中 {hit} | query={(text or '')[:60]}")
    except Exception:
        pass
