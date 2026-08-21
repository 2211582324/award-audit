"""L5-agent 联网核对循环：对一个资源项走"感知→决策→行动"的受控核对，产出证据链报告。

路径优先级（实测驱动）：Excel 附件 > HTML 正文 > 页面图片（视觉，低置信）> 无法核对转人工。
每一步是确定性代码骨架 + LLM 定点判断（页面相关性/列映射/名单抽取）——政务场景要可控可审计，
不做自由发挥的全自主循环；所有结论 review 级，人工终审。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel

from award_audit.agent import tools
from award_audit.agent.llm import LlmClient, LlmError
from award_audit.agent.toolkit import image as image_tools
from award_audit.agent.toolkit import pdf as pdf_tools
from award_audit.agent.toolkit.contracts import EvidenceAssetRecord, utc_now
from award_audit.agent.toolkit.safety import inspect_evidence_file
from award_audit.core import config
from award_audit.core.identity import (
    IDENTITY_VERSION,
    build_identities,
    normalize_identity,
)
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import MatchProfile, TemplateSpec, resolve_match_profile
from award_audit.core.reference import corpus


# 是否启用视觉抽取（图片名单）。默认关：多数纯文本模型（deepseek 等）不支持图片输入，
# 喂进去只会 400。用支持视觉的模型（gpt-4o、qwen-vl 等）时设 AWARD_AUDIT_VISION=1 开启。
def _vision_enabled() -> bool:
    value = os.environ.get("AWARD_AUDIT_VISION")
    if value is None:
        config.load_env()
        value = os.environ.get("AWARD_AUDIT_VISION", "")
    return value.strip() in ("1", "true", "yes", "on")

EXTRACT_SYSTEM = """你是教育部学位中心的数据核对员。给你一段官网公示内容（页面正文、或附件 Excel 的网格、或名单图片），
以及要核对的奖项名称与年份。请判断并抽取：

输出 JSON：
{
  "page_is_target": true/false,     // 该内容是否就是这个奖项、这个年份的获奖名单
  "page_year": "内容里体现的年份，如 2025；看不出填 \\"\\"",
  "entries": [                       // 名单条目（尽量全抽；内容不是目标名单时给空数组）
    {"identifier": "项目/专利/批准编号(无则空)",
     "title": "作品/项目/论文名(无则空)",
     "names": "人名，多人用;连接", "org": "单位(无则空)", "grade": "等级(无则空)"}
  ],
  "notes": "一句话说明（如：名单分多个等级段落/内容被截断/图片模糊等）"
}
只抽名单本身，不要把评委、组织单位等无关人名抽进来。"""


class EvidenceReport(BaseModel):
    """一个资源项的联网核对结论 + 证据链（进复核台供人工终审）。"""

    resource_code: str
    identity_version: str = IDENTITY_VERSION
    award_name: str
    year: str
    source_url: str = ""            # 实际用作核对依据的 URL（页面或附件）
    source_kind: str = "none"       # excel | page | image | none
    page_year: str = ""             # 官网内容体现的年份
    year_match: bool | None = None  # 官网年份与文件年份是否一致
    extracted_count: int = 0        # 官网抽取条目数
    submitted_count: int = 0        # 提交行数
    missing: list[str] = []         # 官网有、提交无（疑漏采，错误#5/#9）
    extra: list[str] = []           # 提交有、官网无（疑多采/来源不符）
    verdict: str = "无法核对"        # 一致 / 疑似缺漏 / 疑似多采 / 来源年份不符 / 无法核对
    confidence: str = "low"         # high / medium / low
    notes: str = ""
    evidence: list[str] = []        # 过程记录（访问过的 URL、走了哪条路径）
    source_urls: list[str] = []     # 采集清单登记的官网网址（人工核对入口）
    found_assets: list[str] = []    # 抓取中发现的名单图片/附件 URL（可直接打开核对）
    evidence_assets: list[EvidenceAssetRecord] = []  # 可由 M5 校验并复用的结构化资产
    reason_codes: list[str] = []    # 降级/成因码（"为什么没把握"的规范化，驱动分诊）

    # 记一个降级/成因码到 reason_codes（去重）；把"为什么没把握"变成机器可读留痕，供分诊/过滤/统计
    def flag(self, code: str) -> None:
        if code not in self.reason_codes:
            self.reason_codes.append(code)


_ASSET_KIND_BY_SUFFIX = {
    ".gif": "image", ".jpeg": "image", ".jpg": "image", ".png": "image",
    ".webp": "image", ".pdf": "pdf", ".xls": "xls", ".xlsx": "xlsx",
    ".doc": "document", ".docx": "document",
}

_ACCESS_DENIED_HTTP_STATUS = re.compile(r"\bHTTP\s+(401|403)\b", re.IGNORECASE)


def _asset_kind(url: str, label: str = "") -> str:
    """Infer a declared type without trusting a download endpoint's path.

    Public download services often end in ``.jsp`` or ``.do`` while keeping the
    original filename in a query parameter or visible link text. The download
    branch still verifies the resulting file by magic before it is trusted.
    """

    parsed = urlsplit(url)
    candidates = [
        unquote(parsed.path),
        unquote(label),
        *(unquote(value) for _key, value in parse_qsl(parsed.query)),
    ]
    for candidate in candidates:
        kind = _ASSET_KIND_BY_SUFFIX.get(Path(candidate).suffix.casefold())
        if kind:
            return kind
    return "unknown"


def _record_asset(report: EvidenceReport, asset: EvidenceAssetRecord) -> None:
    """Upsert one parent-bound asset, retaining the richest processing state."""

    key = (asset.url, asset.parent_url)
    for index, existing in enumerate(report.evidence_assets):
        if (existing.url, existing.parent_url) != key:
            continue
        rank = {
            "discovered": 0,
            "downloaded": 1,
            "skipped": 1,
            "failed": 2,
            "access_denied": 2,
            "parsed": 3,
        }
        if rank[asset.status] >= rank[existing.status]:
            report.evidence_assets[index] = asset
        return
    report.evidence_assets.append(asset)


# 从提交文件取比对键集合：{归一化键: 展示文本}（按核对形态档案组键）
def _submitted_keys(files: list[ImportedFile], profile: MatchProfile) -> dict[str, str]:
    rows: list[dict[str, object]] = []
    for imp in files:
        for ri in range(imp.n_rows):
            rows.append({column: imp.value(ri, column) for column in profile.submit_cols})
    return {
        item.key: item.display
        for item in build_identities(rows, profile.submit_cols, combine=profile.combine)
    }


def _submitted_identity_complete(
    files: list[ImportedFile], profile: MatchProfile
) -> bool:
    """Whether M4 can represent one approved identity alternative for every row."""

    configured = set(profile.submit_cols)
    for imported in files:
        for row_idx in range(imported.n_rows):
            row = {
                field: imported.value(row_idx, field)
                for field in imported.header_codes
            }
            representable = any(
                set(alternative).issubset(configured)
                and all(normalize_identity(row.get(field, "")) for field in alternative)
                and (len(alternative) == 1 or profile.combine == "all")
                for alternative in profile.primary_alternatives
            )
            if not representable:
                return False
    return True


def _reference_identity_complete(
    entries: list[object], profile: MatchProfile
) -> bool:
    """Reject a composite M4 match when the source omitted any configured component."""

    if profile.combine != "all":
        return True
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        if not all(normalize_identity(entry.get(field, "")) for field in profile.web_fields):
            return False
    return True


def _entry_keys(
    entries: list[dict[str, Any]],
    profile: MatchProfile,
) -> dict[str, str]:
    return {
        item.key: item.display
        for item in build_identities(entries, profile.web_fields, combine=profile.combine)
    }


# 用 LLM 对一段内容做名单抽取；LLM 失败（空响应/网络/格式）不抛出，返回空 dict 并记因由
def _extract(llm: LlmClient, award: str, year: str, content: str, log: list[str]) -> dict[str, Any]:
    user = f"奖项：{award}\n年份：{year}\n\n官网内容：\n{content}"
    try:
        result = llm.json_call(EXTRACT_SYSTEM, user, max_tokens=8000)
    except LlmError as exc:
        log.append(f"  LLM 抽取失败（转人工）：{exc}")
        return {}
    return result if isinstance(result, dict) else {}


def _grid_rows(grid: dict[str, Any]) -> list[Any]:
    rows = grid.get("rows")
    return rows if isinstance(rows, list) else []


COLMAP_SYSTEM = """你是教育部学位中心的数据核对员。给你一份官网获奖名单的表头与前几行样例（每列前标了列号 [0][1]…），
以及要核对的奖项名称与年份。请判断这份内容并给出各信息所在的列号。

