from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from award_audit.agent.review_agent import readers as readers_module
from award_audit.agent.review_agent.models import ParsedAsset
from award_audit.agent.review_agent.readers import M4AssetReader
from award_audit.agent.review_agent.service import ReviewMaterialRequest
from award_audit.agent.toolkit import image as image_tools
from award_audit.agent.toolkit import pdf as pdf_tools
from award_audit.agent.toolkit.contracts import EvidenceAssetRecord
from award_audit.agent.toolkit.safety import inspect_evidence_file


def _request() -> ReviewMaterialRequest:
    return ReviewMaterialRequest(
        asset_id="asset-1",
        subunit_id="document",
        content_kind="spreadsheet_sheet",
        reason="确认名单表头和样例。",
    )


def _parsed() -> ParsedAsset:
    return ParsedAsset(
        asset_id="asset-1",
        source_url="https://official.example/list.xlsx",
        kind="xlsx",
        status="parsed",
    )


def test_reader_validates_m4_spreadsheet_and_returns_bounded_sheet_excerpt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "list.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "获奖名单"
    sheet.append(["作品名称", "单位名称"])
    sheet.append(["项目甲", "甲大学"])
    workbook.save(path)
    inspection = inspect_evidence_file(
        path,
        max_bytes=20 * 1024 * 1024,
        allowed_kinds={"xlsx", "xls"},
    )
    asset = EvidenceAssetRecord(
        url="https://official.example/list.xlsx",
        kind="xlsx",
        status="parsed",
        local_path=str(path),
        sha256=inspection.sha256,
    )

    excerpt = M4AssetReader(
        {"asset-1": asset}, allowed_roots=[tmp_path]
    ).read(_request(), _parsed())

    assert excerpt.blocker == ""
    assert "作品名称 | 单位名称" in excerpt.content
    assert excerpt.anchors == ["获奖名单!A1:AD2"]


def test_reader_rejects_changed_file_after_m4_hash(tmp_path: Path) -> None:
    path = tmp_path / "list.xlsx"
    path.write_bytes(b"not an xlsx")
    asset = EvidenceAssetRecord(
        url="https://official.example/list.xlsx",
        kind="xlsx",
        status="parsed",
        local_path=str(path),
        sha256="a" * 64,
    )

    excerpt = M4AssetReader(
        {"asset-1": asset}, allowed_roots=[tmp_path]
    ).read(_request(), _parsed())

    assert "spreadsheet reader rejected asset" in excerpt.blocker


def test_reader_ocrs_scan_pdf_when_digital_text_is_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    source = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "scanned_roster.pdf"
    inspection = inspect_evidence_file(
        source,
        max_bytes=20 * 1024 * 1024,
        allowed_kinds={"pdf"},
    )
    asset = EvidenceAssetRecord(
        url="https://official.example/scan.pdf",
        kind="pdf",
        status="parsed",
        local_path=str(source),
        sha256=inspection.sha256,
    )
    image_path = tmp_path / "derived.png"
    image_path.write_bytes(b"placeholder")
    pdf_report = pdf_tools.PdfInspection(
        page_count=1,
        pages=[pdf_tools.PdfPageInspection(
            page=1,
            width_points=612,
            height_points=792,
            text_chars=0,
            text_presence_ratio=0,
            mode="scan",
        )],
        digital_pages=[],
        scan_candidate_pages=[1],
    )
    monkeypatch.setattr(
        readers_module.pdf_tools,
        "inspect_pdf",
        lambda *_args, **_kwargs: pdf_report,
    )
    monkeypatch.setattr(
        readers_module.pdf_tools,
        "extract_pdf_text",
        lambda *_args, **_kwargs: [pdf_tools.PdfTextPage(
            page=1, text="", text_chars=0, table_rows=0,
        )],
    )
    monkeypatch.setattr(
        readers_module.pdf_tools,
        "render_pdf_pages",
        lambda *_args, **_kwargs: [pdf_tools.RenderedPdfPage(
            page=1,
            path=image_path,
            width=100,
            height=100,
            pixels=10_000,
            dpi=150,
            content_type="image/png",
            sha256="a" * 64,
            size_bytes=1,
            latency_ms=1,
        )],
    )
    monkeypatch.setattr(
        readers_module.image_tools,
        "run_rapid_ocr",
        lambda *_args, **_kwargs: [image_tools.OcrPage(
            page=1,
            path=image_path,
            text="recognized roster text",
            lines=[],
            average_confidence=0.99,
            detected_numbers=[],
            sequence_contiguous=False,
            needs_vision=True,
            image_sha256="a" * 64,
            pixels=10_000,
        )],
    )
    request = ReviewMaterialRequest(
        asset_id="pdf-1",
        subunit_id="document",
        content_kind="pdf_section",
        reason="read scan",
    )
    parsed = ParsedAsset(
        asset_id="pdf-1",
        source_url=asset.url,
        kind="pdf",
        status="parsed",
    )

    excerpt = M4AssetReader(
        {"pdf-1": asset}, allowed_roots=[source.parent]
    ).read(request, parsed)

    assert excerpt.blocker == ""
    assert excerpt.content == "[page 1 OCR]\nrecognized roster text"
    assert excerpt.anchors == ["page:1:ocr"]


def test_reader_uses_m4_page_metadata_for_large_pdf(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    source = Path(__file__).parents[1] / "data" / "m5_golden" / "pdf" / "mixed_roster.pdf"
    inspection = inspect_evidence_file(
        source,
        max_bytes=20 * 1024 * 1024,
        allowed_kinds={"pdf"},
    )
    asset = EvidenceAssetRecord(
        url="https://official.example/large.pdf",
        kind="pdf",
        status="parsed",
        local_path=str(source),
        sha256=inspection.sha256,
        metadata={"page_count": 177, "digital_pages": [1, 2, 3, 4, 5]},
    )
    monkeypatch.setattr(
        readers_module.pdf_tools,
        "inspect_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Reader must reuse M4 page metadata")
        ),
    )
    monkeypatch.setattr(
        readers_module.pdf_tools,
        "extract_pdf_text",
        lambda _path, pages, **_kwargs: [
            pdf_tools.PdfTextPage(
                page=page, text=f"page {page}", text_chars=6, table_rows=0,
            )
            for page in pages
        ],
    )
    request = ReviewMaterialRequest(
        asset_id="pdf-1",
        subunit_id="document",
        content_kind="pdf_section",
        reason="read bounded large PDF excerpt",
    )
    parsed = ParsedAsset(
        asset_id="pdf-1",
        source_url=asset.url,
        kind="pdf",
        status="parsed",
    )

    excerpt = M4AssetReader(
        {"pdf-1": asset}, allowed_roots=[source.parent]
    ).read(request, parsed)

    assert excerpt.blocker == ""
    assert excerpt.anchors == ["page:1", "page:2", "page:3", "page:4", "page:5"]
