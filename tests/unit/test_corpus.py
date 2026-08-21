"""参考库存取层单测。"""

from __future__ import annotations

from pathlib import Path

from award_audit.core.reference import corpus


# 功能：验证 save→load 往返：网格原样返回、原件被归档、meta 记录 sha1/sheets/n_rows
# 设计：用多 sheet 形态的 grid（带 sheets 键）+ 一个假原件文件，覆盖"归档证据+元数据"两条职责；
#       root 传 tmp_path 隔离，不污染真实 data/reference_corpus
def test_corpus_save_load_roundtrip(tmp_path: Path) -> None:
    raw = tmp_path / "获奖名单.xlsx"
    raw.write_bytes(b"PK\x03\x04fake-xlsx-bytes")
    grid = {"sheet": "一等奖 / 二等奖", "sheets": ["一等奖", "二等奖"], "n_rows": 4,
            "rows": [["【等级：一等奖】"], ["作品", "单位"], ["甲", "A"], ["【等级：二等奖】"]]}
    root = tmp_path / "corpus"

    assert corpus.has("04050014", root=root) is False
    meta = corpus.save("04050014", "https://x.gov.cn/list", grid,
                       raw_path=raw, fetched_at="2026-07-20", root=root)
    assert corpus.has("04050014", root=root) is True
    assert meta.sha1 and meta.sheets == ["一等奖", "二等奖"] and meta.n_rows == 4
    assert meta.raw_file == "获奖名单.xlsx" and meta.fetched_at == "2026-07-20"

    entry = corpus.load("04050014", root=root)
    assert entry is not None
    assert entry.grid == grid                       # 网格原样往返
    assert entry.raw_path is not None and entry.raw_path.is_file()  # 原件已归档
    assert entry.meta.source_url == "https://x.gov.cn/list"


# 功能：未收录的资源项 load 返回 None、has 为 False
# 设计：只查不存，确认缺失不抛异常（未命中要能安全回落到联网路径）
def test_corpus_miss_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    assert corpus.has("99999999", root=root) is False
    assert corpus.load("99999999", root=root) is None


# 功能：不带原件也能入库（raw_file 空、无 sha1），grid 仍可往返
# 设计：回写场景可能只有解析后的 grid 而无留存文件，验证这条降级路径不崩
def test_corpus_save_without_raw(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    grid = {"sheet": "获奖名单", "n_rows": 2, "rows": [["题目"], ["论文甲"]]}
    meta = corpus.save("01010001", "https://x.gov.cn/a", grid, root=root)
    assert meta.raw_file == "" and meta.sha1 == "" and meta.sheets == ["获奖名单"]
    entry = corpus.load("01010001", root=root)
    assert entry is not None and entry.grid == grid and entry.raw_path is None
