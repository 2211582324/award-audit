"""Safe, bounded local readers for M4-discovered evidence assets."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from award_audit.agent.review_agent.models import ParsedAsset
from award_audit.agent.review_agent.service import (
    AssetExcerpt,
    ReviewMaterialRequest,
)
from award_audit.agent.toolkit import image as image_tools
from award_audit.agent.toolkit import pdf as pdf_tools
from award_audit.agent.toolkit.contracts import EvidenceAssetRecord
from award_audit.agent.toolkit.safety import inspect_evidence_file, validate_local_path
from award_audit.agent.toolkit.spreadsheet import parse_award_excel


class M4AssetReader:
    """Expose bounded excerpts from assets explicitly discovered by M4 only."""

    def __init__(
        self,
        assets_by_id: Mapping[str, EvidenceAssetRecord],
        *,
        allowed_roots: Sequence[str | Path],
    ) -> None:
        self._assets_by_id = dict(assets_by_id)
        self._allowed_roots = tuple(allowed_roots)

    def read(self, request: ReviewMaterialRequest, asset: ParsedAsset) -> AssetExcerpt:
        source = self._assets_by_id.get(asset.asset_id)
        if source is None:
            return self._blocked(request, asset, "asset is not part of the M4 asset index")
        if request.content_kind == "html_section":
            return self._read_html(request, asset, source)
        if request.content_kind == "pdf_section":
            return self._read_pdf(request, asset, source)
        if request.content_kind != "spreadsheet_sheet":
            return self._blocked(request, asset, "requested asset adapter is not available")
        if source.kind not in {"xlsx", "xls"}:
            return self._blocked(request, asset, "asset is not a spreadsheet")
        if not source.local_path:
            return self._blocked(request, asset, "M4 spreadsheet has no local evidence path")
        try:
            path = validate_local_path(
                source.local_path,
                self._allowed_roots,
                file_only=True,
            )
            inspection = inspect_evidence_file(
                path,
                max_bytes=20 * 1024 * 1024,
                allowed_kinds={"xlsx", "xls"},
            )
            if source.sha256 and inspection.sha256 != source.sha256:
                return self._blocked(request, asset, "M4 spreadsheet hash no longer matches")
            grid = parse_award_excel(path, max_rows=100)
        except Exception as exc:  # noqa: BLE001 - reader errors must remain non-executable facts.
            return self._blocked(
                request,
                asset,
                f"spreadsheet reader rejected asset: {type(exc).__name__}",
            )
        return self._spreadsheet_excerpt(request, asset, grid)

    def _read_html(
        self,
        request: ReviewMaterialRequest,
        asset: ParsedAsset,
        source: EvidenceAssetRecord,
    ) -> AssetExcerpt:
        if source.kind not in {"html", "htm", "web_page"} or not source.local_path:
            return self._blocked(request, asset, "M4 HTML has no local evidence path")
        try:
            path = validate_local_path(source.local_path, self._allowed_roots, file_only=True)
            if path.suffix.casefold() != ".txt":
                raise ValueError("persisted HTML evidence must be UTF-8 text")
            payload = path.read_bytes()
            if not payload or len(payload) > 512 * 1024:
                raise ValueError("persisted HTML evidence is empty or exceeds the reader limit")
            if source.sha256 and hashlib.sha256(payload).hexdigest() != source.sha256:
                raise ValueError("M4 HTML hash no longer matches")
            content = payload.decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - reader errors must remain non-executable facts.
            return self._blocked(request, asset, f"HTML reader rejected asset: {type(exc).__name__}")
        return AssetExcerpt(
            asset_id=asset.asset_id,
            subunit_id=request.subunit_id,
            content_kind=request.content_kind,
            content=content[:8000],
            anchors=asset.anchors,
            blocker="" if content else "HTML page contains no readable text",
        )

    def _read_pdf(
        self,
        request: ReviewMaterialRequest,
        asset: ParsedAsset,
        source: EvidenceAssetRecord,
    ) -> AssetExcerpt:
        if source.kind != "pdf" or not source.local_path:
            return self._blocked(request, asset, "M4 PDF has no local evidence path")
        try:
            path = validate_local_path(source.local_path, self._allowed_roots, file_only=True)
            inspection = inspect_evidence_file(
                path, max_bytes=20 * 1024 * 1024, allowed_kinds={"pdf"}
            )
            if source.sha256 and inspection.sha256 != source.sha256:
                return self._blocked(request, asset, "M4 PDF hash no longer matches")
            metadata = source.metadata if isinstance(source.metadata, Mapping) else {}
            m4_page_count = int(metadata.get("page_count", 0) or 0)
            if m4_page_count:
                pages = list(range(1, min(m4_page_count, 5) + 1))
                scan_pages = [
                    int(page) for page in metadata.get("scan_candidate_pages", [])[:5]
                    if isinstance(page, int) and page > 0
                ]
                total_pages = m4_page_count
            else:
                report = pdf_tools.inspect_pdf(path, max_pages=5)
                pages = list(range(1, min(report.page_count, 5) + 1))
                scan_pages = report.scan_candidate_pages[:5]
                total_pages = report.page_count
            extracted = pdf_tools.extract_pdf_text(path, pages, max_pages=5)
        except Exception as exc:  # noqa: BLE001 - reader errors must remain facts.
            return self._blocked(request, asset, f"PDF reader rejected asset: {type(exc).__name__}")
        content = "\n\n".join(
            f"[page {page.page}]\n{page.text}" for page in extracted
        )[:8000]
        anchors = [f"page:{page.page}" for page in extracted]
        if not any(page.text.strip() for page in extracted) and scan_pages:
            rendered = pdf_tools.render_pdf_pages(
                path,
                scan_pages,
                path.parent / "derived",
                max_pages=5,
                max_pixels_per_page=image_tools.MAX_IMAGE_PIXELS,
            )
            ocr_pages = image_tools.run_rapid_ocr(
                [
                    image_tools.ImagePageRef(
                        path=page.path,
                        page=page.page,
                        total_pages=total_pages,
                    )
                    for page in rendered
                ],
                max_bytes=20 * 1024 * 1024,
                max_pixels=image_tools.MAX_IMAGE_PIXELS,
            )
            content = "\n\n".join(
                f"[page {page.page} OCR]\n{page.text}" for page in ocr_pages
            )[:8000]
            anchors = [f"page:{page.page}:ocr" for page in ocr_pages]
        return AssetExcerpt(
            asset_id=asset.asset_id,
            subunit_id=request.subunit_id,
            content_kind=request.content_kind,
            content=content,
            anchors=anchors,
            blocker="" if content else "PDF pages contain no readable text",
        )

    @staticmethod
    def _blocked(
        request: ReviewMaterialRequest,
        asset: ParsedAsset,
        blocker: str,
    ) -> AssetExcerpt:
        return AssetExcerpt(
            asset_id=asset.asset_id,
            subunit_id=request.subunit_id,
            content_kind=request.content_kind,
            blocker=blocker,
        )

    @staticmethod
    def _spreadsheet_excerpt(
        request: ReviewMaterialRequest,
        asset: ParsedAsset,
        grid: Mapping[str, object],
    ) -> AssetExcerpt:
        excerpts: list[str] = []
        anchors: list[str] = []
        raw_sheets = grid.get("sheet_grids", [])
        for raw_sheet in raw_sheets if isinstance(raw_sheets, list) else []:
            if not isinstance(raw_sheet, Mapping):
                continue
            sheet_name = str(raw_sheet.get("sheet", "") or "Sheet")
            rows = raw_sheet.get("rows", [])
            if not isinstance(rows, list):
                continue
            rendered_rows = [
                " | ".join(str(cell or "")[:300] for cell in row[:30])
                for row in rows[:20]
                if isinstance(row, list)
            ]
            if rendered_rows:
                excerpts.append(f"[{sheet_name}]\n" + "\n".join(rendered_rows))
                anchors.append(f"{sheet_name}!A1:AD{min(len(rows), 20)}")
        content = "\n\n".join(excerpts)[:8000]
        blocker = "" if content else "spreadsheet contains no readable sheet rows"
        return AssetExcerpt(
            asset_id=asset.asset_id,
            subunit_id=request.subunit_id,
            content_kind=request.content_kind,
            content=content,
            anchors=anchors[:50],
            blocker=blocker,
        )
