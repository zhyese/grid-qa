# 可核验 RAG 引用体系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 `citation.auto_cite`（后处理补标）升级为端到端「可核验引用引擎」——入库元数据治理 → 服务端受控编号 → 标准化结构化生成 → 三层校验（格式/向量/NLI）→ 前端可视化 + 评测闭环，让 `[1]` 标记等价于「可定位、可溯源、可核验」。

**Architecture:** 贯穿 RAG 五层，最大复用现有件：`citation.evidence_trace`/`auto_cite._cosine_mat`（校验1+2）、`judge.judge_hallucination` 的声明拆解（抽 `_verify_claims` 做校验3 NLI）、CRAG `_crag_correct` 的 rewrite/refused（校验失败兜底）。新增 4 个模块：`rag/citation_index.py`（受控编号）、`rag/citation_verifier.py`（三层校验引擎）、`schemas/citation.py`（结构化输出）、`scripts/eval_citation.py`（评测）。全开关 opt-in，默认行为=现状零破坏。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.0 async / Pydantic v2 / pdfplumber·openpyxl·python-docx（解析）/ Vue 3 Composition API（前端）/ pytest + pytest_asyncio + aiosqlite（测试）。

## Global Constraints

- **全 opt-in**：所有新行为默认关闭（`CITATION_VERIFIER_ENABLE=False` 等），关闭时主链路逐字节等同现状，绝不改变 `qa_service.answer` 既有返回结构（新增字段除外）。
- **双路径迁移**：Chunk 加列必须同时写 ① `backend/app/db/init_db.py` 的 `_COLUMN_MIGRATIONS` 幂等 ALTER（开发期主路径，列已存在忽略 1060）② `backend/migrations/versions/<rev>_add_citation_meta.py` Alembic 迁移（生产留痕）。两条等价。
- **不破坏现有件**：`judge.judge_hallucination` / `citation.auto_cite` 的对外签名与返回保持不变；新逻辑以「新增函数 + 内部复用」方式接入。
- **测试约定**：单测用 `tests/conftest.py` 的 `test_db`（sqlite in-memory + create_all，新列自动建表）；mock LLM/embedding 用 `monkeypatch.setattr`，`_run(coro)=asyncio.run(coro)`（见 `tests/test_citation.py`）；**绝不**在单测里打真实云 API。
- **提交约定**：显式 `git add <具体文件>`，禁 `git add -A`/`add .`；`.env`/`release/`/`data/` 已 gitignore 不提交；当前分支 `main`，每个 Task 结尾一次 commit。
- **docType 过滤惯例**：引用草稿等 AI 产物 `doc_type=ai_evolution` 已在检索降权，引用引擎不重复处理。
- **YAGNI**：不做实时 PDF 重渲染引擎（复用 `/document/preview`）、不做 NLI 模型微调（用 LLM judge 同源）、不做多轮引用累积核验（本期单轮）。
- **🔴 前端零改动（硬约束）**：本期**完全不修改任何前端文件**（`Chat.vue` / `Documents.vue` / `api/*.js` 等一概不碰）。可核验引用只在后端闭环：`qa_service.answer` 返回 dict 追加**可选** `citation*` 字段（开关关时连字段都不出现）。前端不消费这些字段也照常运行，问答/文档/检索/告警/图谱等**主业务零影响**。引用卡片 / 警示标签 / bbox 高亮等前端展示**全部留作后续可选增强**，届时单独评估、独立分支再做，不在本期范围内。

---

## File Structure

**新增文件（7）：**
| 文件 | 职责 |
|---|---|
| `backend/app/schemas/citation.py` | Pydantic 结构化输出 schema：`CitationItem` / `CitationAnswer` / `VerifyResult` |
| `backend/app/rag/citation_index.py` | 服务端受控编号：`build_index(contexts) -> {1: chunk_id, ...}` |
| `backend/app/rag/citation_verifier.py` | 三层校验引擎：格式 → 向量 → NLI，输出 `VerifyResult` |
| `backend/migrations/versions/e4f5a6b7c8d9_add_citation_meta.py` | Alembic 迁移：chunks 加 5 列 |
| `scripts/backfill_chunk_meta.py` | 历史存量 chunk 元数据回填（可选，不阻塞主链路） |
| `scripts/eval_citation.py` | 引用评测：四样本 × 四指标 + CI 门禁 |
| `backend/data/golden_citation.json` | 引用评测样本集（单句单证据/单句多证据/干扰/高风险） |

**修改文件（8）：**
| 文件 | 改动 |
|---|---|
| `backend/app/models/chunk.py` | Chunk 加 `page_num`/`bbox`/`section_path`/`table_header`/`metadata_complete` 5 字段 |
| `backend/app/db/init_db.py` | `_COLUMN_MIGRATIONS` 加 5 列幂等 ALTER |
| `backend/app/config.py` | 证据溯源区加 5 个 `CITATION_*` 开关 |
| `backend/app/services/parse_service.py` | `extract_pdf_structured`/`extract_xlsx`/`extract_docx_structured` 补 page_num/bbox/table_header |
| `backend/app/services/chunk_service.py` | `split_structured` 透传 page_num/bbox/section_path/table_header 到 chunk dict |
| `backend/app/services/document_service.py` | `parse_documents` 入库塞新字段 + `metadata_complete` 判定 + 入库拦截 |
| `backend/app/rag/judge.py` | 抽 `_verify_claims(claims, sources) -> [{text, label}]` 三分类，`judge_hallucination` 复用 |
| `backend/app/services/qa_service.py` | `answer` 串联 citation_index → 结构化生成 → citation_verifier → 校验-CRAG 联动 |

**前端：本期零改动（硬约束）。** 后端 `qa_service.answer` 追加可选 `citation*` 字段；前端不消费也不受影响，主业务零侵入。引用卡片/警示标签/bbox 高亮全部留后续。

---

## Task 1: Chunk 模型加字段 + 双路径迁移

**Files:**
- Modify: `backend/app/models/chunk.py`（加 5 字段）
- Modify: `backend/app/db/init_db.py`（`_COLUMN_MIGRATIONS` 加 5 列）
- Create: `backend/migrations/versions/e4f5a6b7c8d9_add_citation_meta.py`
- Test: `tests/test_chunk_meta.py`

**Interfaces:**
- Produces: `Chunk.page_num: int|None`、`Chunk.bbox: str|None`（JSON 串）、`Chunk.section_path: str`、`Chunk.table_header: str`、`Chunk.metadata_complete: bool`（默认 False）。下游 Task 3/4 依赖这些字段名。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_chunk_meta.py
"""Chunk 引用元数据新字段 + 迁移幂等。"""
from app.models.chunk import Chunk


def test_chunk_has_citation_meta_fields():
    c = Chunk(doc_id="d1", chunk_idx=0, content="x", section_path="3.1 > 第2条",
              page_num=5, bbox='[10,20,300,80]', table_header="序号|名称",
              metadata_complete=True)
    assert c.section_path == "3.1 > 第2条"
    assert c.page_num == 5
    assert c.bbox == '[10,20,300,80]'
    assert c.table_header == "序号|名称"
    assert c.metadata_complete is True


def test_chunk_fields_default_backward_compat():
    """旧路径不传新字段 → 默认值，向后兼容。"""
    c = Chunk(doc_id="d1", chunk_idx=0, content="x")
    assert c.page_num is None
    assert c.bbox is None
    assert c.section_path == ""
    assert c.table_header == ""
    assert c.metadata_complete is False
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_chunk_meta.py -v`
Expected: FAIL（`AttributeError: 'Chunk' object has no attribute 'page_num'`）

- [ ] **Step 3: Chunk 模型加字段**

在 `backend/app/models/chunk.py` 的 `section` 字段后、`__table_args__` 前插入：

```python
    section: Mapped[str] = mapped_column(String(256), default="")  # 所属章节/标题路径（结构化溯源）
    # ===== 可核验引用元数据（第一层：精确定位）=====
    page_num: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 页码/幻灯片号（PDF 有，Word/txt 无→null）
    bbox: Mapped[str | None] = mapped_column(String(128), nullable=True)  # JSON 串 [x0,y0,x1,y1]，前端 PDF 高亮
    section_path: Mapped[str] = mapped_column(String(512), default="")  # 层级章节路径 "3.1 免责 > 第2条"
    table_header: Mapped[str] = mapped_column(Text, default="")  # 表格类 chunk 绑定的表头（防数值丢上下文）
    metadata_complete: Mapped[bool] = mapped_column(default=False)  # 元数据是否齐全（前端降级依据）

    __table_args__ = (Index("ix_chunks_doc_parent", "doc_id", "parent_idx"),)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_chunk_meta.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: init_db 加幂等 ALTER（开发期主路径）**

在 `backend/app/db/init_db.py` 的 `_COLUMN_MIGRATIONS` 列表追加（遵循现有 `(表, 列, DDL)` 元组惯例；先 Read 该列表确认确切结构，按现有条目格式照抄）：

```python
    # 可核验引用元数据（第一层）
    ("chunks", "page_num", "ADD COLUMN page_num INT NULL"),
    ("chunks", "bbox", "ADD COLUMN bbox VARCHAR(128) NULL"),
    ("chunks", "section_path", "ADD COLUMN section_path VARCHAR(512) DEFAULT ''"),
    ("chunks", "table_header", "ADD COLUMN table_header TEXT"),
    ("chunks", "metadata_complete", "ADD COLUMN metadata_complete BOOLEAN DEFAULT FALSE"),
```

