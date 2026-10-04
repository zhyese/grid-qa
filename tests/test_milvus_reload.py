"""Milvus 写路径 load 状态回归。

真实故障：Milvus 重启 / compaction 会 release 集合，此时 delete/query 报
`collection not loaded` —— 表现为"重建向量、删除文档全部失败"。
search 原本有 not-loaded 重载重试，写路径（delete/count/insert）曾漏掉，
这里锁住 `_run_with_reload` 的行为。
"""
import pytest

from app.clients import milvus_client as mc


class _Col:
    """可记录的假 Collection。"""

    def __init__(self):
        self.expr = None

    def delete(self, expr):
        self.expr = expr


def test_returns_directly_when_op_succeeds(monkeypatch):
    loads = {"n": 0}

    def fake_get_col(name):
        loads["n"] += 1
        return _Col()

    monkeypatch.setattr(mc, "_get_col", fake_get_col)
    assert mc._run_with_reload("c", lambda col: "ok") == "ok"
    assert loads["n"] == 1          # 成功路径只取一次集合，无额外重载


def test_retries_once_after_not_loaded(monkeypatch):
    state = {"loads": 0, "ops": 0}

    def fake_get_col(name):
        state["loads"] += 1
        return _Col()

    def op(col):
        state["ops"] += 1
        if state["ops"] == 1:
            raise Exception("collection not loaded[collection=467739907263250627]")
        return "done"

    monkeypatch.setattr(mc, "_get_col", fake_get_col)
    monkeypatch.setattr(mc, "_col_cache", {"c": object()})
    assert mc._run_with_reload("c", op) == "done"
    assert state["ops"] == 2
    assert state["loads"] == 2      # 必须丢弃缓存对象后重建（重建才会触发 load）
    assert "c" not in mc._col_cache


def test_does_not_retry_on_unrelated_error(monkeypatch):
    calls = {"n": 0}

    def op(col):
        calls["n"] += 1
        raise ValueError("boom")

    monkeypatch.setattr(mc, "_get_col", lambda name: _Col())
    with pytest.raises(ValueError):
        mc._run_with_reload("c", op)
    assert calls["n"] == 1          # 非 load 相关错误直接抛出，不掩盖


def test_delete_by_doc_uses_reload_path(monkeypatch):
    captured = []

    def fake_run(name, op):
        captured.append((name, op))
        return None

    monkeypatch.setattr(mc, "_connect", lambda: None)
    monkeypatch.setattr(mc, "document_collections", lambda: (("grid_chunks", 1024),))
    monkeypatch.setattr(mc.utility, "has_collection", lambda n: True)
    monkeypatch.setattr(mc, "_run_with_reload", fake_run)

    mc.delete_by_doc("d1")

    assert len(captured) == 1
    name, op = captured[0]
    assert name == "grid_chunks"
    col = _Col()
    op(col)
    assert col.expr == 'doc_id == "d1"'


def test_delete_by_doc_skips_missing_collection(monkeypatch):
    called = []
    monkeypatch.setattr(mc, "_connect", lambda: None)
    monkeypatch.setattr(mc, "document_collections", lambda: (("grid_chunks", 1024),))
    monkeypatch.setattr(mc.utility, "has_collection", lambda n: False)
    monkeypatch.setattr(mc, "_run_with_reload", lambda n, op: called.append(n))

    mc.delete_by_doc("d1")
    assert called == []             # 集合不存在 → 不触碰 Milvus
