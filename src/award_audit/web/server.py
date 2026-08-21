"""Local uvicorn entry point for the M5.6 review console."""

from __future__ import annotations

import argparse
from pathlib import Path

from award_audit.core import config
from award_audit.web.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("award_audit.db"))
    parser.add_argument("--evidence-root", type=Path, action="append", default=[])
    parser.add_argument("--import-root", type=Path, action="append", default=[])
    parser.add_argument(
        "--environment",
        choices=("development", "acceptance", "production"),
        default="development",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("M5.6 first version only permits loopback hosts")
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install the web extras: pip install -e .[web]") from exc
    # Load secrets before the worker accepts jobs. Agent threads then reuse the
    # process environment instead of reopening a protected .env during M5.
    config.load_env()
    project_root = Path(__file__).resolve().parents[3]
    import_roots = args.import_root or [project_root / "out" / "imports"]
    app = create_app(
        args.db,
        evidence_roots=args.evidence_root or [project_root / "out"],
        import_roots=import_roots,
        static_dir=project_root / "webui" / "dist",
        environment=args.environment,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
