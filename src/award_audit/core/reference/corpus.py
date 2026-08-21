"""参考库（M4 · RAG）存取层：按资源项码缓存官网获奖名单。

设计（实施方案 §7.4）：审查优先查本地参考库、未命中再联网并回写，让核对可复现、可审计。
- 键：资源项码（ledger 主键，贯穿全流程），精确匹配、不用向量嵌入——审查须"就是这一份名单"。
- 每个资源项一个目录，存三样：官网原件(审计证据) + meta.json(溯源元数据) + grid.json(解析后网格)。

    data/reference_corpus/<资源项码>/
        <官网原件>.xlsx   # 原样归档，"X日下载的就是这一份"
        meta.json         # source_url / fetched_at / sha1 / raw_file / sheets / n_rows
        grid.json         # parse_award_excel 的产物（多 sheet 已合并，比对直接用）
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from award_audit.core import config


class CorpusMeta(BaseModel):
    """一条参考库条目的溯源元数据（写入 meta.json）。"""

    resource_code: str
    source_url: str = ""      # 采集所用官网 URL（人工复核入口）
    fetched_at: str = ""      # 采集日期 YYYY-MM-DD
    sha1: str = ""            # 官网原件字节 sha1（首份；防篡改/去重）
    raw_file: str = ""        # 归档的原件文件名（首份；空=未存原件）
    raw_files: list[str] = [] # 多赛道/多附件时归档的全部原件文件名
    sheets: list[str] = []    # 名单涉及的 sheet/等级（多 sheet 时各等级页名）
    n_rows: int = 0           # grid 的行数（含合并标记行）


class CorpusEntry(BaseModel):
    """一次参考库读取的产物：元数据 + 网格 + 原件路径。"""

    meta: CorpusMeta
    grid: dict[str, object]        # 与 tools.parse_award_excel 同构，可直接喂比对
    raw_path: Path | None = None   # 归档原件的本地路径（不存在则 None）


# 解析参考库根目录（默认 config.corpus_dir，测试可传 root 覆盖）
def _root(root: Path | None) -> Path:
    return root or config.corpus_dir()


# 某资源项的条目目录
def _entry_dir(code: str, root: Path | None = None) -> Path:
    return _root(root) / code


# 参考库是否已收录该资源项（以 grid.json 存在为准）
def has(code: str, root: Path | None = None) -> bool:
    return (_entry_dir(code, root) / "grid.json").is_file()


# 从 grid 推断涉及的 sheet 列表（多 sheet 有 "sheets"，单 sheet 用 "sheet"）
def _grid_sheets(grid: dict[str, object]) -> list[str]:
    sheets = grid.get("sheets")
    if isinstance(sheets, list) and sheets:
        return [str(s) for s in sheets]
    single = grid.get("sheet")
    return [str(single)] if single else []


# 收录/更新一个资源项：归档官网原件（可多份赛道附件）、写 meta.json 与 grid.json，返回元数据
def save(code: str, source_url: str, grid: dict[str, object],
         raw_path: Path | None = None, raw_paths: list[Path] | None = None,
         fetched_at: str | None = None, root: Path | None = None) -> CorpusMeta:
    d = _entry_dir(code, root)
    d.mkdir(parents=True, exist_ok=True)

    paths = list(raw_paths) if raw_paths else ([raw_path] if raw_path is not None else [])
    raw_files: list[str] = []
    sha1 = ""
    for p in paths:
        if p is None or not Path(p).is_file():
            continue
        src = Path(p)
        if not sha1:
            sha1 = hashlib.sha1(src.read_bytes()).hexdigest()
        dst = d / src.name
        if src.resolve() != dst.resolve():  # 已在库内则免复制（幂等）
            shutil.copy2(src, dst)
        if src.name not in raw_files:
            raw_files.append(src.name)

    meta = CorpusMeta(
        resource_code=code, source_url=source_url,
        fetched_at=fetched_at or date.today().isoformat(),
        sha1=sha1, raw_file=raw_files[0] if raw_files else "", raw_files=raw_files,
        sheets=_grid_sheets(grid), n_rows=int(grid.get("n_rows") or 0),
    )
    (d / "meta.json").write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    (d / "grid.json").write_text(json.dumps(grid, ensure_ascii=False), encoding="utf-8")
    return meta


# 读取一个资源项的参考库条目；未收录返回 None
def load(code: str, root: Path | None = None) -> CorpusEntry | None:
    d = _entry_dir(code, root)
    grid_p, meta_p = d / "grid.json", d / "meta.json"
    if not grid_p.is_file():
        return None
    grid = json.loads(grid_p.read_text(encoding="utf-8"))
    meta = (CorpusMeta.model_validate_json(meta_p.read_text(encoding="utf-8"))
            if meta_p.is_file() else CorpusMeta(resource_code=code))
    raw = d / meta.raw_file if meta.raw_file and (d / meta.raw_file).is_file() else None
    return CorpusEntry(meta=meta, grid=grid, raw_path=raw)
