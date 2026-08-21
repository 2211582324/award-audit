"""Offline M5.2 registry chain over the controlled scanned PDF."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from award_audit.agent.toolkit import (
    SafeToolExecutor,
    ToolBudgetLimits,
    ToolExecutionContext,
    build_default_registry,
)
from award_audit.agent.toolkit.pdf import resolve_poppler_command

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/data/m5_golden/pdf"
RENDERED = ROOT / "tests/data/m5_golden/results/pdf_ocr_pages"


@pytest.mark.skipif(not resolve_poppler_command("pdftoppm"), reason="Poppler unavailable")
def test_scanned_pdf_ocr_vision_compare_chain_is_bounded_and_traceable(
    tmp_path: Path,
) -> None:
    expected = json.loads(
        (GOLDEN / "scanned_roster.expected.json").read_text("utf-8")
    )

    class OcrEngine:
        def __call__(self, _path: str):  # noqa: ANN202
            return [
                [[[0, 0], [100, 0], [100, 20], [0, 20]], "受控 OCR 候选文本", 0.91]
            ], [0.01]

    class VisionClient:
        provider = "openai"
        model = "fake-vision"

        def __init__(self) -> None:
            self.page = 0

        def vision_json_call(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            self.page += 1
            entries = [
                {key: value for key, value in entry.items() if key != "page"}
                for entry in expected["entries"]
                if entry["page"] == self.page
            ]
            return {
                "page": self.page,
                "total_pages": 2,
                "is_roster_page": True,
                "headers": ["序号", "姓名", "单位", "等级"],
                "entries": entries,
                "first_no": entries[0]["no"],
                "last_no": entries[-1]["no"],
                "truncated": False,
                "unreadable": [],
                "confidence": 0.99,
            }

    client = VisionClient()
    registry = build_default_registry(
        ocr_engine_factory=OcrEngine,
        vision_client_factory=lambda: client,
    )
    executor = SafeToolExecutor(registry)
    limits = ToolBudgetLimits(max_calls=6)
    context = ToolExecutionContext.create([GOLDEN, tmp_path], limits)
    pdf_path = GOLDEN / "scanned_roster.pdf"

    inspected = executor.execute("inspect_pdf", {"path": str(pdf_path)}, context)
    rendered = executor.execute(
        "render_pdf_pages",
        {
            "path": str(pdf_path),
            "pages": [1, 2],
            "output_dir": str(tmp_path / "pages"),
            "dpi": 150,
        },
        context,
    )
    image_refs = [
        {"path": page["path"], "page": page["page"], "total_pages": 2}
        for page in rendered.data["pages"]
    ]
    ocr = executor.execute("ocr_image", {"images": image_refs}, context)
    vision = executor.execute("vision_extract_roster", {"images": image_refs}, context)
    submitted = [
        {key: value for key, value in entry.items() if key != "page"}
        for entry in expected["entries"]
    ]
    compared = executor.execute(
        "compare_roster",
        {
            "submitted": submitted,
            "official_pages": vision.data["pages"],
            "expected_total": expected["total"],
        },
        context,
    )

    assert inspected.ok and inspected.data["scan_candidate_pages"] == [1, 2]
    assert rendered.ok and len(rendered.artifacts) == 2
    assert ocr.ok and len(ocr.data["pages"]) == 2
    assert vision.ok and client.page == 2
    assert compared.ok and compared.data["consistent"] is True
    assert context.budget.calls == 5
    assert context.budget.pdf_pages == 2
    assert context.budget.rendered_pages == 2
    assert context.budget.ocr_pages == 2
    assert context.budget.vision_pages == 2
    trace = json.dumps([item.model_dump(mode="json") for item in context.trace], ensure_ascii=False)
    assert submitted[0]["name"] not in trace and submitted[0]["org"] not in trace


@pytest.mark.skipif(
    importlib.util.find_spec("rapidocr_onnxruntime") is None,
    reason="RapidOCR optional dependency unavailable",
)
def test_default_rapidocr_backend_runs_in_isolated_process() -> None:
    image_path = RENDERED / "scanned_roster-1.png"
    executor = SafeToolExecutor(build_default_registry())
    context = ToolExecutionContext.create([RENDERED], ToolBudgetLimits(max_calls=2))
    result = executor.execute(
        "ocr_image",
        {"images": [{"path": str(image_path), "page": 1, "total_pages": 2}]},
        context,
    )
    assert result.ok and result.data["backend"] == "rapidocr"
    assert result.data["pages"][0]["lines"]
    assert context.budget.ocr_pages == 1
