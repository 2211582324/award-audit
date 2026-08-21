"""M5 final offline evaluation tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _module() -> ModuleType:
    path = ROOT / "scripts" / "eval_m5.py"
    spec = importlib.util.spec_from_file_location("eval_m5", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_m5_passes_available_thresholds_without_external_calls(tmp_path) -> None:  # noqa: ANN001
    module = _module()
    result = module.evaluate(
        ROOT / "tests" / "data" / "m5_golden" / "results",
        ROOT / "tests" / "data" / "m5_golden" / "awards_10.json",
        tmp_path,
    )
    assert result["status"] == "complete"
    assert result["execution_boundary"] == {
        "network_calls": 0,
        "model_calls": 0,
        "real_probes_rerun": False,
        "input_policy": "redacted probe JSON plus controlled local scenarios only",
    }
    assert result["measured"]["domestic_official_recall_top5"]["value"] == 0.875
    assert result["measured"]["digital_pdf_field_recall"]["value"] == 1.0
    assert result["measured"]["structured_json_valid_rate"]["value"] == 1.0
    assert result["controlled"]["severe_false_passes"]["value"] == 0
    assert result["controlled"]["case_memory_top3"]["value"] >= 0.8
    assert result["controlled"]["budget_handoff"]["value"] == 1.0
    assert result["not_rerun"]["production_operational_sample"] == "insufficient_sample"
    assert result["operational_statistics"]["reflection_corrected_count"]["value"] is None


def test_eval_m5_marks_missing_probe_inputs_as_not_rerun(tmp_path) -> None:  # noqa: ANN001
    module = _module()
    result = module.evaluate(tmp_path / "missing", tmp_path / "gold.json", tmp_path)
    assert result["measured"]["domestic_official_recall_top5"]["source"] == "not_rerun"
    assert result["measured"]["scanned_vision_f1"]["passed"] is None
    assert result["controlled"]["severe_false_passes"]["passed"] is True