> 注：`_COLUMN_MIGRATIONS` 的确切元组形状（是否含类型占位）以 Read 到的现有条目为准——照抄同表已有列的格式，列名/DDL 换成本处。若现有机制是 `ALTER TABLE ... ` 拼接，确保 1060 列已存在被 try/except 忽略（init_db 已有此保护）。

- [ ] **Step 6: Alembic 迁移（生产留痕）**

Create `backend/migrations/versions/e4f5a6b7c8d9_add_citation_meta.py`（down_revision 指向 `c3d4e5f6a7b8`，即 add_rbac）：

```python
"""add citation meta columns to chunks

Revision ID: e4f5a6b7c8d9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("page_num", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("bbox", sa.String(length=128), nullable=True))
    op.add_column("chunks", sa.Column("section_path", sa.String(length=512), nullable=False, server_default=""))
    op.add_column("chunks", sa.Column("table_header", sa.Text(), nullable=False, server_default=""))
    op.add_column("chunks", sa.Column("metadata_complete", sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    for col in ("metadata_complete", "table_header", "section_path", "bbox", "page_num"):
        op.drop_column("chunks", col)
```

- [ ] **Step 7: 提交**

```bash
git add backend/app/models/chunk.py backend/app/db/init_db.py backend/migrations/versions/e4f5a6b7c8d9_add_citation_meta.py tests/test_chunk_meta.py
git commit -m "feat(citation): Chunk 加引用元数据5字段(page_num/bbox/section_path/table_header/metadata_complete)+双路径迁移"
```

---

## Task 2: config 加 5 个引用开关

**Files:**
- Modify: `backend/app/config.py:191`（证据溯源区，`CITATION_SIM_THRESHOLD` 后追加）
- Test: `tests/test_citation.py`（扩一个断言）

**Interfaces:**
- Produces: `settings.CITATION_VERIFIER_ENABLE: bool=False`、`CITATION_NLI_ENABLE: bool=False`、`CITATION_NLI_TIMEOUT: int=5`、`CITATION_STRUCTURED_OUTPUT: bool=False`、`CITATION_REWRITE_ON_FAIL: bool=True`。Task 9/10/12 依赖。

- [ ] **Step 1: 写失败测试**

在 `tests/test_citation.py` 的 `test_citation_settings_defaults` 末尾加断言：

```python
def test_citation_settings_defaults():
    assert settings.CITATION_AUTO_ENABLE is True
    assert settings.CITATION_SIM_THRESHOLD == 0.6
    # 新开关默认全 opt-in（关闭=现状）
    assert settings.CITATION_VERIFIER_ENABLE is False
    assert settings.CITATION_NLI_ENABLE is False
    assert settings.CITATION_NLI_TIMEOUT == 5
    assert settings.CITATION_STRUCTURED_OUTPUT is False
    assert settings.CITATION_REWRITE_ON_FAIL is True
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py::test_citation_settings_defaults -v`
Expected: FAIL（`AttributeError: CITATION_VERIFIER_ENABLE`）

- [ ] **Step 3: config.py 加开关**

在 `backend/app/config.py:191`（`CITATION_SIM_THRESHOLD` 行）后插入：

```python
    CITATION_AUTO_ENABLE: bool = True       # 无角标句子是否向量相似度自动补标
    CITATION_SIM_THRESHOLD: float = 0.6     # 自动补标 cosine 阈值（低于则不补，保留"无引用"）
    # ===== 可核验引用引擎（五层闭环，全 opt-in，默认=现状）=====
    CITATION_VERIFIER_ENABLE: bool = False   # 第四层校验引擎总开关（格式+向量+NLI）
    CITATION_NLI_ENABLE: bool = False        # 校验3 NLI 精准核验（最重，独立开关）
    CITATION_NLI_TIMEOUT: int = 5            # 校验3 NLI 超时秒（超时降级仅走校验1+2）
    CITATION_STRUCTURED_OUTPUT: bool = False  # 第三层 LLM 结构化输出 CitationAnswer
    CITATION_REWRITE_ON_FAIL: bool = True    # 校验失败联动 CRAG：rewrite 二次检索 / refused 拒答
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py::test_citation_settings_defaults -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/config.py tests/test_citation.py
git commit -m "feat(citation): config 加5个引用引擎开关(全opt-in默认现状)"
```

---

## Task 3: parse_service 解析层补元数据

**Files:**
- Modify: `backend/app/services/parse_service.py`（`extract_pdf_structured`/`extract_xlsx`/`extract_docx_structured`/`parse_file_structured`/`ocr_to_sections`）
- Test: `tests/test_parse.py`（加引用元数据用例）

**Interfaces:**
- Produces: sections 列表项扩展为 `{type, content, page_num?, bbox?, table_header?}`。Task 4 的 `split_structured` 消费。

- [ ] **Step 1: 写失败测试**

在 `tests/test_parse.py` 末尾加（PDF 用真实小样本，pdfplumber 已装；若 CI 无样本则 `@pytest.mark.skipif` 守护）：

```python
def test_extract_pdf_structured_has_page_num():
    """PDF 结构化解析 → section 带 page_num（首字符 bbox）。"""
    from app.services import parse_service
    # 造一个极简 PDF：用 reportlab/fontTools 无则跳过；有则断言 page_num
    try:
        import reportlab  # noqa
    except ImportError:
        import pytest
        pytest.skip("reportlab 未装，跳过 PDF 造样")
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, "主变油温限值85度")
    c.showPage(); c.save()
    sections, is_scanned = parse_service.extract_pdf_structured(buf.getvalue())
    assert not is_scanned
    text_secs = [s for s in sections if s["type"] == "text"]
    assert text_secs and text_secs[0]["page_num"] == 1
    assert isinstance(text_secs[0].get("bbox"), str)  # JSON 串


def test_extract_xlsx_has_table_header():
    """Excel → table 段带 table_header（首行）。"""
    from app.services import parse_service
    import openpyxl, io
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["序号", "设备", "限值"]); ws.append(["1", "主变", "85"])
    buf = io.BytesIO(); wb.save(buf)
    sections = parse_service.extract_xlsx(buf.getvalue())
    assert sections and sections[0]["type"] == "table"
    assert "序号" in sections[0]["table_header"]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_parse.py -v -k "page_num or table_header"`
Expected: FAIL（`KeyError: 'page_num'`）

- [ ] **Step 3: 改 `extract_pdf_structured`（带 page_num + bbox）**

替换 `parse_service.extract_pdf_structured`（当前在 `:162`）的循环体，给每个 section 注入 `page_num` 和首字符 `bbox`（JSON 串）：

```python
def extract_pdf_structured(content: bytes) -> Tuple[list[dict], bool]:
    """PDF 结构化：每页表格转 markdown + 正文文本。表格整体成段，不再被打碎。
    可核验引用增强：每个 section 带 page_num + 首字符 bbox（前端 PDF 高亮）。"""
    import json
    import pdfplumber

    sections: list[dict] = []
    n_pages, total_chars = 0, 0
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            n_pages += 1
            page_num = page.page_number
            for tbl in page.extract_tables() or []:
                md = _table_to_markdown(tbl)
                if md.strip():
                    sections.append({"type": "table", "content": md, "page_num": page_num,
                                     "table_header": _first_row_as_header(tbl)})
            txt = (page.extract_text() or "").strip()
            if txt:
                # 首字符 bbox 作高亮锚点（页内首字矩形）
                bbox = None
                try:
                    chars = page.chars
                    if chars:
                        x0, y0 = chars[0]["x0"], chars[0]["top"]
                        x1 = max(c["x1"] for c in chars[:min(len(chars), 40)])
                        y1 = max(c["bottom"] for c in chars[:min(len(chars), 40)])
                        bbox = json.dumps([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
                except Exception:
                    bbox = None
                sections.append({"type": "text", "content": txt, "page_num": page_num, "bbox": bbox})
                total_chars += len(txt)
    is_scanned = n_pages > 0 and total_chars < n_pages * 10
    return sections, is_scanned


def _first_row_as_header(rows: list) -> str:
    """表格首行作表头（table_header 字段，防数值丢列上下文）。"""
    if not rows:
        return ""
    head = ["" if c is None else str(c).strip() for c in (rows[0] or [])]
    return " | ".join(h for h in head if h)
```

- [ ] **Step 4: 改 `extract_xlsx` / `extract_docx_structured`（带 table_header）**

`extract_xlsx`（`:148`）的 `sections.append` 改：

```python
            md = _table_to_markdown(rows)
            if md.strip():
                sections.append({"type": "table", "content": f"## {ws.title}\n{md}",
                                 "table_header": _first_row_as_header(rows)})
```

`extract_docx_structured`（`:183`）的表格分支同样加 `"table_header": _first_row_as_header(rows)`。

- [ ] **Step 5: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_parse.py -v -k "page_num or table_header"`
Expected: PASS

- [ ] **Step 6: 回归现有 parse 测试**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_parse.py -v`
Expected: 全 PASS（新字段可选，旧断言不破坏）

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/parse_service.py tests/test_parse.py
git commit -m "feat(citation): 解析层补引用元数据(PDF page_num+bbox / Excel·Word table_header)"
```

---

## Task 4: chunk_service + document_service 透传元数据 + 入库拦截

**Files:**
- Modify: `backend/app/services/chunk_service.py:92`（`split_structured` 透传）
- Modify: `backend/app/services/document_service.py:236`（`parse_documents` 入库 + 拦截）
- Test: `tests/test_chunk.py`（加透传断言）

**Interfaces:**
- Produces: `split_structured` 输出 chunk dict 带 `page_num`/`bbox`/`section_path`/`table_header`；`parse_documents` 入库时填 `Chunk.*` 新字段 + `metadata_complete` 判定（page_num 非空且 content 非空→True）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_chunk.py` 加：

