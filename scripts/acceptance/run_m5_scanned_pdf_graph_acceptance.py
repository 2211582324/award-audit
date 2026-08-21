"""Run a controlled scanned-PDF acceptance through the real M5 Graph and API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from award_audit.agent.investigation import InvestigationAgent
from award_audit.agent.llm import LlmClient
from award_audit.agent.toolkit import build_default_registry
from award_audit.agent.toolkit.safety import inspect_evidence_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()

    run_root = args.run_root.resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    pdf_path = (ROOT / "tests/data/m5_golden/pdf/scanned_roster.pdf").resolve()
    inspection = inspect_evidence_file(
        pdf_path, max_bytes=20 * 1024 * 1024, allowed_kinds={"pdf"}
    )
    result = InvestigationAgent(
        LlmClient(),
        build_default_registry(),
        allowed_roots=[pdf_path.parent, run_root],
        planner_tool_names=(
            "extract_pdf_text",
            "render_pdf_pages",
            "ocr_image",
            "vision_extract_roster",
        ),
    ).run(
        case_id=1,
        objective=(
            "Read this controlled two-page scanned award roster. Execute every prepared "
            "batch in order, use local OCR before vision, and compare only after all 16 "
            "visible roster entries are structurally extracted."
        ),
        known_urls=["controlled-fixture:scanned-roster"],
        expected_record_count=16,
        asset_index=[{
            "asset_id": f"sha256:{inspection.sha256}",
            "kind": "pdf",
            "source_url": "controlled-fixture:scanned-roster",
            "local_path": str(pdf_path),
            "sha256": inspection.sha256,
            "readable": True,
            "page_count": 2,
        }],
    )
    observations = result.observations
    vision_pages = [
        page
        for observation in observations
        if observation.get("tool_name") == "vision_extract_roster"
        for page in observation.get("summary", {}).get("data", {}).get("pages", [])
    ]
    payload = {
        "status": result.status,
        "reason": result.reason,
        "action_batches": [
            action.get("prepared_batch_id", "") for action in result.actions
            if action.get("kind") == "tool"
        ],
        "tools": [observation.get("tool_name", "") for observation in observations],
        "vision_page_count": len(vision_pages),
        "vision_record_count": sum(len(page.get("entries", [])) for page in vision_pages),
        "vision_complete": bool(vision_pages) and all(
            page.get("all_rows_extracted", True)
            and not page.get("truncated", False)
            and not page.get("unreadable", [])
            for page in vision_pages
        ),
        "node_timeline": [
            {
                "node": event.get("node", ""),
                "duration_ms": event.get("duration_ms", 0),
                "transition_reason": event.get("transition_reason", ""),
            }
            for event in result.node_events
        ],
        "tool_trace": [
            {
                "tool_name": trace.get("tool_name", ""),
                "ok": trace.get("ok", False),
                "duration_ms": trace.get("duration_ms", 0),
                "error_code": trace.get("error_code", ""),
            }
            for trace in result.tool_trace
        ],
    }
    (run_root / "scan-acceptance.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if (
        result.status == "compare"
        and payload["vision_record_count"] == 16
        and payload["vision_complete"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
