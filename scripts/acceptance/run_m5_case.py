"""Run the production M5 path for one already M4-bound audit case."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from award_audit.agent.review_workflow import run_review_case


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--case-id", type=int, required=True)
    parser.add_argument("--evidence-root", type=Path, action="append", required=True)
    args = parser.parse_args()

    result = run_review_case(
        args.database.resolve(),
        [path.resolve(strict=False) for path in args.evidence_root],
        args.case_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