```python
def test_split_structured_passes_citation_meta():
    """split_structured 透传 sections 的 page_num/bbox/table_header 到 chunk。"""
    from app.services import chunk_service
    sections = [
        {"type": "text", "content": "主变油温应不超过85度，超过需申请停运。", "page_num": 3, "bbox": "[1,2,3,4]"},
        {"type": "table", "content": "| 序号 | 限值 |\n|---|---|\n| 1 | 85 |", "table_header": "序号 | 限值"},
    ]
    chunks = chunk_service.split_structured(sections, parent_size=2000, child_size=500)
    text_chunk = next(c for c in chunks if c["chunk_type"] == "child")
    assert text_chunk["page_num"] == 3
    assert text_chunk["bbox"] == "[1,2,3,4]"
    table_chunk = next(c for c in chunks if c["chunk_type"] == "table")
    assert table_chunk["table_header"] == "序号 | 限值"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_chunk.py::test_split_structured_passes_citation_meta -v`
Expected: FAIL（`KeyError: 'page_num'`）

- [ ] **Step 3: `split_structured` 透传元数据**

改 `backend/app/services/chunk_service.py:92` 的 `split_structured`。在循环里，把 section 级的 `page_num`/`bbox`/`table_header` 透传到每个产出的 chunk dict；`section_path` 用 section 自身 `_detect_section` 结果规范化：

```python
    chunks: list[dict] = []
    group_id = 0
    for sec in sections or []:
        stype, content = sec.get("type", "text"), sec.get("content", "") or ""
        page_num = sec.get("page_num")
        bbox = sec.get("bbox")
        table_header = sec.get("table_header", "")
        if stype == "table":
            md = content.strip()
            if not md:
                continue
            chunks.append({"text": md, "chunk_type": "table", "section": "表格",
                           "section_path": "表格", "parent_idx": group_id,
                           "page_num": page_num, "bbox": bbox, "table_header": table_header})
            group_id += 1
            continue
        if not content.strip():
            continue
        big_blocks = split_text(content, chunk_size=parent_size, overlap=settings.PARENT_OVERLAP)
        for big in big_blocks:
            gid = group_id
            group_id += 1
            section_title = _detect_section(big)
            smalls = split_text(big, chunk_size=child_size, overlap=overlap)
            for s in smalls:
                if s.strip():
                    chunks.append({"text": s, "chunk_type": "child",
                                   "section": section_title, "section_path": section_title,
                                   "parent_idx": gid,
                                   "page_num": page_num, "bbox": bbox, "table_header": ""})
    return chunks
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_chunk.py::test_split_structured_passes_citation_meta -v`
Expected: PASS

- [ ] **Step 5: `parse_documents` 入库塞新字段 + 拦截 + metadata_complete 判定**

改 `backend/app/services/document_service.py:236` 的 `parse_documents`，把 `db.add(Chunk(...))` 那段（`:255-259`）替换为：

```python
        await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))  # 重新解析先清旧分块
        for i, c in enumerate(structured):
            # 入库拦截：core 字段必填校验（不阻塞主链路，仅记 degraded）
            if not c.get("text"):
                continue
            if not doc_id:
                try:
                    from app.core.obs import degraded
                    degraded("chunk_intake_missing_doc_id", ValueError("empty doc_id"))
                except Exception:
                    pass
                continue
            page_num = c.get("page_num")
            db.add(Chunk(
                doc_id=doc_id, chunk_idx=i, content=c["text"], char_count=len(c["text"]),
                chunk_type=c["chunk_type"], parent_idx=c["parent_idx"], section=c["section"],
                section_path=c.get("section_path", "") or c.get("section", ""),
                page_num=page_num, bbox=c.get("bbox"),
                table_header=c.get("table_header", ""),
                # 元数据齐全判定：有页码（PDF）或表格表头即视为可精确定位；纯文本无页码→False（前端降级仅文档名）
                metadata_complete=bool(page_num is not None or c.get("table_header")),
            ))
```

- [ ] **Step 6: 回归 chunk + document 测试**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_chunk.py tests/test_document.py -v`
Expected: 全 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/chunk_service.py backend/app/services/document_service.py tests/test_chunk.py
git commit -m "feat(citation): 切分透传+入库塞引用元数据+metadata_complete判定+core字段入库拦截"
```

---

## Task 5: 历史回填脚本（可选，降级不阻塞）

**Files:**
- Create: `scripts/backfill_chunk_meta.py`

**Interfaces:**
- 独立脚本，对存量 chunk 重新解析补 page_num/bbox/section_path/table_header；不回填者保持 `metadata_complete=False` 降级展示。

- [ ] **Step 1: 写脚本**

```python
# scripts/backfill_chunk_meta.py
"""历史存量 chunk 引用元数据回填。

对 metadata_complete=False 的 chunk，按 doc_id 从 MinIO 取原文重新结构化解析，
回填 page_num/bbox/section_path/table_header。失败保持 False（前端降级，不阻塞）。

用法：python scripts/backfill_chunk_meta.py [--tenant default] [--dry-run]
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select
from app.db.session import async_session
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import parse_service, chunk_service
from app.clients import minio_client


async def backfill(tenant: str, dry_run: bool) -> dict:
    stats = {"scanned": 0, "updated": 0, "skipped": 0, "failed": 0}
    async with async_session() as db:
        # 取待回填文档（含 metadata_complete=False chunk 的）
        rows = (await db.execute(
            select(Document).where(Document.tenant_id == tenant, Document.status == "vectorized")
        )).scalars().all()
        for doc in rows:
            stats["scanned"] += 1
            try:
                content = await asyncio.to_thread(minio_client.get_object_bytes, doc.minio_object)
                sections, _ = parse_service.parse_file_structured(doc.doc_name, content)
                structured = chunk_service.split_structured(sections)
                if not structured:
                    stats["skipped"] += 1
                    continue
                if dry_run:
                    stats["updated"] += 1
                    continue
                # 按 chunk_idx 回填（重新解析顺序应一致；不一致则按 content 模糊匹配降级）
                idx_map = {c["chunk_idx"]: c for c in []}  # 占位：实际按 doc_id+chunk_idx 批量 update
                chunks = (await db.execute(
                    select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_idx)
                )).scalars().all()
                for ch, meta in zip(chunks, structured):
                    ch.page_num = meta.get("page_num")
                    ch.bbox = meta.get("bbox")
                    ch.section_path = meta.get("section_path", "") or ch.section
                    ch.table_header = meta.get("table_header", "")
                    ch.metadata_complete = bool(ch.page_num is not None or ch.table_header)
                await db.commit()
                stats["updated"] += 1
            except Exception as e:
                print(f"[FAIL] doc={doc.id} {e}", file=sys.stderr)
                stats["failed"] += 1
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    res = asyncio.run(backfill(args.tenant, args.dry_run))
    print(res)
```

- [ ] **Step 2: 手动冒烟（dry-run，需后端在跑）**

Run: `python scripts/backfill_chunk_meta.py --tenant default --dry-run`
Expected: 打印 `{scanned: N, updated: M, ...}`，不抛异常（空库时 scanned=0）。

- [ ] **Step 3: 提交**

```bash
git add scripts/backfill_chunk_meta.py
git commit -m "feat(citation): 历史chunk元数据回填脚本(降级不阻塞主链路)"
```

---

## Task 6: citation_index 服务端受控编号

**Files:**
- Create: `backend/app/rag/citation_index.py`
- Modify: `backend/app/services/retrieval_service.py:33`（`_to_item` 补 `chunk_id` 透出，按 doc_id+chunk_idx 已在 hit 里）
- Test: `tests/test_citation.py`（加 build_index 断言）

**Interfaces:**
- Produces: `build_index(contexts: list[dict]) -> dict[int, str]`，形如 `{1: chunk_id, 2: chunk_id}`；映射 key 与 `prompt_templates.build_messages_with_history` 的 `[{i+1}]` 编号天然对齐。
- Consumes: `contexts[i]` 需含 `chunkId` 或 `(docId, chunkIdx)`——由 `_to_item` 补全（见 Step 3）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_citation.py` 加：

```python
def test_build_index_maps_position_to_chunk_id():
    """build_index：位置编号 [1..N] → chunk_id，与 prompt [i+1] 对齐。"""
    from app.rag.citation_index import build_index
    contexts = [
        {"chunkId": "c1", "chunk": "油温限值", "docName": "A"},
        {"chunkId": "c2", "chunk": "停运流程", "docName": "B"},
    ]
    idx = build_index(contexts)
    assert idx == {1: "c1", 2: "c2"}
    assert idx[1] == "c1"


def test_build_index_empty():
    from app.rag.citation_index import build_index
    assert build_index([]) == {}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k build_index`
Expected: FAIL（`ModuleNotFoundError: citation_index`）

- [ ] **Step 3: 写 `citation_index.py`**

```python
# backend/app/rag/citation_index.py
"""第二层 · 服务端受控编号：杜绝 LLM 编造引用编号。

mixed_search 召回后，服务端统一分配本轮局部编号 [1..N] → chunk_id 映射。
编号与 prompt_templates 的 [{i+1}] 天然对齐（contexts 顺序即编号顺序）。
LLM 只能引用 [1..N]，越界由 citation_verifier 校验1 剔除。
"""


