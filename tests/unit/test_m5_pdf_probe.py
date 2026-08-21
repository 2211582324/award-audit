"""Offline tests for the M5 P3 PDF/OCR probe facility."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gen_m5_pdf_samples as generator  # noqa: E402
import probe_m5_pdf_ocr as probe  # noqa: E402


def _expected() -> dict:  # noqa: ANN202
    return {
        "page_count": 2,
        "page_modes": ["digital", "scan"],
        "entries": [
            {"no": 1, "name": "张伟明", "org": "清华大学", "level": "一等奖", "page": 1},
            {"no": 2, "name": "李思远", "org": "北京大学", "level": "二等奖", "page": 2},
        ],
    }


def test_score_text_is_whitespace_and_punctuation_tolerant() -> None:
    score = probe._score_text("张 伟 明｜清华大学｜一等奖", _expected(), page=1)
    assert score["row_recall"] == 1.0 and score["field_recall"] == 1.0


def test_score_entries_uses_name_and_org_pair() -> None:
    predicted = {"entries": [
        {"no": 1, "name": "张伟明", "org": "清华大学", "level": "一等奖"},
        {"no": 99, "name": "错误姓名", "org": "北京大学", "level": "二等奖"},
    ]}
    score = probe._score_entries(predicted, _expected(), page=1)
    assert score["true_positive"] == 1 and score["f1"] == 0.667
    assert score["sequence_recall"] == 1.0 and score["coverage_complete"] is False


def test_scan_detection_is_page_level() -> None:
    extraction = {"ok": True, "pages": ["张伟明 清华大学 一等奖 " * 4, ""]}
    detected = probe._scan_detection([extraction], _expected())
    assert detected["predicted_modes"] == ["digital", "scan"]
    assert detected["accuracy"] == 1.0


def test_public_extraction_never_contains_raw_text() -> None:
    public = probe._public_extraction(
        {"ok": True, "pages": ["张伟明 清华大学"], "latency_ms": 1}, _expected()
    )
    payload = json.dumps(public, ensure_ascii=False)
    assert "pages" not in public and "张伟明" not in payload and public["page_text_chars"] == [7]


def test_ocr_selection_requires_a_chinese_backend() -> None:
    args = probe._parser().parse_args(["--ocr", "auto"])
    inventory = {
        "commands": {"tesseract": True},
        "modules": {"rapidocr_onnxruntime": False},
        "tesseract_languages": ["eng"],
    }
    backend, reason = probe._choose_ocr(args, inventory)
    assert backend == "unavailable" and "中文 OCR" in reason


def test_vision_path_with_fake_client(tmp_path) -> None:  # noqa: ANN001
    expected = _expected()
    (tmp_path / "scanned_roster.expected.json").write_text(
        json.dumps(expected, ensure_ascii=False), "utf-8"
    )
    images = [tmp_path / "page-1.png", tmp_path / "page-2.png"]
    for image in images:
        image.write_bytes(b"controlled-image")
    local = [{"sample": "scanned_roster", "render": {"pages": [str(p) for p in images]}}]

    class FakeClient:
        provider = "openai"
        model = "fake-vision"

        def __init__(self) -> None:
            self.calls = 0

        def vision_json_call(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            self.calls += 1
            entry = expected["entries"][self.calls - 1]
            return {"entries": [entry], "truncated": False, "unreadable": []}

    client = FakeClient()
    args = probe._parser().parse_args(["--vision", "--vision-max-pages", "2"])
    result = probe._vision_pages(local, tmp_path, args, client_factory=lambda: client)
    assert result["status"] == "complete" and result["average_f1"] == 1.0
    assert client.calls == 2 and all(page["json_valid"] for page in result["pages"])


def test_summary_requires_requested_vision_to_meet_quality_gate() -> None:
    shared = {"scan_detection": {"accuracy": 1.0}, "render": {"ok": True}}
    records = [
        {
            "sample": "digital_roster",
            **shared,
            "best_extraction": {
                "extractor": "pypdf",
                "score": {"row_recall": 1.0, "sequence_recall": 1.0},
            },
            "cross_page": {"complete": True},
            "ocr": {"status": "complete", "combined": {}},
        },
        {
            "sample": "scanned_roster",
            **shared,
            "ocr": {
                "status": "complete",
                "backend": "rapidocr",
                "combined": {
                    "row_recall": 1.0,
                    "sequence_recall": 0.938,
                    "coverage_complete": False,
                },
            },
        },
        {"sample": "mixed_roster", **shared, "ocr": {"status": "complete"}},
    ]
    vision = {
        "status": "complete",
        "average_f1": 1.0,
        "pages": [{"score": {"coverage_complete": True}}],
    }
    assert probe._summary(records, None)["status"] == "local_complete"
    assert probe._summary(records, vision)["status"] == "complete"
    vision["pages"][0]["score"]["coverage_complete"] = False
    assert probe._summary(records, vision)["status"] == "partial"


@pytest.mark.skipif(
    not all(importlib.util.find_spec(name) for name in ("reportlab", "pypdf", "pdfplumber")),
    reason="optional probe-pdf dependencies are not installed",
)
def test_generated_samples_run_through_local_probe(tmp_path) -> None:
    samples = tmp_path / "samples"
    render = tmp_path / "render"
    manifest = generator.generate(samples)
    assert len(manifest["samples"]) == 3
    args = probe._parser().parse_args([
        "--local-only", "--ocr", "none", "--samples-dir", str(samples),
        "--render-dir", str(render), "--output", str(tmp_path / "result.json"),
    ])
    result = probe.run(args)
    assert result["contains_raw_text"] is False
    assert result["summary"]["digital_row_recall"] == 1.0
    assert result["summary"]["digital_sequence_recall"] == 1.0
    assert result["summary"]["cross_page_complete"] is True
    assert result["summary"]["scan_detection_accuracy"] == 1.0
    assert result["summary"]["render_success"] is True
    assert [record["scan_detection"]["predicted_modes"] for record in result["records"]] == [
        ["digital", "digital"], ["scan", "scan"], ["digital", "scan"]
    ]
