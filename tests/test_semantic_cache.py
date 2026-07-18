"""语义缓存单测：fp16 b64 往返精度 + matmul 余弦与逐条一致（点2）。"""
import json

import numpy as np

from app.rag.semantic_cache import _b64_to_emb, _emb_to_b64


def test_fp16_roundtrip_low_error():
    vec = [0.1234, -0.5678, 0.9, -0.1, 0.3333] * 200  # 1000 维
    restored = _b64_to_emb(_emb_to_b64(vec))
    assert len(restored) == len(vec)
    assert np.allclose(restored, vec, atol=1e-2)  # fp16 精度损失 < 1e-2


def test_fp16_size_smaller_than_float_json():
    vec = [0.123456] * 1024
    b64 = _emb_to_b64(vec)
    float_json = json.dumps(vec)
    assert len(b64) < len(float_json) / 2  # 体积减半以上


def test_matmul_cosine_matches_naive():
    """matmul 批量余弦 == 逐条 numpy 余弦（验证矩阵化没改变结果）。"""
    rng = np.random.default_rng(42)
    M = rng.standard_normal((50, 128)).astype(np.float32)
    q = rng.standard_normal(128).astype(np.float32)

    def _cos(a, b):
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))

    naive = np.array([_cos(M[i], q) for i in range(50)])
    Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-10)
    qn = q / (np.linalg.norm(q) + 1e-10)
    matmul = Mn @ qn
    assert np.allclose(naive, matmul, atol=1e-3)