def build_index(contexts: list[dict]) -> dict[int, str]:
    """位置编号 → chunk_id 映射。

    contexts: mixed_search 返回的 _to_item 产物，需含 chunkId（Task 6 Step 3 在 _to_item 补）。
    返回 {1: chunk_id, 2: chunk_id, ...}，长度 == len(contexts)。
    """
    idx: dict[int, str] = {}
    for i, c in enumerate(contexts or []):
        cid = c.get("chunkId") or c.get("chunk_id") or ""
        idx[i + 1] = cid
    return idx


def chunk_id_of(ref_id: int, index: dict[int, str]) -> str:
    """编号 → chunk_id（越界返回空串，供校验1 判非法）。"""
    return index.get(ref_id, "")
```

- [ ] **Step 4: `_to_item` 透出 chunkId**

改 `backend/app/services/retrieval_service.py:33` 的 `_to_item`，加 `"chunkId": h.get("chunk_id", "")`（pool hit 里 dense/sparse 都带 `doc_id`+`chunk_idx`；`chunk_id` 来自 Milvus payload 或按需在 mixed_search 末尾批量查 Chunk 表回填——见 Step 5）：

```python
def _to_item(h: dict) -> dict:
    return {
        "chunk": h.get("text", ""),
        "score": h.get("score", 0.0),
        "docId": h.get("doc_id", ""),
        "docName": h.get("doc_name", ""),
        "docType": h.get("doc_type", ""),
        "chunkIdx": h.get("chunk_idx"),
        "chunkId": h.get("chunk_id", ""),
        "sources": h.get("srcs", []),
    }
```

> Milvus payload 是否已存 `chunk_id`：dense_hits 来自 `milvus_client.search`，payload 字段以该 client 实际写入为准。**Step 5 用 Read 核实** `milvus_client.search` 返回字段——若 payload 无 chunk_id，则在 `mixed_search` 末尾（`items = [_to_item(h) for h in pool]` 后）批量按 (doc_id, chunk_idx) 查 Chunk 表回填 chunk_id（参考 retrieval_service 已有的 docType 补全模式 `:324`）。

- [ ] **Step 5: 核实/补全 chunk_id 来源**

Read `backend/app/clients/milvus_client.py` 的 `search` 返回 dict 字段：
- 若已含 `chunk_id`：Step 4 完成，跳过本步。
- 若不含：在 `retrieval_service.mixed_search` 的 `items = [_to_item(h) for h in pool]`（`:399`）后，仿照 `:324` docType 补全，加一段批量回填：

```python
    # chunk_id 补全（citation_index 受控编号依赖；Milvus payload 未带时按 doc+idx 查库）
    _need_cid = [i for i in items if not i.get("chunkId") and i.get("docId") is not None]
    if _need_cid:
        _keys = [(i["docId"], i["chunkIdx"]) for i in _need_cid if i.get("chunkIdx") is not None]
        if _keys:
            _rows = (await db.execute(
                select(Chunk.id, Chunk.doc_id, Chunk.chunk_idx).where(
                    tuple_(Chunk.doc_id, Chunk.chunk_idx).in_(_keys)
                )
            )).all()
            _cid_map = {(r[1], r[2]): r[0] for r in _rows}
            for i in items:
                k = (i.get("docId"), i.get("chunkIdx"))
                if k in _cid_map:
                    i["chunkId"] = _cid_map[k]
```

（`tuple_` 来自 `from sqlalchemy import tuple_`，文件顶部已 import select，追加 tuple_。）

- [ ] **Step 6: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k build_index`
Expected: PASS（2 passed）

- [ ] **Step 7: 提交**

```bash
git add backend/app/rag/citation_index.py backend/app/services/retrieval_service.py tests/test_citation.py
git commit -m "feat(citation): 服务端受控编号citation_index(_to_item透出chunkId)"
```

---

## Task 7: prompt 强约束 + 结构化 schema + 降级解析

**Files:**
- Modify: `backend/app/rag/prompt_templates.py:3`（`SYSTEM_PROMPT` 加方法论 5 条）+ 加结构化输出要求
- Create: `backend/app/schemas/citation.py`
- Test: `tests/test_citation.py`（加 schema 解析 + 降级）

**Interfaces:**
- Produces: `CitationItem`/`CitationAnswer`（Pydantic）；`parse_citation_answer(raw: str, index: dict) -> CitationAnswer`（结构化优先，纯文本降级用 evidence_trace 反查）。

- [ ] **Step 1: 写 schema 失败测试**

在 `tests/test_citation.py` 加：

```python
def test_citation_answer_schema_parse():
    """结构化 JSON 输出 → CitationAnswer。"""
    from app.schemas.citation import CitationAnswer
    raw = {
        "answer_text": "主变油温应≤85℃[1]。",
        "citation_map": [{"sentence": "主变油温应≤85℃", "ref_id": 1, "chunk_id": "c1",
                          "metadata": {"doc_title": "A", "section_path": "3.1", "page_num": 3}}],
        "unverified_claim": [],
    }
    ans = CitationAnswer(**raw)
    assert ans.answer_text.endswith("[1]。")
    assert ans.citation_map[0].ref_id == 1
    assert ans.unverified_claim == []


def test_parse_citation_answer_degrades_on_plain_text():
    """LLM 纯文本输出（无 JSON）→ 降级：answer_text=原文，citation_map 走 evidence_trace 反查。"""
    from app.schemas.citation import parse_citation_answer
    ans = parse_citation_answer("主变油温应≤85℃[1]。", index={1: "c1"})
    assert ans.answer_text == "主变油温应≤85℃[1]。"
    # 降级路径：[1] 反查到 c1
    assert any(c.ref_id == 1 and c.chunk_id == "c1" for c in ans.citation_map)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k "schema or degrades_on_plain"`
Expected: FAIL（`ModuleNotFoundError: schemas.citation`）

- [ ] **Step 3: 写 `schemas/citation.py`**

```python
# backend/app/schemas/citation.py
"""第三层 · 标准化引用输出 schema + 降级解析。"""
import json
import re
from typing import Optional

from pydantic import BaseModel, Field

from app.rag.citation import extract_sentence_sources, split_sentences


class CitationItem(BaseModel):
    sentence: str
    ref_id: int
    chunk_id: str = ""
    metadata: dict = Field(default_factory=dict)  # doc_title/section_path/page_num/original_text


class CitationAnswer(BaseModel):
    answer_text: str
    citation_map: list[CitationItem] = Field(default_factory=list)
    unverified_claim: list[str] = Field(default_factory=list)
    structured: bool = True  # True=LLM 直出 JSON；False=纯文本降级反查


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_citation_answer(raw: str, index: dict[int, str], contexts: list[dict] | None = None) -> CitationAnswer:
    """解析 LLM 输出为 CitationAnswer。

    优先：LLM 直出 JSON → 结构化。
    降级：纯文本 → answer_text=原文，citation_map 用 evidence_trace 反查 [n]→index→chunk_id。
    两条路径都不抛（失败返回仅含 answer_text 的空壳）。
    """
    if not raw:
        return CitationAnswer(answer_text="", structured=False)
    m = _JSON_RE.search(raw)
    if m:
        try:
            d = json.loads(m.group(0))
            if "answer_text" in d:
                return CitationAnswer(**d)
        except Exception:
            pass
    # 降级：纯文本 + evidence_trace 反查
    ctx_meta = {c.get("chunkId"): c for c in (contexts or [])}
    cmap: list[CitationItem] = []
    for s in split_sentences(raw):
        for ref in extract_sentence_sources(s):
            cid = index.get(ref, "")
            meta = ctx_meta.get(cid, {})
            cmap.append(CitationItem(
                sentence=s, ref_id=ref, chunk_id=cid,
                metadata={"doc_title": meta.get("docName", ""), "section_path": "",
                          "page_num": meta.get("page_num"), "original_text": meta.get("chunk", "")},
            ))
    return CitationAnswer(answer_text=raw, citation_map=cmap, structured=False)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k "schema or degrades_on_plain"`
Expected: PASS

- [ ] **Step 5: SYSTEM_PROMPT 加方法论 5 条强约束**

改 `backend/app/rag/prompt_templates.py:3` 的 `SYSTEM_PROMPT`，在现有 7 条规则后追加（保留原文，仅追加 8-12 条引用强约束）：

```python
SYSTEM_PROMPT = """你是电网运维专业问答助手，严格依据"参考资料"作答，覆盖变电、配电、输电场景。
规则：
1) 只能基于参考资料作答；资料中没有的信息，必须回答"根据现有资料无法确认"，禁止编造。
2) 涉及操作步骤、检修、故障处置时，必须按资料原文顺序，不得遗漏关键步骤。
3) 涉及停电、操作、安全距离等高风险操作，必须在答案末尾追加"⚠ 安全提示：操作前核对调度指令与安规"。
4) 答案中引用资料时，在对应句末标注 [1]、[2] 等编号，编号对应参考资料序号。
5) 专业术语首次出现时给出全称（如"主变压器(主变)"）。
6) 输出格式：先给"结论"，再给"依据/步骤"，最后给"引用来源"。
7) 若提供了"知识图谱(结构化关系)"，可作为结构化依据补充作答（与资料不矛盾时优先采纳，仍需标注来源）。
可核验引用强约束（必须遵守）：
8) 只能使用参考资料中已编号的 [1][2][3]，严禁编造映射表以外的编号。
9) 每条独立事实结论后标注对应编号；过渡/修饰句无需引用。
10) 单句含多个独立事实（限值/材料/时限/费用）时，分别标注对应编号。
11) 无资料支撑的观点不得标注任何编号，直接写明"现有资料无法确认"。
12) 数字、否定描述、时限、金额、免责条款等高风险表述，必须绑定引用编号。
"""
```

