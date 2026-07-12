"""插件 / 扩展框架（BRD §5.3.1 插件化扩展）。

机制：PluginRegistry 维护 3 类 hook，qa 主链路在边界点调用 run_hook 串行执行已启用插件。
- query_preprocess(query, ctx) → query：query 预处理（在 qa 路由入口）。
- retrieval_filter(items, ctx) → items：检索结果过滤（预留，retrieval 内部按需调）。
- answer_postprocess(answer, ctx) → answer：答案后处理（在 qa 路由返回前）。

内置插件：
- safety_banner（answer_postprocess）：高风险操作答案追加安全提示横幅。
- length_guard（query_preprocess）：超长 query 截断，防滥用。

新增能力 = 写一个函数 + register()，无需改主链路代码（扩展点已就位）。
"""
from typing import Callable

HOOKS = ("query_preprocess", "retrieval_filter", "answer_postprocess")

# _plugins: name -> {"enabled": bool, "desc": str, hooks: {hook: fn}}
_plugins: dict[str, dict] = {}


def register(name: str, desc: str, hooks: dict[str, Callable], enabled: bool = True) -> None:
    """注册一个插件。hooks: {hook_name: fn(value, ctx)->value}。"""
    if any(h not in HOOKS for h in hooks):
        raise ValueError(f"非法 hook，合法：{HOOKS}")
    _plugins[name] = {"enabled": enabled, "desc": desc, "hooks": hooks}


def unregister(name: str) -> None:
    _plugins.pop(name, None)


def enable(name: str) -> bool:
    if name in _plugins:
        _plugins[name]["enabled"] = True
        return True
    return False


def disable(name: str) -> bool:
    if name in _plugins:
        _plugins[name]["enabled"] = False
        return True
    return False


def list_plugins() -> list[dict]:
    return [{"name": n, "enabled": p["enabled"], "desc": p["desc"], "hooks": list(p["hooks"])}
            for n, p in _plugins.items()]


def run_hook(hook: str, value, ctx: dict | None = None):
    """串行执行所有已启用插件的该 hook。value 链式传递。异常降级（不中断主链路）。"""
    ctx = ctx or {}
    for name, p in _plugins.items():
        if not p["enabled"]:
            continue
        fn = p["hooks"].get(hook)
        if not fn:
            continue
        try:
            value = fn(value, ctx)
        except Exception as e:
            from app.core.obs import degraded
            degraded(f"plugin_{name}_{hook}", e)
    return value


# ===== 内置插件 =====

_HIGH_RISK = ("停电", "送电", "带电", "接地线", "倒闸", "检修", "安全距离", "放电")


def _safety_banner(answer: str, ctx: dict) -> str:
    if not answer:
        return answer
    if "安全提示" in answer:
        return answer
    if any(k in answer for k in _HIGH_RISK):
        return answer.rstrip() + "\n\n⚠ 安全提示：操作前核对调度指令与安规，做好停电验电接地。"
    return answer


def _length_guard(query: str, ctx: dict) -> str:
    if isinstance(query, str) and len(query) > 500:
        return query[:500]
    return query


register("safety_banner", "高风险答案追加安全提示横幅", {"answer_postprocess": _safety_banner}, enabled=True)
register("length_guard", "超长 query(>500)截断防滥用", {"query_preprocess": _length_guard}, enabled=True)
