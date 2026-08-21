from pathlib import Path

from scripts.probe_m5_security import run_probe


def test_p5_offline_security_probe(tmp_path: Path) -> None:
    result = run_probe(tmp_path)

    assert result["status"] == "complete"
    assert result["network_calls"] == 0
    assert result["model_calls"] == 0
    assert all(result["checks"].values())