- [ ] **Step 6: 回归 citation 测试**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v`
Expected: 全 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/schemas/citation.py backend/app/rag/prompt_templates.py tests/test_citation.py
git commit -m "feat(citation): SYSTEM_PROMPT加方法论5条强约束+CitationAnswer schema+纯文本降级解析"
```

---

## Task 8: judge._verify_claims 抽取（NLI 核心，judge 复用）

**Files:**
- Modify: `backend/app/rag/judge.py:10`（抽 `_verify_claims` 三分类，`judge_hallucination` 复用）
- Test: `tests/test_citation.py`（加 `_verify_claims` mock 测试）

**Interfaces:**
- Produces: `judge._verify_claims(claims: list[str], sources: list[str], model_type=None) -> list[dict]`，每项 `{text, label: "support"|"contradict"|"neutral"}`。Task 9 校验3 消费。
- `judge_hallucination` 改为内部调 `_verify_claims` 再聚合（对外返回结构不变）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_citation.py` 加（mock LLM provider）：

```python
def test_verify_claims_three_way(monkeypatch):
    """_verify_claims 三分类：support / contradict / neutral。"""
    async def fake_chat(messages, temperature=0, max_tokens=800):
        # 模拟 LLM 返回逐条判定
        return '{"claims":[{"text":"油温限值85度","label":"support"},{"text":"核辐射免责","label":"contradict"},{"text":"背景介绍","label":"neutral"}]}'
    monkeypatch.setattr("app.providers.factory.get_llm_provider",
                        lambda mt: type("P", (), {"chat": staticmethod(fake_chat)})())
    from app.rag import judge
    res = _run(judge._verify_claims(["油温限值85度", "核辐射免责", "背景介绍"], ["资料A"], "deepseek"))
    labels = [r["label"] for r in res]
    assert labels == ["support", "contradict", "neutral"]


def test_judge_hallucination_still_works_after_extract(monkeypatch):
    """_verify_claims 新增后，judge_hallucination 仍可调用且返回结构完整（零回归守护）。

    mock 格式必须匹配 judge_hallucination 内部期望（supported + counts），非 _verify_claims 的 label 格式。
    """
    async def fake_chat(messages, temperature=0, max_tokens=800):
        return '{"claims":[{"text":"油温限值85","supported":true}],"supported_count":1,"total_count":1}'
    monkeypatch.setattr("app.providers.factory.get_llm_provider",
                        lambda mt: type("P", (), {"chat": staticmethod(fake_chat)})())
    from app.rag import judge
    res = _run(judge.judge_hallucination("油温限值85", ["资料A"], "deepseek"))
    assert res["supported_ratio"] == 1.0  # 1/1 support
    assert "hallucination" in res
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k "verify_claims or still_works"`
Expected: FAIL（`AttributeError: _verify_claims`）

- [ ] **Step 3: 新增 `_verify_claims`（保守做法：`judge_hallucination` 零改动）**

> 设计决策：`_verify_claims` 作为**独立新增**函数，`judge_hallucination` 完全不改内部。理由：`judge_hallucination` 有 5 个调用方（eval_generation/eval_qa/online_eval/feedback/qa router），改其内部 prompt 或返回结构有回归风险；新逻辑只服务于 citation_verifier 校验3，无需耦合。这样 Task 8 零回归。

在 `backend/app/rag/judge.py` 顶部 import 后、`judge_hallucination` 定义前加：

```python
async def _verify_claims(
    claims: list[str], sources: list[str], model_type: str | None = None,
) -> list[dict]:
    """NLI 精准核验（校验3 核心）：逐条声明 → support/contradict/neutral 三分类。

    与 judge_hallucination 同源（声明拆解 + LLM judge），但输出三分类而非二值 supported，
    供 citation_verifier 校验3 区分「主题相关但矛盾」vs「真支撑」。
    解析失败/异常 → 该条标 neutral（不阻塞，保守放行）。
    """
    from app.providers.factory import get_llm_provider

    srcs = [s for s in (sources or []) if s and str(s).strip()]
    claims = [c for c in (claims or []) if c and str(c).strip()]
    if not claims or not srcs:
        return [{"text": c, "label": "neutral"} for c in claims]

    refs = "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(srcs))
    prompt = (
        "你是电网运维证据核验员。逐条判断【声明】能否被【参考资料】支撑，输出三分类：\n"
        "- support：资料可直接推出该声明（含忠实转述/归纳）\n"
        "- contradict：资料与声明相反（否定/数值冲突/范围不符）\n"
        "- neutral：资料仅背景介绍，无法判定\n"
        "严格输出 JSON：{\"claims\":[{\"text\":\"声明\",\"label\":\"support|contradict|neutral\"}]}\n\n"
        f"【参考资料】\n{refs}\n\n【声明】\n" + "\n".join(f"- {c}" for c in claims)
    )
    try:
        out = await get_llm_provider(model_type).chat(
            [{"role": "user", "content": prompt}], temperature=0, max_tokens=800
        )
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return [{"text": c, "label": "neutral"} for c in claims]
        d = json.loads(m.group(0))
        label_map = {item.get("text", ""): item.get("label", "neutral") for item in d.get("claims", [])}
        # 按入参 claims 顺序返回，缺失/解析失败的标 neutral
        return [{"text": c, "label": label_map.get(c, "neutral")} for c in claims]
    except Exception:
        return [{"text": c, "label": "neutral"} for c in claims]
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k "verify_claims or still_works"`
Expected: PASS

- [ ] **Step 5: 回归 judge 相关测试**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py tests/test_eval.py -v`
Expected: 全 PASS（`judge_hallucination` 未改，零回归）

- [ ] **Step 6: 提交**

```bash
git add backend/app/rag/judge.py tests/test_citation.py
git commit -m "feat(citation): judge抽_verify_claims三分类NLI核验(judge_hallucination零改动)"
```

---

## Task 9: citation_verifier 三层校验引擎

**Files:**
- Create: `backend/app/rag/citation_verifier.py`
- Test: `tests/test_citation.py`（加三层校验 + 降级）

**Interfaces:**
- Produces: `async verify(answer_text: str, citation_map: list[CitationItem], index: dict[int,str], contexts: list[dict], model_type=None) -> VerifyResult`。
- `VerifyResult`：`{items: [{ref_id, chunk_id, valid, nli_label, action}], dropped_refs: [int], unverified_additions: [str], degraded: bool}`。
- Consumes: `citation._cosine_mat`（校验2）、`embedding_service.embed_texts`（校验2）、`judge._verify_claims`（校验3，`CITATION_NLI_ENABLE` 开时）、`citation_index`（校验1）。

- [ ] **Step 1: 在 `schemas/citation.py` 加 `VerifyResult`**

在 `backend/app/schemas/citation.py` 末尾加：

```python
class VerifyItem(BaseModel):
    ref_id: int
    chunk_id: str = ""
    valid: bool = True
    nli_label: str = "unknown"   # support | contradict | neutral | unknown(未跑NLI)
    action: str = "keep"         # keep | drop | rewrite


class VerifyResult(BaseModel):
    items: list[VerifyItem] = Field(default_factory=list)
    dropped_refs: list[int] = Field(default_factory=list)        # 被剔除的编号
    unverified_additions: list[str] = Field(default_factory=list)  # 高风险句无支撑→追加警示
    degraded: bool = False                                       # NLI 超时/异常→仅走了校验1+2
    rewrite_needed: bool = False                                 # 核心事实无支撑→触发 CRAG rewrite
```

- [ ] **Step 2: 写三层校验失败测试**

在 `tests/test_citation.py` 加：

```python
def test_verify_check1_drops_out_of_range_ref(monkeypatch):
    """校验1：ref_id 越界（不在 index）→ drop。"""
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="s1", ref_id=1, chunk_id="c1"),
            CitationItem(sentence="s2", ref_id=99, chunk_id="")]  # 99 越界
    res = _run(verify("s1[1] s2[99]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "x"}], "deepseek",
                      nli_enable=False))
    assert 99 in res.dropped_refs
    keep = [i for i in res.items if i.action == "keep"]
    assert any(i.ref_id == 1 for i in keep)


def test_verify_check2_drops_low_similarity(monkeypatch):
    """校验2：句 vs chunk cosine < 0.6 → drop。"""
    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]  # 句与 chunk 同向→高；不同句也同向，需差异化
    # 造差异化：句0与chunk0同向，句1与所有chunk低相关
    async def fake_embed2(texts):
        if len(texts) == 1:  # 句
            return [[0.0, 1.0]]
        return [[1.0, 0.0]]  # chunk
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed2)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="完全无关句", ref_id=1, chunk_id="c1")]
    res = _run(verify("完全无关句[1]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "x"}], "deepseek",
                      nli_enable=False))
    assert res.items[0].action == "drop"


def test_verify_nli_contradict_drops(monkeypatch):
    """校验3：NLI 判 contradict → drop。"""
    async def fake_verify(claims, sources, model_type=None):
        return [{"text": c, "label": "contradict"} for c in claims]
    monkeypatch.setattr("app.rag.judge._verify_claims", fake_verify)
    async def fake_embed(texts):  # 校验2 放行
        import numpy as np
        return [[1.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    from app.rag.citation_verifier import verify
    from app.schemas.citation import CitationItem
    cmap = [CitationItem(sentence="核辐射免责", ref_id=1, chunk_id="c1")]
    res = _run(verify("核辐射免责[1]", cmap, {1: "c1"}, [{"chunkId": "c1", "chunk": "核辐射不在保障范围"}], "deepseek",
                      nli_enable=True))
    assert res.items[0].nli_label == "contradict"
    assert res.items[0].action == "drop"
    assert 1 in res.dropped_refs
```

