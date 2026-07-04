"""安全合规：入站 prompt injection 防护 + 电网安全关键词监测 + 出站敏感信息脱敏 + 高风险操作识别（D4）。

电网运维是强监管行业：查询注入可能诱导 LLM 绕过规程编造危险操作，答案泄漏内部信息。
- detect_injection：识别常见越狱/指令注入模式（命中计数 + 告警，保守不误杀技术问题）
- scan_grid_hazards：按分类检测电网危险操作关键词（接地/放电/带电/短路/误操作等 8 类）
- mask_pii：答案敏感信息脱敏（手机/身份证/密码，PII_MASK_ENABLE 开启）
- extract_high_risk：提取答案中的高风险操作词，供前端风险 badge 展示
"""
import re
from typing import Dict, List, Tuple

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

# ===== 电网安全关键词分类（8 类危险操作维度） =====
# 每类包含：关键词列表 + 严重程度(critical/warning/info)
_GRID_HAZARD_CATEGORIES: Dict[str, Dict] = {
    "接地安全": {
        "severity": "critical",
        "patterns": [
            "接地线拆除", "接地保护退出", "未经验电即挂接地线", "接地电阻超标",
            "接地装置腐蚀严重", "接地网断开", "接地引下线断裂", "接地刀闸合闸不到位",
            "无接地保护运行", "接地短路", "工作接地不可靠",
        ],
    },
    "放电": {
        "severity": "critical",
        "patterns": [
            "局部放电", "电弧放电", "闪络放电", "电晕放电", "沿面放电",
            "操作过电压放电", "雷电过电压放电", "绝缘击穿放电",
            "触头放电痕迹", "套管表面放电", "GIS内部放电", "变压器内部放电",
        ],
    },
    "带电作业": {
        "severity": "critical",
        "patterns": [
            "带电搭接", "带电更换", "带电清扫", "带电断接引线",
            "等电位作业", "地电位作业", "中间电位作业", "带电水冲洗",
            "带电检测零值绝缘子", "未使用绝缘遮蔽", "安全距离不足带电",
        ],
    },
    "短路": {
        "severity": "critical",
        "patterns": [
            "相间短路", "单相接地短路", "两相短路", "三相短路",
            "母线短路", "出口短路", "近区短路", "匝间短路",
            "短路冲击电流", "短路容量超标", "短路热稳定不满足",
        ],
    },
    "误操作": {
        "severity": "warning",
        "patterns": [
            "带负荷拉合隔离开关", "带地线合闸", "误入带电间隔",
            "误拉合断路器", "误动保护压板", "误整定保护定值",
            "走错间隔", "误碰运行设备", "误投退保护", "误操作五防失效",
        ],
    },
    "倒闸操作": {
        "severity": "warning",
        "patterns": [
            "倒闸操作无人监护", "操作票跳项", "操作票漏项",
            "不执行唱票复诵", "解锁操作未审批", "操作前未核对设备双重编号",
            "操作后未检查设备位置", "事故处理不按操作票",
        ],
    },
    "安全措施": {
        "severity": "warning",
        "patterns": [
            "安全措施未执行", "工作票未许可即开工", "未挂接地线",
            "未设安全围栏", "未挂标识牌", "未断开二次回路",
            "未做安全交底", "危险点未分析", "未穿绝缘靴", "未戴绝缘手套",
            "验电器未检验", "绝缘工器具超期",
        ],
    },
    "设备异常": {
        "severity": "info",
        "patterns": [
            "变压器重瓦斯动作", "变压器差动保护动作", "断路器SF6压力低闭锁",
            "隔离开关触头发热", "CT二次开路", "PT二次短路",
            "避雷器爆炸", "电容器鼓肚", "电缆头爆炸", "绝缘油色谱异常",
            "有载分接开关拒动", "保护装置异常告警",
        ],
    },
}

# 预编译分类正则
_CATEGORY_RES = {
    cat: re.compile("|".join(info["patterns"]))
    for cat, info in _GRID_HAZARD_CATEGORIES.items()
}

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


def scan_grid_hazards(text: str) -> List[Dict]:
    """按分类扫描文本中的电网危险操作关键词。

    返回每类命中详情：[{category, severity, hit_count, matches, sample}]
    同时累加 Prometheus 指标 SAFETY_KEYWORD{category} 用于 Grafana 告警。
    """
    if not text:
        return []
    results = []
    for cat, cre in _CATEGORY_RES.items():
        matches = cre.findall(text)
        if matches:
            info = _GRID_HAZARD_CATEGORIES[cat]
            results.append({
                "category": cat,
                "severity": info["severity"],
                "hit_count": len(matches),
                "matches": matches[:5],       # 最多 5 条样例
                "sample": text[:200],
            })
            # Prometheus 打点
            try:
                from app.core import metrics
                metrics.SAFETY_KEYWORD.labels(cat).inc(len(matches))
            except Exception:
                pass
    return results


def get_hazard_categories() -> Dict:
    """返回所有注册的危险操作类别及严重程度（供前端/API 查询）。"""
    return {
        cat: {"severity": info["severity"], "pattern_count": len(info["patterns"])}
        for cat, info in _GRID_HAZARD_CATEGORIES.items()
    }


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
    # 优先用 .env 自定义关键词；若未配置则用内置的电网安全关键词
    kws = [k.strip() for k in (getattr(settings, "HIGH_RISK_KEYWORDS", "") or "").split(",") if k.strip()]
    if not kws:
        # fallback：合并所有内置危险类别关键词
        for info in _GRID_HAZARD_CATEGORIES.values():
            kws.extend(info["patterns"])
        # 去重
        kws = list(set(kws))
    return sorted({k for k in kws if k and k in answer})


def safe_answer(answer: str) -> str:
    """答案安全后处理：按开关脱敏（入口在 qa_service 生成答案后调用）。"""
    if getattr(settings, "PII_MASK_ENABLE", False):
        return mask_pii(answer)
    return answer


def guard_query(text: str) -> None:
    """入站 prompt injection + 电网危险操作 告警（计数 + 日志，不阻断，避免误杀）。

    保守策略：仅记录可疑请求到 SAFETY_BLOCK / SAFETY_KEYWORD 指标（Grafana 可见）+
    警告日志，不直接拒绝——电网技术问题本身也可能含"接地""放电"等术语，硬拦会误伤。
    """
    if not getattr(settings, "SAFETY_FILTER_ENABLE", False) or not text:
        return

    # prompt injection
    flagged, hit = detect_injection(text)
    if flagged:
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

    # 电网危险操作关键词扫描
    hazards = scan_grid_hazards(text)
    if hazards:
        try:
            from loguru import logger
            cats = [f"{h['category']}({h['hit_count']})" for h in hazards]
            logger.warning(f"[安全:grid_hazard] {'; '.join(cats)} | query={(text or '')[:80]}")
        except Exception:
            pass
