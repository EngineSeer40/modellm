from pathlib import Path

import duckdb

from src.demo import REQUEST, write_demo
from src.workflow import run


def test_deterministic_vertical_slice(tmp_path: Path, monkeypatch):
    paths = write_demo(tmp_path / "input")
    monkeypatch.chdir(tmp_path)
    result = run(paths, REQUEST, Path("runs"), demo=True)
    assert result["status"] == "complete", result.get("error")
    assert result["repair_attempts"] == 0
    assert result["metrics"]["Revenue"] == 515.0
    assert result["metrics"]["Orders"] == 4
    assert len(result["analysis"]["views"]) <= 4
    assert len(list(Path(result["run_dir"]).glob("chart_*.json"))) == 4
    with duckdb.connect(str(Path(result["run_dir"]) / "analytics.duckdb"), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM raw.order_items").fetchone()[0] == 8
        assert con.execute("SELECT count(*) FROM main.fact_order_items").fetchone()[0] == 5
        assert con.execute("SELECT count(*) FROM main.fact_order_items WHERE customer_key = 'unknown'").fetchone()[0] == 1
    assert all(item["ok"] for item in result["dbt_runs"])
    assert any("orphan" in warning for warning in result["quality_warnings"])