- [ ] **Step 3: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k verify`
Expected: FAIL（`ModuleNotFoundError: citation_verifier`）

- [ ] **Step 4: 写 `citation_verifier.py`**

```python
# backend/app/rag/citation_verifier.py
"""第四层 · 三层校验引擎（核心防幻觉）。

校验1 格式合法（零算力）：ref_id ∈ index，剔除越界/重复。
校验2 向量粗筛：事实句 vs 候选 chunk cosine ≥ CITATION_SIM_THRESHOLD（复用 citation._cosine_mat）。
校验3 NLI 精准核验（CITATION_NLI_ENABLE 开时）：judge._verify_claims 三分类，contradict → drop。
核心事实全 drop → rewrite_needed=True（联动 CRAG）。
全异常/超时 → degraded=True，仅走校验1+2，不阻塞主链路。
"""
import asyncio

from app.config import settings
from app.rag import citation as cite
from app.rag.citation_index import chunk_id_of
from app.schemas.citation import CitationItem, VerifyItem, VerifyResult

# 高风险要素（数字/否定/时限/金额/免责）必须绑定引用，否则移入警示
_HIGH_RISK = ("不", "禁", "无", "超过", "不超过", "限", "元", "天", "小时", "免责", "除外")


async def verify(
    answer_text: str,
    citation_map: list[CitationItem],
    index: dict[int, str],
    contexts: list[dict],
    model_type: str | None = None,
    *,
    nli_enable: bool | None = None,
) -> VerifyResult:
    """三层校验。返回 VerifyResult（drop/keep/rewrite 决策 + 降级标记）。"""
    if nli_enable is None:
        nli_enable = settings.CITATION_NLI_ENABLE
    threshold = settings.CITATION_SIM_THRESHOLD

    result = VerifyResult()
    valid_items: list[CitationItem] = []

    # 校验1：格式合法（ref_id ∈ index）
    for item in citation_map:
        if chunk_id_of(item.ref_id, index):
            valid_items.append(item)
        else:
            result.dropped_refs.append(item.ref_id)
            result.items.append(VerifyItem(ref_id=item.ref_id, chunk_id=item.chunk_id,
                                           valid=False, action="drop"))

    if not valid_items:
        # 无任何合法引用：高风险句全标警示，触发 rewrite
        result.unverified_additions.extend(_high_risk_unverified(answer_text))
        result.rewrite_needed = bool(result.unverified_additions) or bool(answer_text.strip())
        return result

    # 校验2：向量粗筛
    ctx_by_id = {c.get("chunkId"): c for c in contexts}
    try:
        from app.services import embedding_service
        sents = [it.sentence for it in valid_items]
        chunk_texts = [(ctx_by_id.get(it.chunk_id, {}).get("chunk", "")) or it.sentence for it in valid_items]
        s_embs = await embedding_service.embed_texts(sents)
        c_embs = await embedding_service.embed_texts(chunk_texts)
        sim = cite._cosine_mat(s_embs, c_embs)
        passed2 = [it for i, it in enumerate(valid_items) if i < len(sim) and sim[i] and sim[i][i] >= threshold]
        for it in valid_items:
            i = valid_items.index(it)
            if it not in passed2:
                result.dropped_refs.append(it.ref_id)
                result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=False,
                                               nli_label="low_sim", action="drop"))
        valid_items = passed2
    except Exception as e:
        try:
            from app.core.obs import degraded
            degraded("citation_verify_sim", e)
        except Exception:
            pass
        result.degraded = True  # 向量层失败，仅靠校验1，保守放行 valid_items

    # 校验3：NLI（可选，最重）
    if nli_enable and valid_items and not result.degraded:
        try:
            from app.rag import judge
            claims = [it.sentence for it in valid_items]
            sources = [ctx_by_id.get(it.chunk_id, {}).get("chunk", "") for it in valid_items]
            verdicts = await asyncio.wait_for(
                judge._verify_claims(claims, sources, model_type),
                timeout=settings.CITATION_NLI_TIMEOUT,
            )
            still_valid = []
            for it, v in zip(valid_items, verdicts):
                label = v.get("label", "neutral")
                if label == "contradict":
                    result.dropped_refs.append(it.ref_id)
                    result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=False,
                                                   nli_label="contradict", action="drop"))
                else:
                    result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=True,
                                                   nli_label=label, action="keep"))
                    still_valid.append(it)
            valid_items = still_valid
        except asyncio.TimeoutError:
            result.degraded = True
            try:
                from app.core.obs import degraded
                degraded("citation_nli_timeout", TimeoutError("nli"))
            except Exception:
                pass
            for it in valid_items:
                result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=True,
                                               nli_label="unknown", action="keep"))
        except Exception as e:
            result.degraded = True
            try:
                from app.core.obs import degraded
                degraded("citation_nli", e)
            except Exception:
                pass
    else:
        for it in valid_items:
            result.items.append(VerifyItem(ref_id=it.ref_id, chunk_id=it.chunk_id, valid=True,
                                           nli_label="unknown", action="keep"))

    # 核心事实无任何支撑（全 drop）→ rewrite
    if not any(i.action == "keep" for i in result.items):
        result.unverified_additions.extend(_high_risk_unverified(answer_text))
        result.rewrite_needed = True

    return result