只输出 JSON（不要任何解释）：
{
  "is_target": true/false,     // 这份内容是否就是该奖项的获奖名单
  "year": "名单体现的年份，如 2025；看不出填 \\"\\"",
  "identifier_col": 数字列号或 -1, // 项目编号/专利号/批准号 所在列
  "title_col": 数字列号或 -1,   // 作品名称/项目名/论文题目 所在列
  "names_col": 数字列号或 -1,   // 获奖人/团队/成员 名单所在列
  "org_col": 数字列号或 -1,     // 参赛单位/学校 所在列
  "grade_col": 数字列号或 -1,   // 获奖等级所在列；若等级不在列里而在分段标记中，填 -1
  "notes": "一句话说明"
}"""


# 从网格取样例喂 LLM 认列：跳过【】分段标记行，首个非标记行为表头，随后取 n 行样例
def _grid_sample(grid: dict[str, Any], n: int = 6) -> str:
    rows = _grid_rows(grid)
    header: list[str] | None = None
    samples: list[list[str]] = []
    for r in rows:
        cells = [str(c) for c in r]
        if len(cells) == 1 and cells[0].startswith("【"):
            continue
        if header is None:
            header = cells
            continue
        if cells == header:
            continue
        samples.append(cells)
        if len(samples) >= n:
            break
    if header is None:
        return ""
    head_line = " | ".join(f"[{i}]{c}" for i, c in enumerate(header))
    body = "\n".join(" | ".join(s) for s in samples)
    return f"表头：{head_line}\n样例行：\n{body}"


# 按列映射从网格确定性抽取全部条目（跟踪【等级：x】分段、跳过各段重复表头）。
# keep_org_only：身份型（排名/认证）名单只有机构列（如「排名|学校名称|国家」无题无人），
# 保留仅有 org 的行——否则会被"整行无题无人"护栏整表丢弃。
def _entries_from_grid(grid: dict[str, Any], colmap: dict[str, Any],
                       keep_org_only: bool = False) -> list[dict[str, str]]:
    rows = _grid_rows(grid)

    def _col(key: str) -> int:
        try:
            return int(colmap.get(key, -1))
        except (TypeError, ValueError):
            return -1

    ii = _col("identifier_col")
    ti, ni, oi, gi = _col("title_col"), _col("names_col"), _col("org_col"), _col("grade_col")

    def _cell(cells: list[str], i: int) -> str:
        return cells[i].strip() if 0 <= i < len(cells) else ""

    entries: list[dict[str, str]] = []
    header: list[str] | None = None
    grade_seg = ""
    source_seg = ""
    for r in rows:
        cells = [str(c) for c in r]
        if len(cells) == 1 and cells[0].startswith("【"):  # 分段标记
            if cells[0].startswith("【等级："):
                grade_seg = cells[0][len("【等级："):].rstrip("】").strip()
            elif cells[0].startswith("【名单："):  # 新来源(赛道/附件)：重置表头与等级
                source_seg = cells[0][len("【名单："):].rstrip("】").strip()
                header, grade_seg = None, ""
            continue
        if header is None:  # 首个非标记行 = 表头
            header = cells
            continue
        if cells == header:  # 各分段重复的表头行
            continue
        identifier = _cell(cells, ii)
        title, names, org = _cell(cells, ti), _cell(cells, ni), _cell(cells, oi)
        if not identifier and not title and not names and not (keep_org_only and org):
            continue
        entries.append({"identifier": identifier, "title": title, "names": names, "org": org,
                        "grade": _cell(cells, gi) or grade_seg, "source": source_seg})
    return entries


# 把带「【名单：X】」标记的合并网格拆回 [(来源名, 子网格)]；无标记则视为单一来源。
# 各来源列结构可能不同（不同赛道/届次的附件），必须分别认列，不能一套列号套全部。
def _split_sources(grid: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows = _grid_rows(grid)
    sources: list[tuple[str, list[list[Any]]]] = []
    cur_name = ""
    cur_rows: list[list[Any]] = []
    seen_marker = False
    for r in rows:
        cells = [str(c) for c in r]
        if len(cells) == 1 and cells[0].startswith("【名单："):
            if cur_rows:
                sources.append((cur_name, cur_rows))
            cur_name = cells[0][len("【名单："):].rstrip("】").strip()
            cur_rows = []
            seen_marker = True
        else:
            cur_rows.append(r)
    if cur_rows:
        sources.append((cur_name, cur_rows))
    if not seen_marker:  # 无【名单】标记 → 整个网格是单一来源
        return [("", grid)]
    return [(name, {"rows": rws}) for name, rws in sources]


# 网格名单抽取（认列版）：按来源拆分后**逐源认列**（LLM 只返回列号，极小输出、不被截断），
# Python 确定性读全部行。每条打上来源标记，供下游年度安全网按来源过滤。返回与 _extract 同构的 dict。
def _extract_grid(llm: LlmClient, award: str, year: str,
                  grid: dict[str, Any], log: list[str], profile: MatchProfile) -> dict[str, Any]:
    keep_org = "org" in profile.web_fields  # 身份型（排名/认证）靠 org 组键，保留仅有 org 的行
    all_entries: list[dict[str, str]] = []
    for src_name, sub in _split_sources(grid):
        header = _header_of(sub)
        if header is None:
            continue
        colmap = _map_columns_by_header(header, profile.kind)  # 规整中文表头：关键词确定性映射
        if _needs_llm_colmap(colmap, profile):  # 确定性认列不足（按形态判） → LLM 兜底
            colmap = _llm_colmap(llm, award, year, sub, log) or colmap
        ents = _entries_from_grid(sub, colmap, keep_org_only=keep_org)
        for e in ents:  # 用拆分得到的来源名覆盖（子网格内已无【名单】标记）
            e["source"] = src_name
        all_entries.extend(ents)
    return {"page_is_target": True, "page_year": "", "entries": all_entries, "notes": ""}


# 列角色关键词（规整中文表头确定性映射；names 优先"负责人/队长"这类每行必有的核心人，而非可能为空的"队员"）
_TITLE_KEYS = ("作品名称", "作品名", "项目名称", "项目名", "课程名称", "论文题目", "成果名称", "题目")
_IDENTIFIER_KEYS = ("项目编号", "项目批准号", "批准号", "专利号", "编号")
_LEADER_KEYS = ("负责人", "队长", "第一作者", "主持人", "申报人", "牵头人")
_MEMBER_KEYS = ("队员", "成员", "参赛作者", "获奖人员", "作者名单", "参赛者", "作者")
_ORG_KEYS = ("学校", "院校", "高校", "参赛单位", "学生单位", "单位名称", "单位")
_GRADE_KEYS = ("获奖等级", "奖项等级", "获奖情况", "奖励等级", "等级", "奖项")
# 身份型追加词表（仅 ranking/cert 用，roster 不加 → 零回归）：排名榜的学科列/名次列、认证的专业列
_RANKING_TITLE_KEYS = ("学科名称", "学科")
_RANKING_GRADE_KEYS = ("综合排名", "排名", "名次")
_CERT_TITLE_KEYS = ("认证专业", "专业名称", "专业")


# 表头里首个含任一关键词的列序号，无则 -1
def _find_col(header: list[str], keys: tuple[str, ...]) -> int:
    for i, h in enumerate(header):
        if any(k in str(h).strip() for k in keys):
            return i
    return -1


# 按表头中文名确定性映射列角色（研创赛等官网名单表头规整，无需 LLM）。
# kind：roster 用原词表（零回归）；ranking/cert 才在 title/grade 上追加对应词表（org/names 不动，
# 不加"学校→title"以免与排名榜 org 抢列）。
def _map_columns_by_header(header: list[str], kind: str = "roster") -> dict[str, int]:
    title_keys: tuple[str, ...] = _TITLE_KEYS
    grade_keys: tuple[str, ...] = _GRADE_KEYS
    if kind == "ranking":
        title_keys = _TITLE_KEYS + _RANKING_TITLE_KEYS
        grade_keys = _GRADE_KEYS + _RANKING_GRADE_KEYS
    elif kind == "cert":
        title_keys = _TITLE_KEYS + _CERT_TITLE_KEYS
    leader = _find_col(header, _LEADER_KEYS)
    names = leader if leader >= 0 else _find_col(header, _MEMBER_KEYS)
    return {"identifier_col": _find_col(header, _IDENTIFIER_KEYS),
            "title_col": _find_col(header, title_keys), "names_col": names,
            "org_col": _find_col(header, _ORG_KEYS), "grade_col": _find_col(header, grade_keys)}


# web_fields 角色 → colmap 键（组键要用到的列）
_FIELD_TO_COLMAP = {
    "identifier": "identifier_col", "title": "title_col", "names": "names_col",
    "org": "org_col", "grade": "grade_col",
}


# 确定性认列是否不足、需 LLM 兜底：身份型(all)组键列任一缺失即兜底；名单型(first)全缺才兜底。
# roster([title,names],first) ≡ 旧写死条件 `title<0 and names<0`，零回归。
def _needs_llm_colmap(colmap: dict[str, Any], profile: MatchProfile) -> bool:
    def _lt0(field: str) -> bool:
        try:
            return int(colmap.get(_FIELD_TO_COLMAP.get(field, ""), -1)) < 0
        except (TypeError, ValueError):
            return True
    flags = [_lt0(f) for f in profile.web_fields]
    if not flags:
        return False
    return any(flags) if profile.combine == "all" else all(flags)


# 取子网格的表头（首个非【】标记行）
def _header_of(sub: dict[str, Any]) -> list[str] | None:
    for r in _grid_rows(sub):
        cells = [str(c) for c in r]
        if len(cells) == 1 and cells[0].startswith("【"):
            continue
        return cells
    return None


# LLM 认列兜底（表头关键词认不出时用）：返回列映射 dict 或 None
def _llm_colmap(llm: LlmClient, award: str, year: str,
                sub: dict[str, Any], log: list[str]) -> dict[str, Any] | None:
    sample = _grid_sample(sub)
    if not sample:
        return None
    user = f"奖项：{award}\n年份：{year}\n\n{sample}"
    try:
        colmap = llm.json_call(COLMAP_SYSTEM, user, max_tokens=600)
    except LlmError as exc:
        log.append(f"  LLM 认列失败：{exc}")
        return None
    return colmap if isinstance(colmap, dict) else None


# 参考库命中：用缓存网格做认列抽取并落好来源标注，返回 extraction（未收录/空则返回 {}）
def _try_corpus(resource_code: str, award: str, year: str,
                llm: LlmClient, report: EvidenceReport, profile: MatchProfile) -> dict[str, Any]:
    entry = corpus.load(resource_code)
    if entry is None:
        return {}
    grid = entry.grid
    if not (isinstance(grid.get("rows"), list) and grid["rows"]):
        return {}
    report.evidence.append(f"参考库命中（采集于 {entry.meta.fetched_at}）：{entry.meta.source_url}")
    extraction = _extract_grid(llm, award, year, grid, report.evidence, profile)
    if extraction.get("entries"):
        report.source_url, report.source_kind = entry.meta.source_url, "excel"
        report.confidence = "high"
        report.flag("corpus_hit")
        report.notes = f"名单来自参考库（采集于 {entry.meta.fetched_at}）；"
    return extraction


def _acquire_pdf_asset(
    report: EvidenceReport,
    pdf_url: str,
    workdir: Path,
    *,
    parent_url: str = "",
    label: str = "",
    error_code: str = "DIRECT_PDF_ACQUISITION_FAILED",
    extraction_method: str = "m4_direct_pdf_inspection",
    downloaded_path: Path | None = None,
) -> None:
    """Persist a direct or page-discovered PDF without deciding its business role."""

    local_path: Path | None = None
    inspection = None
    try:
        local_path = downloaded_path or tools.download_file(
            pdf_url,
            workdir,
            excel_only=False,
            referer=parent_url,
        )
        inspection = inspect_evidence_file(
            local_path, max_bytes=20 * 1024 * 1024, allowed_kinds={"pdf"}
        )
        pdf_inspection = pdf_tools.inspect_pdf(
            local_path, max_pages=pdf_tools.MAX_PDF_PAGES
        )
        if pdf_url not in report.found_assets:
            report.found_assets.append(pdf_url)
        _record_asset(report, EvidenceAssetRecord(
            url=pdf_url,
            parent_url=parent_url,
            label=label,
            kind="pdf",
            status="parsed",
            content_type=inspection.content_type,
            sha256=inspection.sha256,
            size_bytes=inspection.size_bytes,
            fetched_at=utc_now(),
            local_path=str(local_path),
            extraction_method=extraction_method,
            metadata={
                "summary": "M4 verified a PDF; M5 must determine its relation and contribution.",
                "page_count": pdf_inspection.page_count,
                "digital_pages": pdf_inspection.digital_pages[:50],
                "scan_candidate_pages": pdf_inspection.scan_candidate_pages[:50],
                "anchors": [f"page:{page}" for page in pdf_inspection.digital_pages[:10]],
            },
        ))
    except Exception as exc:  # noqa: BLE001 - a PDF must remain auditable on failure.
        denied = _ACCESS_DENIED_HTTP_STATUS.search(str(exc))
        status = "access_denied" if denied else "failed"
        recorded_error_code = "ATTACHMENT_ACCESS_DENIED" if denied else error_code
        if pdf_url not in report.found_assets:
            report.found_assets.append(pdf_url)
        _record_asset(report, EvidenceAssetRecord(
            url=pdf_url,
            parent_url=parent_url,
            label=label,
            kind="pdf",
            status=status,
            content_type=inspection.content_type if inspection is not None else "",
            sha256=inspection.sha256 if inspection is not None else "",
            size_bytes=inspection.size_bytes if inspection is not None else 0,
            fetched_at=utc_now() if inspection is not None else "",
            local_path=str(local_path) if inspection is not None and local_path else "",
            extraction_method=(
                extraction_method.replace("inspection", "downloaded_unparsed")
                if inspection is not None else ""
            ),
            error_code=recorded_error_code,
            error_message=f"{type(exc).__name__}: {str(exc)[:400]}",
            metadata={
                "summary": (
                    "Official attachment access was denied before M4 could "
                    "inspect the PDF."
                    if denied else "M4 downloaded a PDF but could not complete page inspection."
                ),
                "page_inspection_status": "failed",
                "http_status": int(denied.group(1)) if denied else None,
                "access_status": "denied" if denied else "",
                "blockers": ["official_attachment_access_denied"] if denied else [],
            },
        ))
        report.flag("attachment_access_denied" if denied else "direct_pdf_unavailable")


def _acquire_image_asset(
    report: EvidenceReport,
    image_url: str,
    workdir: Path,
    *,
    parent_url: str,
    page: int,
    total_pages: int,
    parent_roster_complete: bool = False,
) -> None:
    """Download and verify one page image without assigning a business scope."""

    local_path: Path | None = None
    inspection = None
    try:
        local_path = tools.download_file(
            image_url,
            workdir / "images",
            excel_only=False,
            referer=parent_url,
        )
        inspection = image_tools.inspect_image(
            local_path,
            max_bytes=20 * 1024 * 1024,
            max_pixels=image_tools.MAX_IMAGE_PIXELS,
        )
        metadata: dict[str, Any] = {
            "summary": (
                "M4 downloaded and verified a page image; M5 must extract its "
                "roster records and determine the applicable scope."
            ),
            "page": page,
            "total_pages": total_pages,
            "width": inspection.width,
            "height": inspection.height,
            "pixels": inspection.pixels,
            "anchors": [f"image:{page}"],
        }
        if parent_roster_complete:
            metadata["m4_html_parent_roster_complete"] = True
        _record_asset(report, EvidenceAssetRecord(
            url=image_url,
            parent_url=parent_url,
            kind="image",
            status="downloaded",
            content_type=inspection.content_type,
            sha256=inspection.sha256,
            size_bytes=inspection.size_bytes,
            fetched_at=utc_now(),
            local_path=str(local_path),
            extraction_method="m4_page_image_download",
            metadata=metadata,
        ))
    except Exception as exc:  # noqa: BLE001 - every discovered image remains auditable.
        denied = _ACCESS_DENIED_HTTP_STATUS.search(str(exc))
        _record_asset(report, EvidenceAssetRecord(
            url=image_url,
            parent_url=parent_url,
            kind="image",
            status="access_denied" if denied else "failed",
            content_type=inspection.content_type if inspection is not None else "",
            sha256=inspection.sha256 if inspection is not None else "",
            size_bytes=inspection.size_bytes if inspection is not None else 0,
            fetched_at=utc_now() if inspection is not None else "",
            local_path=str(local_path) if inspection is not None and local_path else "",
            extraction_method="m4_page_image_download",
            error_code=("IMAGE_ACCESS_DENIED" if denied else "IMAGE_ACQUISITION_FAILED"),
            error_message=f"{type(exc).__name__}: {str(exc)[:400]}",
            metadata={
                "summary": "M4 could not safely download and decode a discovered page image.",
                "page": page,
                "total_pages": total_pages,
                "blockers": [
                    "official_image_access_denied" if denied else "image_acquisition_failed"
                ],
            },
        ))
        report.flag("image_access_denied" if denied else "image_acquisition_failed")


def _acquire_unknown_attachment(
    report: EvidenceReport,
    attachment_url: str,
    workdir: Path,
    *,
    parent_url: str,
    label: str,
) -> None:
    """Probe a discovered extensionless attachment by bounded binary inspection."""

    local_path: Path | None = None
    inspection = None
    try:
        local_path = tools.download_file(
            attachment_url,
            workdir,
            excel_only=False,
            referer=parent_url,
        )
        inspection = inspect_evidence_file(local_path, max_bytes=20 * 1024 * 1024)
        if inspection.kind == "pdf":
            _acquire_pdf_asset(
                report,
                attachment_url,
                workdir,
                parent_url=parent_url,
                label=label,
                error_code="ATTACHMENT_PDF_ACQUISITION_FAILED",
                extraction_method="m4_extensionless_attachment_pdf_inspection",
                downloaded_path=local_path,
            )
            return
        if inspection.kind in {"xlsx", "xls"}:
            grid = tools.parse_award_excel(local_path)
            rows = grid.get("rows", [])
            sheets = grid.get("sheets", [])
            _record_asset(report, EvidenceAssetRecord(
                url=attachment_url,
                parent_url=parent_url,
                label=label,
                kind=inspection.kind,
                status="parsed",
                content_type=inspection.content_type,
                sha256=inspection.sha256,
                size_bytes=inspection.size_bytes,
                fetched_at=utc_now(),
                local_path=str(local_path),
                truncated=bool(grid.get("truncated", False)),
                extraction_method="m4_extensionless_attachment_spreadsheet_inspection",
                metadata={
                    "summary": "M4 identified and parsed an extensionless spreadsheet attachment.",
                    "sample_rows": [row for row in rows[:10] if isinstance(row, list)]
                    if isinstance(rows, list) else [],
                    "anchors": [f"{str(sheet)[:200]}!A1" for sheet in sheets[:20]]
                    if isinstance(sheets, list) else [],
                },
            ))
            return
        _record_asset(report, EvidenceAssetRecord(
            url=attachment_url,
            parent_url=parent_url,
            label=label,
            kind=inspection.kind,
            status="downloaded",
            content_type=inspection.content_type,
            sha256=inspection.sha256,
            size_bytes=inspection.size_bytes,
            fetched_at=utc_now(),
            local_path=str(local_path),
            extraction_method="m4_extensionless_attachment_binary_probe",
            metadata={
                "summary": "M4 downloaded an extensionless attachment, but no roster reader is available for its type.",
                "blockers": ["unsupported_extensionless_attachment_type"],
            },
        ))
    except Exception as exc:  # noqa: BLE001 - retain an auditable terminal attachment state.
        denied = _ACCESS_DENIED_HTTP_STATUS.search(str(exc))
        _record_asset(report, EvidenceAssetRecord(
            url=attachment_url,
            parent_url=parent_url,
            label=label,
            kind=inspection.kind if inspection is not None else "unknown",
            status="access_denied" if denied else "failed",
            content_type=inspection.content_type if inspection is not None else "",
            sha256=inspection.sha256 if inspection is not None else "",
            size_bytes=inspection.size_bytes if inspection is not None else 0,
            fetched_at=utc_now() if inspection is not None else "",
            local_path=str(local_path) if inspection is not None and local_path else "",
            extraction_method="m4_extensionless_attachment_binary_probe",
            error_code="ATTACHMENT_ACCESS_DENIED" if denied else "ATTACHMENT_ACQUISITION_FAILED",
            error_message=f"{type(exc).__name__}: {str(exc)[:400]}",
            metadata={
                "summary": (
                    "Official attachment access was denied before M4 could identify its binary type."
                    if denied else "M4 could not download an extensionless attachment for binary inspection."
                ),
                "http_status": int(denied.group(1)) if denied else None,
                "access_status": "denied" if denied else "",
                "blockers": ["official_attachment_access_denied"] if denied else ["attachment_acquisition_failed"],
            },
        ))
        report.flag("attachment_access_denied" if denied else "attachment_acquisition_failed")


def _persist_html_text_asset(workdir: Path, url: str, text: str) -> tuple[Path, str, int]:
    """Store the bounded M4-visible text so M5 can audit it without refetching."""

    payload = text.encode("utf-8")
    if not payload:
        raise ValueError("HTML page contains no visible text")
    destination = workdir / "html" / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination, hashlib.sha256(payload).hexdigest(), len(payload)


def _html_roster_role_count(text: str) -> int:
    """Count explicit roster table shapes without deciding their award relation."""

    normalized = normalize_identity(text)
    layouts = (
        ("\u5b66\u6821\u540d\u79f0", "\u961f\u4f0d\u540d\u79f0"),
        ("\u5b66\u6821", "\u961f\u540d"),
        ("\u5b66\u6821\u540d\u79f0", "\u6307\u5bfc\u8001\u5e08"),
        ("\u5b66\u6821\u540d\u79f0", "\u6307\u5bfc\u6559\u5e08"),
        ("\u5b66\u6821", "\u6559\u5e08"),
        ("\u5e8f\u53f7", "\u5355\u4f4d"),
        ("\u83b7\u5956\u5355\u4f4d",),
    )
    matched_roles: set[str] = set()
    for index, markers in enumerate(layouts):
        if all(normalize_identity(marker) in normalized for marker in markers):
            matched_roles.add(("team", "team", "person", "person", "person", "organization", "organization")[index])
    return len(matched_roles)


def discover_resource(
    resource_code: str,
    files: list[ImportedFile],
    urls: list[str],
    spec: TemplateSpec | None,
    llm: LlmClient | None,
    workdir: Path,
    *,
    use_corpus: bool = False,
) -> EvidenceReport:
    """Discover durable M4 evidence without deciding business relationships."""

    # Retain the former verifier signature while keeping M4 entirely model-free.
    del llm, spec, use_corpus
    award = files[0].award_name if files else ""
    year = files[0].year if files else ""
    report = EvidenceReport(
        resource_code=resource_code,
        award_name=award,
        year=year,
        submitted_count=sum(item.n_rows for item in files),
        source_urls=list(urls),
        notes="M4 仅完成来源和资产发现；名单关系、版本与业务差异由 M5 ReviewAgent 判定。",
    )
    report.flag("m4_discovery_only")
    direct_pdf_urls = [url for url in urls if _asset_kind(url) == "pdf"]
    for pdf_url in direct_pdf_urls:
        _acquire_pdf_asset(report, pdf_url, workdir)
    if direct_pdf_urls:
        report.evidence.append(
            f"M4 已尝试 {len(direct_pdf_urls)} 个已登记直接 PDF。"
        )
    try:
        acquired = tools.acquire_excel_grid(
            [url for url in urls if _asset_kind(url) != "pdf"],
            workdir,
            attachment_filter_fn=lambda attachment: _asset_kind(
                attachment.url, attachment.text
            ) != "pdf",
        )
    except Exception as exc:  # noqa: BLE001 - retain source candidates for M5.
        acquired = None
        report.evidence.append(
            f"附件采集异常：{type(exc).__name__}: {str(exc)[:60]}"
        )
    if acquired is not None:
        for asset_url in acquired.found_assets:
            if asset_url not in report.found_assets:
                report.found_assets.append(asset_url)
        document_urls = {document.source_url for document in acquired.documents}
        for document in acquired.documents:
            content_type = ""
            sha256 = ""
            size_bytes = 0
            kind = _asset_kind(document.source_url)
            try:
                inspected = inspect_evidence_file(
                    document.raw_path,
                    max_bytes=20 * 1024 * 1024,
                    allowed_kinds={"xlsx", "xls"},
                )
                kind = inspected.kind
                content_type = inspected.content_type
                sha256 = inspected.sha256
                size_bytes = inspected.size_bytes
            except Exception as exc:  # noqa: BLE001 - parser record remains useful to M5.
                report.evidence.append(
                    f"附件安全检查失败：{document.source_url}: {type(exc).__name__}"
                )
            rows = document.grid.get("rows", [])
            samples = (
                [row for row in rows[:10] if isinstance(row, list)]
                if isinstance(rows, list) else []
            )
            sheet_names = document.grid.get("sheets", [])
            anchors = (
                [
                    f"{str(sheet_name)[:200]}!A1"
                    for sheet_name in sheet_names[:20]
                    if str(sheet_name).strip()
                ]
                if isinstance(sheet_names, list) else []
            )
            _record_asset(report, EvidenceAssetRecord(
                url=document.source_url,
                parent_url=document.page_url,
                label=document.label,
                kind=kind,
                status="parsed",
                content_type=content_type,
                sha256=sha256,
                size_bytes=size_bytes,
                fetched_at=utc_now() if sha256 else "",
                local_path=str(document.raw_path),
                truncated=bool(document.grid.get("truncated", False)),
                extraction_method="m4_excel_discovery",
                metadata={
                    "summary": "M4 已解析 Excel 候选附件；业务角色和版本关系未判定。",
                    "sample_rows": samples,
                    "anchors": anchors,
                },
            ))
        for asset_url in acquired.found_assets:
            if asset_url in document_urls:
                continue
            acquisition_error = acquired.attachment_errors.get(asset_url, "")
            if "file type pdf is not allowed here" in acquisition_error.casefold():
                # Extensionless download endpoints can be PDFs. The Excel
                # collector has already established their actual type; retry
                # through the bounded PDF inspection path instead of marking
                # a readable attachment as a failed spreadsheet.
                _acquire_pdf_asset(
                    report,
                    asset_url,
                    workdir,
                    parent_url=acquired.attachment_parent_urls.get(
                        asset_url, acquired.page_url
                    ),
                    error_code="ATTACHMENT_PDF_ACQUISITION_FAILED",
                    extraction_method="m4_extensionless_attachment_pdf_inspection",
                )
                continue
            _record_asset(report, EvidenceAssetRecord(
                url=asset_url,
                parent_url=acquired.attachment_parent_urls.get(
                    asset_url, acquired.page_url
                ),
                kind=_asset_kind(asset_url),
                status="failed" if acquisition_error else "discovered",
                error_code=(
                    "ATTACHMENT_ACQUISITION_FAILED" if acquisition_error else ""
                ),
                error_message=acquisition_error,
            ))
        if not acquired.all_attachments_processed:
            report.flag("attachment_group_incomplete")
        report.evidence.append(
            f"M4 已发现 {len(acquired.found_assets)} 个附件候选，"
            f"已解析 {len(acquired.documents)} 个 Excel。"
        )

    for url in urls:
        if _asset_kind(url) == "pdf":
            # Direct PDFs were already downloaded and inspected above. Fetching the
            # binary endpoint as HTML can overwrite its audited PDF terminal state.
            report.evidence.append(f"已跳过直接 PDF 的 HTML 抓取：{url}")
            continue
        report.evidence.append(f"访问页面：{url}")
        try:
            page = tools.fetch_page(url)
        except Exception as exc:  # noqa: BLE001 - later M5 has the original URL.
            report.evidence.append(f"  抓取失败：{type(exc).__name__}: {exc}")
            continue
        if page.status != 200:
            report.evidence.append(f"  HTTP {page.status}，保留原 URL 供 M5 重试")
            continue
        try:
            html_path, html_sha256, html_size_bytes = _persist_html_text_asset(
                workdir, page.url, page.text
            )
        except Exception as exc:  # noqa: BLE001 - an unpersisted page cannot enter M5 comparison.
            _record_asset(report, EvidenceAssetRecord(
                url=page.url,
                parent_url=url if page.url != url else "",
                label=page.title,
                kind="html",
                status="failed",
                truncated=page.text_truncated,
                extraction_method="m4_html_discovery",
                error_code="HTML_EVIDENCE_PERSIST_FAILED",
                error_message=f"{type(exc).__name__}: {str(exc)[:400]}",
                metadata={
                    "title": page.title,
                    "text_summary": page.text[:4000],
                    "anchors": [f"text:1-{min(len(page.text), 4000)}"],
                    "blockers": ["HTML body was not persisted as local evidence"],
                },
            ))
            continue
        _record_asset(report, EvidenceAssetRecord(
            url=page.url,
            parent_url=url if page.url != url else "",
            label=page.title,
            kind="html",
            status="parsed",
            content_type="text/plain; charset=utf-8",
            sha256=html_sha256,
            size_bytes=html_size_bytes,
            fetched_at=utc_now(),
            local_path=str(html_path),
            truncated=page.text_truncated,
            extraction_method="m4_html_discovery",
            metadata={
                "title": page.title,
                "text_summary": page.text[:4000],
                "anchors": [f"text:1-{len(page.text)}"],
                "blockers": ["page_text_truncated"] if page.text_truncated else [],
            },
        ))
        for attachment in page.attachments:
            if attachment.url not in report.found_assets:
                report.found_assets.append(attachment.url)
            attachment_kind = _asset_kind(attachment.url, attachment.text)
            if attachment_kind == "pdf":
                _acquire_pdf_asset(
                    report,
                    attachment.url,
                    workdir,
                    parent_url=page.url,
                    label=attachment.text,
                    error_code="ATTACHMENT_PDF_ACQUISITION_FAILED",
                    extraction_method="m4_attachment_pdf_inspection",
                )
            elif attachment_kind == "unknown":
                _acquire_unknown_attachment(
                    report,
                    attachment.url,
                    workdir,
                    parent_url=page.url,
                    label=attachment.text,
                )
            else:
                _record_asset(report, EvidenceAssetRecord(
                    url=attachment.url,
                    parent_url=page.url,
                    label=attachment.text,
                    kind=attachment_kind,
                ))
        html_roster_complete = (
            not page.text_truncated and _html_roster_role_count(page.text) >= 2
        )
        total_images = len(page.images)
        for image_index, image_url in enumerate(page.images, start=1):
            if image_url not in report.found_assets:
                report.found_assets.append(image_url)
            _acquire_image_asset(
                report,
                image_url,
                workdir,
                parent_url=page.url,
                page=image_index,
                total_pages=total_images,
                parent_roster_complete=html_roster_complete,
            )
    return report


# 对一个资源项执行联网核对：按路径优先级取来源 → 抽取 → 比对 → 报告
def verify_resource(
    resource_code: str,
    files: list[ImportedFile],
    urls: list[str],
    spec: TemplateSpec | None,
    llm: LlmClient,
    workdir: Path,
    *,
    use_corpus: bool = False,
) -> EvidenceReport:
    award = files[0].award_name if files else ""
    year = files[0].year if files else ""
    profile = resolve_match_profile(spec)  # 核对形态：名单型走旧键，排名/认证型换身份列组键
    submitted = _submitted_keys(files, profile)
    submitted_identity_complete = _submitted_identity_complete(files, profile)
    report = EvidenceReport(resource_code=resource_code, award_name=award, year=year,
                            submitted_count=sum(f.n_rows for f in files),
                            source_urls=list(urls))
    if profile.kind != "roster":  # 身份型：留痕核对形态，复核台可见「按什么核对」
        report.evidence.append(f"核对形态：{profile.label}")
        report.notes += f"{profile.label}；"

    extraction: dict[str, Any] = {}
    if use_corpus:  # 先查参考库：命中则用缓存网格比对，全程不联网
        extraction = _try_corpus(resource_code, award, year, llm, report, profile)

    # 路径①：Excel 附件（聚合该资源项所有赛道/附件为一份网格）——参考库未命中才联网
    if not extraction.get("entries"):
        acquired = None
        try:
            acquired = tools.acquire_excel_grid(urls, workdir)
        except Exception as exc:  # noqa: BLE001  采集异常不致命，回退页面/图片
            report.evidence.append(f"附件采集异常：{type(exc).__name__}: {str(exc)[:60]}")
        if acquired is not None:
            for a in acquired.found_assets:
                if a not in report.found_assets:
                    report.found_assets.append(a)
            document_urls = {document.source_url for document in acquired.documents}
            for document in acquired.documents:
                content_type = ""
                sha256 = ""
                size_bytes = 0
                kind = _asset_kind(document.source_url)
                try:
                    inspected = inspect_evidence_file(
                        document.raw_path,
                        max_bytes=20 * 1024 * 1024,
                        allowed_kinds={"xlsx", "xls"},
                    )
                    kind = inspected.kind
                    content_type = inspected.content_type
                    sha256 = inspected.sha256
                    size_bytes = inspected.size_bytes
                except Exception:  # noqa: BLE001 - parser success remains auditable in tests/legacy
                    pass
                _record_asset(report, EvidenceAssetRecord(
                    url=document.source_url,
                    parent_url=document.page_url,
                    label=document.label,
                    kind=kind,
                    status="parsed",
                    content_type=content_type,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    fetched_at=utc_now() if sha256 else "",
                    local_path=str(document.raw_path),
                    truncated=bool(document.grid.get("truncated", False)),
                    extraction_method="excel_grid",
                ))
            for asset_url in acquired.found_assets:
                if asset_url in document_urls:
                    continue
                acquisition_error = acquired.attachment_errors.get(asset_url, "")
                _record_asset(report, EvidenceAssetRecord(
                    url=asset_url,
                    parent_url=acquired.attachment_parent_urls.get(
                        asset_url, acquired.page_url
                    ),
                    kind=_asset_kind(asset_url),
                    status="failed" if acquisition_error else "discovered",
                    error_code=("ATTACHMENT_ACQUISITION_FAILED" if acquisition_error else ""),
                    error_message=acquisition_error,
                ))
            if not acquired.all_attachments_processed:
                report.flag("attachment_group_incomplete")
                report.evidence.append(
                    "附件组未完整处理："
                    f"发现 {len(acquired.discovered_attachment_urls)} 个，"
                    f"失败 {len(acquired.failed_attachment_urls)} 个，"
                    f"未处理 {len(acquired.unprocessed_attachment_urls)} 个"
                )
            n = len(acquired.source_urls)
            report.evidence.append(f"附件名单（{n} 份赛道/附件）：{'；'.join(acquired.source_urls[:3])}")
            extraction = _extract_grid(llm, award, year, acquired.grid, report.evidence, profile)
            if extraction.get("entries"):
                report.source_url, report.source_kind = acquired.source_url, "excel"
                report.confidence = "high"
                if n > 1:
                    report.flag("multi_source")
                    report.notes = f"官网名单合并自 {n} 份赛道/附件；"
                if use_corpus and acquired.all_attachments_processed:
                    # 仅完整附件组可进入参考库，避免将部分名单长期缓存为完整事实。
                    try:
                        corpus.save(resource_code, acquired.source_url, acquired.grid,
                                    raw_paths=acquired.raw_paths)
                        report.evidence.append("已回写参考库")
                    except Exception as exc:  # noqa: BLE001  回写失败不影响本次结论
                        report.evidence.append(f"回写参考库失败：{str(exc)[:60]}")

    # 路径②③：页面正文 / 图片（Excel 未取到时回退，逐 url 抓页）
    for url in urls:
        if extraction.get("entries"):  # 已有名单（参考库/附件），无需再抓页
            break
        report.evidence.append(f"访问页面：{url}")
        try:
            page = tools.fetch_page(url)
        except Exception as exc:  # 网络异常记录后试下一个 URL
            report.evidence.append(f"  抓取失败：{type(exc).__name__}: {exc}")
            continue
        if page.status != 200:
            report.evidence.append(f"  HTTP {page.status}，跳过")
            continue
        if page.text_truncated:
            report.flag("page_text_truncated")
            report.evidence.append(
                f"页面正文已截断：原始约 {page.original_text_chars} 字符，"
                f"当前只保留 {len(page.text)} 字符"
            )
        # 记录发现的附件与图片 URL（供人工直接打开核对）
        for att in page.attachments:
            if att.url not in report.found_assets:
                report.found_assets.append(att.url)
            _record_asset(report, EvidenceAssetRecord(
                url=att.url,
                parent_url=url,
                label=att.text,
                kind=_asset_kind(att.url),
            ))
        for img in page.images:
            if img not in report.found_assets:
                report.found_assets.append(img)
            _record_asset(report, EvidenceAssetRecord(
                url=img,
                parent_url=url,
                kind="image",
            ))

        # 路径②：页面正文
        if page.text.strip():
            extraction = _extract(llm, award, year, f"[页面正文]\n{page.text}", report.evidence)
            if extraction.get("page_is_target") and extraction.get("entries"):
                page_year = str(extraction.get("page_year", "") or "").strip()
                if year and page_year and page_year != year:
                    report.flag("cross_year_source_skipped")
                    report.evidence.append(
                        f"跳过跨年来源：页面年份 {page_year} != 目标年份 {year}"
                    )
                    extraction = {}
                    continue
                report.source_url, report.source_kind = url, "page"
                report.confidence = "high"
                break

        # 路径③：页面图片（视觉，低置信）。仅在明确启用视觉时尝试——多数国产/纯文本模型
        # （如 deepseek）不支持图片输入，喂进去只会 400 报错并浪费 token；未启用则图片 URL
        # 已记进 found_assets，转人工时会给出，让复核员直接打开核对。
        if _vision_enabled():
            for img_url in page.images[:3]:
                report.evidence.append(f"尝试图片名单：{img_url}")
                try:
                    local = tools.download_file(img_url, workdir)
                    media = "image/png" if local.suffix.lower() == ".png" else "image/jpeg"
                    extraction = llm.vision_json_call(
                        EXTRACT_SYSTEM,
                        f"奖项：{award}\n年份：{year}\n请从图片中的名单抽取。",
                        local.read_bytes(), media)
                except (LlmError, Exception) as exc:  # noqa: BLE001
                    _record_asset(report, EvidenceAssetRecord(
                        url=img_url,
                        parent_url=url,
                        kind="image",
                        status="failed",
                        error_code=type(exc).__name__,
                        error_message=str(exc)[:500],
                    ))
                    report.evidence.append(f"  图片抽取失败：{exc}")
                    continue
                if isinstance(extraction, dict) and extraction.get("entries"):
                    content_type = media
                    sha256 = ""
                    size_bytes = 0
                    try:
                        inspected = inspect_evidence_file(
                            local, max_bytes=20 * 1024 * 1024, allowed_kinds={"image"}
                        )
                        content_type = inspected.content_type
                        sha256 = inspected.sha256
                        size_bytes = inspected.size_bytes
                    except Exception:  # noqa: BLE001 - vision result is still retained for review
                        pass
                    _record_asset(report, EvidenceAssetRecord(
                        url=img_url,
                        parent_url=url,
                        kind="image",
                        status="parsed",
                        content_type=content_type,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        fetched_at=utc_now() if sha256 else "",
                        local_path=str(local),
                        extraction_method="vision_roster",
                    ))
                    report.source_url, report.source_kind = img_url, "image"
                    report.confidence = "low"
                    report.flag("image_source")
                    report.notes += "名单来自图片视觉抽取，置信度低，务必人工复核；"
                    break
        elif page.images:
            report.evidence.append(f"发现 {len(page.images)} 张名单图片，但未启用视觉模型，转人工（见检索网址）")
        if extraction.get("entries"):
            break

    # 无任何来源可用 → 转人工
    entries = extraction.get("entries") if isinstance(extraction, dict) else None
    if not entries or not isinstance(entries, list):
        report.verdict = "无法核对"
        lowered_assets = [item.casefold().split("?", 1)[0] for item in report.found_assets]
        if any(item.endswith(".pdf") for item in lowered_assets):
            report.flag("pdf_only")
            report.notes += "页面正文没有名单，已发现 PDF 附件，转 M5 深度解析"
        elif any(
            item.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            for item in lowered_assets
        ):
            report.flag("image_only")
            report.notes += "页面正文没有名单，已发现名单图片，转 M5 图片识别"
        else:
            report.flag("no_list")
            report.notes += "未能从页面/附件/图片获取名单，转人工"
        return report

    # 身份型（排名/认证）非结构化来源护栏：可靠来源是结构化名单；页面正文/图片抽取的大表易截断、
    # 不可审计——非 Excel 来源不硬比缺漏/多采，诚实转人工（守铁律）。参考库/附件命中为 excel，正常放行。
    if profile.kind != "roster" and report.source_kind != "excel":
        report.verdict = "无法核对"
        report.flag("identity_needs_excel")
        report.notes += (f"仅取到非结构化来源（{report.source_kind}），"
                         "排名/认证类大表非结构化抽取易截断不可审计，转人工；")
        return report

    # 年份判定
    report.page_year = str(extraction.get("page_year", "") or "")
    if report.page_year and year:
        report.year_match = report.page_year == year
    if report.year_match is False:
        report.verdict = "来源年份不符"
        report.flag("year_mismatch")
        report.notes += f"官网内容年份 {report.page_year} ≠ 文件年份 {year}，需人工确认来源；"
        return report

    # 按来源(赛道/附件)分组，为 ① 年度分片 + 年度安全网做准备。
    submitted_keys = set(submitted.keys())
    by_source: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        if isinstance(e, dict):
            by_source.setdefault(str(e.get("source", "")), []).append(e)

    # ① 年度分片（§7.5C）：来源名带年度标签（采集侧从附件名/详情页标题写入）时，优先选
    # "标签含提交年度"的来源比对；无标签来源留给下面的重叠安全网兜底；各来源都带标签却无一
    # 含提交年度 → 未取到对应年度名单，转人工。年度确定性抽取，不依赖时好时坏的中转站 LLM。
    src_years = {src: tools.extract_years(src) for src in by_source}
    if year and any(src_years.values()):
        matching = {s for s, ys in src_years.items() if year in ys}
        unlabeled = {s for s, ys in src_years.items() if not ys}
        if matching:
            selected = matching | unlabeled  # 年度命中 + 无标签者留待重叠兜底；他年度剔除
            dropped_yr = [s for s in by_source if s not in selected]
            by_source = {s: es for s, es in by_source.items() if s in selected}
            report.evidence.append(f"年度分片：按提交年度 {year} 选中来源 {sorted(matching)}"
                                   + (f"；剔除他年度来源 {dropped_yr}" if dropped_yr else ""))
            report.notes += f"已按年度标签锁定 {year} 年度来源；"
        elif not unlabeled:  # 各来源都带年度标签但无一匹配 → 未取到对应年度名单
            all_years = sorted({y for ys in src_years.values() for y in ys})
            report.verdict = "无法核对"
            report.flag("year_no_match")
            report.notes += (f"官网各来源年度标签（{all_years}）均不含提交年度 {year}，"
                             f"未取到对应年度名单，转人工；")
            report.evidence.append(
                f"年度分片：无来源匹配提交年度 {year}（来源年度 {all_years}），转人工")
            return report
        else:  # 有标签但都不匹配，仅无标签来源可用 → 交下面的重叠安全网兜底
            by_source = {s: es for s, es in by_source.items() if s in unlabeled}

    # 年度安全网（兜底）：无年度标签时，丢掉与提交"零重叠"的来源（跨届名单必与提交零重叠）。
    # 一条采集清单常捆多届（如 2024/2025）；全部来源都零重叠则来源存疑，转人工，绝不硬比出假缺漏。
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    multi_source = len(by_source) > 1
    for src, ents in by_source.items():
        keys = set(_entry_keys(ents, profile))
        if multi_source and keys and not (keys & submitted_keys):  # 该来源与提交零重叠 → 疑似跨届
            dropped.append(src or "(未命名来源)")
        else:
            kept.extend(ents)
    if dropped:
        report.evidence.append(f"年度安全网：剔除 {len(dropped)} 个与提交零重叠的来源（疑似跨届/错采）：{dropped[:3]}")
        report.notes += f"已剔除 {len(dropped)} 个与提交零重叠的官网来源（疑似跨届/错采）；"
        report.flag("cross_year_dropped")
    if multi_source and not kept:  # 全部来源都对不上 → 未取到对年度名单，转人工
        report.verdict = "无法核对"
        report.flag("zero_overlap")
        report.notes += "所有官网来源都与提交零重叠，疑似均为跨届或未取到对应年度名单，转人工；"
        return report

    # 确定性比对：官网条目 vs 提交行
    web_keys = _entry_keys(kept, profile)
    report.extracted_count = len(web_keys)

    # 全局零重叠（单来源也覆盖）→ 来源/年度存疑，转人工而非报假缺漏
    if report.extracted_count >= 3 and not (set(web_keys) & submitted_keys):
        report.verdict = "无法核对"
        report.flag("zero_overlap")
        report.notes += (f"官网名单与提交零重叠，疑似来源/年度不符，转人工"
                         f"（官网 {report.extracted_count} 条、提交 {len(submitted)} 行）；")
        return report

    missing = [disp for k, disp in web_keys.items() if k not in submitted]
    extra = [disp for k, disp in submitted.items() if k not in web_keys]
    report.missing = missing[:20]
    report.extra = extra[:20]
    # 折叠行 = 提交总行数 − 去重键数：被同键折叠、未能逐行独立核对的行。
    # 常是官网名单未覆盖的类别（如"优秀指导老师"在 PDF、却沿用本队队长/负责人名 → 被去重蒙进"一致"）。
    collapsed = report.submitted_count - len(submitted)
    if not missing and not extra:
        if collapsed > max(5, int(report.submitted_count * 0.02)):  # 折叠显著 → "一致"不可信，诚实降级
            report.verdict = "基本一致（需人工抽核）"
            report.confidence = "medium"
            report.flag("collapsed_rows")
            report.notes += (f"官网 {report.extracted_count} 条与提交去重键完全一致；但提交另有约 {collapsed} 行"
                             f"与他行同键、未能逐行独立核对（疑含官网名单未覆盖的类别，如指导教师奖/PDF-only 类别），"
                             f"建议人工抽核；")
        else:
            report.verdict = "一致"
    elif missing and not extra:
        report.verdict = "疑似缺漏"
    elif extra and not missing:
        report.verdict = "疑似多采"
    else:
        report.verdict = "疑似缺漏"
        report.notes += f"另有 {len(extra)} 条提交记录未在官网匹配到；"
    if isinstance(extraction.get("notes"), str) and extraction["notes"]:
        report.notes += extraction["notes"]
    # 页面来源、一致但年份未读出 → 未做年份交叉验证留痕（Excel 靠来源标签分片，不计此列）
    if report.verdict == "一致" and report.source_kind == "page" and not report.page_year:
        report.flag("year_unverified")
    unresolved_page_assets = [
        asset
        for asset in report.evidence_assets
        if asset.status != "parsed"
        and asset.kind in {"pdf", "document", "xls", "xlsx", "image", "unknown"}
    ]
    if report.source_kind == "page" and unresolved_page_assets:
        report.flag("unresolved_page_assets")
        report.verdict = "无法核对"
        report.confidence = "low"
        report.notes += (
            f"页面正文虽有匹配内容，但同页仍有 {len(unresolved_page_assets)} 个"
            "未解析附件或图片，不能据正文局部内容证明完整名单；"
        )
    if "attachment_group_incomplete" in report.reason_codes:
        report.verdict = "无法核对"
        report.confidence = "low"
        report.notes += "官网附件组存在失败或未处理项，当前名单只代表部分来源，已转 M5 补证；"
    if "page_text_truncated" in report.reason_codes:
        report.verdict = "无法核对"
        report.confidence = "low"
        report.notes += "官网正文超过抓取上限，当前名单只代表页面前段，已转 M5 补证；"
    if not submitted_identity_complete or not _reference_identity_complete(entries, profile):
        report.flag("identity_fields_unverified")
        report.verdict = "无法核对"
        report.confidence = "low"
        report.notes += (
            f"当前来源未完整抽取身份规则“{profile.label}”所需字段，"
            "现有命中仅作候选，已转 M5 补证；"
        )
    return report