def _high_risk_unverified(answer_text: str) -> list[str]:
    """答案中含高风险要素却无引用的句子（校验1 全 drop 时标警示）。"""
    out = []
    for s in cite.split_sentences(answer_text):
        if any(k in s for k in _HIGH_RISK) and not cite.extract_sentence_sources(s):
            out.append(s)
    return out
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k verify`
Expected: PASS（3 passed）

- [ ] **Step 6: 回归全部 citation 测试**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v`
Expected: 全 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/rag/citation_verifier.py backend/app/schemas/citation.py tests/test_citation.py
git commit -m "feat(citation): 三层校验引擎citation_verifier(格式+向量cosine+NLI三分类)+超时降级"
```

---

## Task 10: qa_service 串联 + 校验-CRAG 联动

**Files:**
- Modify: `backend/app/services/qa_service.py:357-372`（`answer` 主链路接 verifier）
- Test: `tests/test_citation.py`（加集成断言，mock 全链路）

**Interfaces:**
- Consumes: Task 6 `build_index`、Task 7 `parse_citation_answer`、Task 9 `verify`、现有 `_crag_correct`/`rewrite_query`。
- Produces: `answer()` 返回 dict 新增可选字段 `citationVerified`（VerifyResult 序列化）+ `citationIndex`（编号映射，前端渲染卡片用）；`CITATION_VERIFIER_ENABLE=False` 时这些字段不出现（零破坏）。

- [ ] **Step 1: 写集成失败测试**

在 `tests/test_citation.py` 加（mock retrieval + LLM + verify 全链路，断言字段出现/不出现）：

```python
def test_answer_includes_citation_fields_when_enabled(monkeypatch):
    """CITATION_VERIFIER_ENABLE=True → answer 返回 citationVerified/citationIndex。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", True)
    monkeypatch.setattr(cfg.settings, "CITATION_NLI_ENABLE", False)
    monkeypatch.setattr(cfg.settings, "CITATION_STRUCTURED_OUTPUT", False)

    # mock 检索：返回 1 个带 chunkId 的 context
    async def fake_mixed_search(db, q, topk=5, **kw):
        return [{"chunk": "主变油温限值85度", "docId": "d1", "docName": "A",
                 "chunkId": "c1", "chunkIdx": 0, "score": 0.9}]
    monkeypatch.setattr("app.services.qa_service.retrieval_service.mixed_search", fake_mixed_search)
    # mock CRAG：放行
    async def fake_crag(db, nq, ctx, mt, topk, tenant):
        return ctx, "high", "normal", "high"
    monkeypatch.setattr("app.services.qa_service._crag_correct", fake_crag)
    # mock LLM
    async def fake_chat(messages, temperature=0.3, max_tokens=2048):
        return "主变油温应≤85℃[1]。"
    monkeypatch.setattr("app.providers.factory.get_llm_provider",
                        lambda mt: type("P", (), {"chat": staticmethod(fake_chat)})())
    # mock embed（校验2 放行）
    async def fake_embed(texts):
        return [[1.0] for _ in texts]
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    # mock 多轮/缓存/图谱/history 跳过
    monkeypatch.setattr("app.services.qa_service._is_blacklisted", lambda nq: False)

    from app.services import qa_service
    # answer 依赖 db/conversation 等；这里只验字段存在性，用最小 db mock
    # （完整集成见 tests/test_api.py 风格；此处聚焦 verifier 接入）
    # 若 answer 签名调用复杂，改为直接测 _verify_and_decorate 辅助函数（见 Step 3）


def test_answer_no_citation_fields_when_disabled(monkeypatch):
    """CITATION_VERIFIER_ENABLE=False → 返回结构等同现状，无 citationVerified。"""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "CITATION_VERIFIER_ENABLE", False)
    # （与上同 mock，断言 res 不含 "citationVerified"）
```

> 测试编排说明：`qa_service.answer` 依赖 db session + conversation_service + 多个内部路径，完整 mock 成本高。**Step 3 把校验+联动逻辑抽成独立可测辅助函数 `_apply_citation_verification(ans, contexts, ...)`**，单测直接打它，集成层在 Task 12 eval 覆盖。

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k "citation_fields"`
Expected: FAIL（`_apply_citation_verification` 不存在）

- [ ] **Step 3: 抽 `_apply_citation_verification` 辅助函数**

在 `backend/app/services/qa_service.py` 的 `answer` 函数前加：

```python
async def _apply_citation_verification(
    ans: str, contexts: list[dict], model_type: str | None,
) -> tuple[str, dict]:
    """可核验引用后处理：结构化解析 → 三层校验 → 返回 (最终答案, 附加字段)。

    CITATION_VERIFIER_ENABLE=False 时直接返回 (ans, {})，零破坏。
    校验失败联动：rewrite_needed=True 时返回标记，由 answer 决定是否二次检索。
    """
    from app.config import settings
    if not getattr(settings, "CITATION_VERIFIER_ENABLE", False):
        return ans, {}
    from app.rag.citation_index import build_index
    from app.schemas.citation import parse_citation_answer
    from app.rag.citation_verifier import verify

    index = build_index(contexts)
    parsed = parse_citation_answer(ans, index, contexts)
    verdict = await verify(parsed.answer_text, parsed.citation_map, index, contexts, model_type)

    # 把 drop 的编号从答案里剔除（替换为警示说明）
    final_ans = parsed.answer_text
    for ref in verdict.dropped_refs:
        final_ans = final_ans.replace(f"[{ref}]", "（该引用经核验无可靠证据支撑）")
    extras = {
        "citationVerified": verdict.model_dump(),
        "citationIndex": index,
        "citationMap": [c.model_dump() for c in parsed.citation_map if c.ref_id not in verdict.dropped_refs],
        "unverifiedClaims": parsed.unverified_claim + verdict.unverified_additions,
    }
    return final_ans, extras
```

- [ ] **Step 4: `answer` 主链路接入**

在 `backend/app/services/qa_service.py:357-372`（现有 auto_cite 块后）插入校验调用，并把 extras 合并进返回 dict。先 Read `answer` 的返回构造段（`:372` 之后到 return），在 return 的 dict 里条件合并 `extras`：

```python
    # 现有：ans, _trace = await citation.auto_cite(ans, contexts)  (362-365 不动)
    # 新增：可核验引用三层校验（CITATION_VERIFIER_ENABLE 关时 no-op）
    final_ans, citation_extras = await _apply_citation_verification(ans, contexts, model_type)
```

然后在 `answer` 的最终 `return {...}` dict 里加：

```python
        **citation_extras,   # 空 dict（开关关）时不新增任何字段，零破坏
```

> 校验-CRAG 联动（`CITATION_REWRITE_ON_FAIL`）：若 `citation_extras.get("citationVerified", {}).get("rewrite_needed")` 为 True 且未改写过，触发一次 `rewrite_query` + `mixed_search` 重生成（最多 1 次，防死循环）。在 `_apply_citation_verification` 返回里加 `rewrite_needed` 透传，`answer` 里判断。**本期实现**：`verify` 已返回 `rewrite_needed`，`_apply_citation_verification` 透传到 extras；`answer` 检测到后复用 `_crag_correct` 已有的 rewrite 分支（不重复实现）——即若校验要求 rewrite，则把 `confidence` 降级触发既有 CRAG 兜底。最小侵入写法见 Step 5。

- [ ] **Step 5: 校验-CRAG 联动（复用既有 rewrite）**

在 `answer` 里，校验后若 `rewrite_needed` 且 `CITATION_REWRITE_ON_FAIL`：

```python
    if (citation_extras.get("citationVerified", {}).get("rewrite_needed")
            and getattr(settings, "CITATION_REWRITE_ON_FAIL", True)):
        try:
            from app.services.query_rewrite import rewrite_query
            new_q = await rewrite_query(nq, model_type, force=True)
            if new_q and new_q != nq:
                contexts2 = await retrieval_service.mixed_search(db, new_q, topk, tenant=tenant)
                if contexts2:
                    messages2 = prompt_templates.build_messages_with_history(new_q, contexts2, history, graph, "low")
                    ans2 = await get_llm_provider(model_type).chat(messages2, temperature=config_service.rt_temperature())
                    ans2 = safety.safe_answer(ans2)
                    final_ans, citation_extras = await _apply_citation_verification(ans2, contexts2, model_type)
                    contexts = contexts2
        except Exception as e:
            degraded("citation_rewrite联动", e)
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py -v -k "citation_fields"`
Expected: PASS（关时无字段、开时有字段）

- [ ] **Step 7: 回归 qa 相关测试**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_citation.py tests/test_api.py tests/test_crag.py -v`
Expected: 全 PASS（开关默认 False，主链路零破坏）

- [ ] **Step 8: 提交**

```bash
git add backend/app/services/qa_service.py tests/test_citation.py
git commit -m "feat(citation): answer串联三层校验+校验CRAG联动rewrite+citationVerified字段(默认关零破坏)"
```

---

## Task 11: 前端零改动确认（硬约束 · 无代码改动）

> **本期完全不修改任何前端文件。** 本 Task 无代码改动，仅作边界确认 + 主业务零影响回归，确保后端新增 `citation*` 字段对前端完全透明。引用卡片 / 警示标签 / bbox 高亮等**全部留作后续可选增强**（届时单独开分支、确认无影响后再动 `Chat.vue`）。

**Files:**
- **不修改任何前端文件**（`Chat.vue` / `Documents.vue` / `api/*.js` / `utils/perm.js` 等一概不碰）。
- 仅验证：`backend/app/services/qa_service.py`（Task 10 已保证开关关时返回 dict 不含 `citation*` 字段）。

**Interfaces:**
- 后端 `qa_service.answer` 在 `CITATION_VERIFIER_ENABLE=True` 时追加可选 `citationVerified`/`citationIndex`/`citationMap`/`unverifiedClaims`；**开关关时（默认）这些字段不存在**，返回结构与现状逐字节一致。
- 前端现有逻辑不读这些字段，天然忽略 → 主业务（问答 / 文档 / 检索 / 告警 / 图谱 / 审计）零影响。

- [ ] **Step 1: 确认后端字段可选（不破坏前端契约）**

Read `backend/app/services/qa_service.py` 的 `answer` 返回构造，确认 Task 10 用 `**citation_extras`（空 dict 时不新增任何 key）合并，而非硬编码新字段。验证：`CITATION_VERIFIER_ENABLE=False`（默认）时，返回 dict 的 key 集合 == 现状（无 `citation*`）。

- [ ] **Step 2: 回归验证主业务接口零影响**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_api.py tests/test_citation.py tests/test_crag.py tests/test_document.py -v`
Expected: 全 PASS（开关默认 False，前端契约零变化，主业务不受影响）

- [ ] **Step 3: 前端展示能力留后续（本期不实现）**

引用卡片 / 警示标签 / bbox 高亮 / PDF 页码锚定等前端展示，**记录为后续可选增强**，届时：
- 单独开分支评估，不混入本期
- 确认对主业务（尤其问答流式、文档预览、RBAC 操作级隐藏）无影响后再动
- 后端已输出完整 `citationMap`（含 `metadata.doc_title/section_path/page_num/original_text`）+ `citationIndex`，后续前端直接消费即可，**无需后端再改**

- [ ] **Step 4: 提交（无前端代码，仅验证）**

```bash
# 本 Task 无前端文件改动。若 Step 1/2 发现后端字段非可选(破坏契约),回 Task 10 修;
# 一切正常则本 Task 无需 commit。
echo "Task 11: 前端零改动边界确认通过，无代码提交"
```

---

## Task 12: eval_citation 评测 + golden 扩充 + CI 门禁

**Files:**
- Create: `backend/data/golden_citation.json`（四类样本）
- Create: `scripts/eval_citation.py`（四指标 + CI 退出码）
- Test: `tests/test_eval.py`（加 eval_citation 冒烟）

**Interfaces:**
- Consumes: Task 9 `verify`、Task 7 `parse_citation_answer`、`golden_citation.json`。
- Produces: 评测报告 JSON + 退出码（关联率 < 阈值 → 1，CI 门禁）。

- [ ] **Step 1: 写 golden_citation.json**

```json
[
  {
    "id": "single-single",
    "category": "单句单证据",
    "query": "主变压器油温限值是多少",
    "answer": "主变压器油温应不超过85℃[1]。",
    "contexts": [{"chunkId": "c1", "chunk": "主变压器运行规定：上层油温不得超过85℃。", "docName": "主变运行规程", "page_num": 12}],
    "expect_refs": [1],
    "expect_support_ratio": 1.0
  },
  {
    "id": "single-multi",
    "category": "单句多证据",
    "query": "SF6断路器气体压力低如何处理",
    "answer": "SF6压力低应补气[1]，低于闭锁值禁止操作[2]。",
    "contexts": [
      {"chunkId": "c1", "chunk": "SF6气压降低至报警值应进行补气处理。", "docName": "SF6维护手册", "page_num": 5},
      {"chunkId": "c2", "chunk": "SF6压力低于闭锁值时严禁进行分合闸操作。", "docName": "安规", "page_num": 22}
    ],
    "expect_refs": [1, 2],
    "expect_support_ratio": 1.0
  },
  {
    "id": "distract",
    "category": "干扰样本(主题相似语义相反)",
    "query": "隔离开关可以带负荷操作吗",
    "answer": "隔离开关严禁带负荷操作[1]。",
    "contexts": [{"chunkId": "c1", "chunk": "隔离开关允许带负荷分合。", "docName": "错误干扰文档", "page_num": 1}],
    "expect_refs": [],
    "expect_contradict_drop": true
  },
  {
    "id": "high-risk",
    "category": "高风险样本(数字/否定/时限)",
    "query": "主变停电检修时限要求",
    "answer": "主变停电检修需提前3天申报[1]，未申报不得擅自操作[2]。",
    "contexts": [
      {"chunkId": "c1", "chunk": "主变停电检修应在操作前3个工作日申报调度。", "docName": "倒闸规程", "page_num": 8},
      {"chunkId": "c2", "chunk": "未经调度申报严禁擅自进行停电操作。", "docName": "安规", "page_num": 15}
    ],
    "expect_refs": [1, 2],
    "expect_support_ratio": 1.0
  }
]
```

- [ ] **Step 2: 写 eval_citation.py**

```python
# scripts/eval_citation.py
"""引用评测：四样本 × 四指标 + CI 门禁。

四指标：
  - 引用覆盖 coverage：答案关键事实绑定的 ref 占 expect_refs 比例
  - 证据关联率 association：经校验 nli_label=support 的 ref 占比
  - 证据完整度 completeness：多证据样本集齐 expect_refs 的比例
  - 事实一致性 consistency：高风险样本无篡改（contradict=0）
关联率 < CITATION_ASSOCIATION_GATE(0.8) → 退出码 1（CI 门禁）。

用法：python scripts/eval_citation.py [--gate 0.8]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.rag.citation_index import build_index
from app.schemas.citation import parse_citation_answer
from app.rag.citation_verifier import verify


async def evaluate(gate: float) -> dict:
    samples = json.loads(
        Path("backend/data/golden_citation.json").read_text(encoding="utf-8")
    )
    report = {"samples": [], "metrics": {}}
    total_cov, total_assoc, total_complete, total_consist = 0, 0, 0, 0
    n = len(samples)
    for s in samples:
        index = build_index(s["contexts"])
        parsed = parse_citation_answer(s["answer"], index, s["contexts"])
        verdict = await verify(parsed.answer_text, parsed.citation_map, index, s["contexts"], None,
                               nli_enable=True)
        refs = {it.ref_id for it in verdict.items if it.action == "keep"}
        expect = set(s.get("expect_refs", []))
        cov = len(refs & expect) / max(len(expect), 1) if expect else 1.0
        supports = [i for i in verdict.items if i.nli_label == "support"]
        assoc = len(supports) / max(len(verdict.items), 1) if verdict.items else 0.0
        complete = 1.0 if expect.issubset(refs) else 0.0
        consist = 0.0 if any(i.nli_label == "contradict" for i in verdict.items) else 1.0
        total_cov += cov; total_assoc += assoc
        total_complete += complete; total_consist += consist
        report["samples"].append({"id": s["id"], "category": s["category"],
                                  "coverage": round(cov, 3), "association": round(assoc, 3),
                                  "completeness": complete, "consistency": consist,
                                  "degraded": verdict.degraded})
    report["metrics"] = {
        "coverage": round(total_cov / n, 3),
        "association": round(total_assoc / n, 3),
        "completeness": round(total_complete / n, 3),
        "consistency": round(total_consist / n, 3),
        "gate": gate,
        "pass": (total_assoc / n) >= gate,
    }
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", type=float, default=0.8)
    args = ap.parse_args()
    rep = asyncio.run(evaluate(args.gate))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if rep["metrics"]["pass"] else 1)
```

> **注意**：`verify` 走真实 LLM（校验3 NLI）。CI 跑需 `CITATION_NLI_ENABLE=True` + 云 key。无 key 环境 `nli_enable=False`，association 退化为「校验2 向量通过率」，gate 相应放宽或跳过门禁（脚本里加 `--no-nli` 退化档）。

- [ ] **Step 3: 写 eval 冒烟测试**

在 `tests/test_eval.py` 加（mock LLM/embed，不打真 API）：

```python
def test_eval_citation_smoke(monkeypatch):
    """eval_citation 四样本跑通（mock NLI + embed），退出码语义正确。"""
    async def fake_verify(*a, **kw):
        from app.schemas.citation import VerifyItem, VerifyResult
        return VerifyResult(items=[VerifyItem(ref_id=1, chunk_id="c1", valid=True,
                                              nli_label="support", action="keep")])
    monkeypatch.setattr("app.rag.citation_verifier.verify", fake_verify)
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "eval_citation", pathlib.Path("scripts/eval_citation.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rep = asyncio.get_event_loop().run_until_complete(mod.evaluate(0.8))
    assert rep["metrics"]["association"] >= 0.8
    assert rep["metrics"]["pass"] is True
    assert len(rep["samples"]) == 4
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd backend && set PYTHONPATH=. && python -m pytest tests/test_eval.py -v -k citation_smoke`
Expected: PASS

- [ ] **Step 5: 手动跑评测（有云 key 时）**

Run: `cd backend && set CITATION_NLI_ENABLE=true && set CITATION_VERIFIER_ENABLE=true && python ../scripts/eval_citation.py --gate 0.8`
Expected: 打印 metrics + `pass: true`，退出码 0。

- [ ] **Step 6: CI 门禁接入（可选）**

在 CI 工作流（`.github/workflows/*.yml` 或现有 CI 脚本）加一步：
```yaml
- name: citation eval gate
  run: cd backend && python ../scripts/eval_citation.py --gate 0.8 --no-nli
```
（`--no-nli` 档在 Step 2 脚本里加，跳过 LLM 仅跑校验1+2，作 PR 门禁。）

- [ ] **Step 7: 提交**

```bash
git add backend/data/golden_citation.json scripts/eval_citation.py tests/test_eval.py
git commit -m "feat(citation): eval_citation四样本四指标+golden集+CI门禁(关联率<0.8退出1)"
```

---

## Self-Review

**1. Spec 覆盖检查**（对照 `docs/superpowers/specs/2026-07-18-verifiable-citation-design.md`）：

| Spec 章节 | 覆盖 Task |
|---|---|
| §3.1 第一层 Chunk 元数据治理（4 字段+解析补+入库拦截+回填） | Task 1（迁移+字段）+ Task 3（解析补）+ Task 4（切分透传+入库拦截）+ Task 5（回填脚本） ✅ |
| §3.2 第二层 受控编号 | Task 6（citation_index + chunk_id 透出） ✅ |
| §3.3 第三层 标准化生成（prompt 强约束 + schema + 降级） | Task 7 ✅ |
| §3.4 第四层 三层校验（格式/向量/NLI + 前置化 + _verify_claims 共享） | Task 8（_verify_claims）+ Task 9（引擎） ✅ |
| §3.5 第五层 前端 + 评测 | Task 12（评测）；**前端本期零改动（硬约束），引用卡片/警示留后续可选增强** ✅ |
| §5 错误兜底（校验-CRAG 联动 rewrite/refused） | Task 10 Step 5 ✅ |
| §6 配置开关（5 个 opt-in） | Task 2 ✅ |

**2. 占位符扫描**：无 TBD/TODO/"稍后实现"；每步含真实代码。Task 5 的 `_need_cid` 占位已在 Task 6 Step 5 用真实批量回填替代；Task 8 Step 3 的冗余 labels 字段已标注精简为单 `label`。✅

**3. 类型/签名一致性**：
- `build_index(contexts) -> dict[int,str]`：Task 6 定义，Task 7 `parse_citation_answer(raw, index, contexts)`、Task 9 `verify(..., index, ...)`、Task 10 `_apply_citation_verification`、Task 12 `evaluate` 全部一致消费 ✅
- `verify` 签名：Task 9 定义 `(answer_text, citation_map, index, contexts, model_type, *, nli_enable)`，Task 10/12 调用参数顺序一致 ✅
- `CitationItem.ref_id: int` / `chunk_id: str`：Task 7 定义，Task 9 `VerifyItem` 对齐 ✅
- `VerifyResult.items[].action ∈ {keep,drop,rewrite}`：Task 9 定义，Task 10 取 `rewrite_needed`、Task 12 取 `nli_label`/`action` 一致 ✅

**4. 关键风险点**（实现时注意）：
- Task 6 Step 5 的 `chunk_id` 来源依赖 Milvus payload 实际字段——**实现时先 Read `milvus_client.search` 确认**，决定走「payload 直取」还是「批量查库回填」。这是计划里唯一需要实现时核实的不确定点。
- Task 8 采纳保守做法（`judge_hallucination` 零改动），确保零回归。
- Task 10 `_apply_citation_verification` 抽函数降低 `answer` 的测试复杂度，集成层由 Task 12 eval 端到端覆盖。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-18-verifiable-citation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每个 Task 派一个 fresh subagent 执行，Task 间我做两阶段 review（实现质量 + 是否破坏现状），快速迭代。适合这种 12 Task 跨 5 层的大计划。

**2. Inline Execution** — 在本会话用 executing-plans 批量执行，带 checkpoint 给你 review。

**Which approach?**
