from pathlib import Path

import duckdb
import pytest

from src.data import load_raw, profile, relationships


def test_json_formats_load_alongside_csv(tmp_path: Path):
    files = {
        "accounts.json": '[{"account_id":1,"name":"North"},{"account_id":2,"name":"South"}]',
        "events.jsonl": '{"event_id":10,"account_id":1,"amount":12}\n{"event_id":11,"account_id":2,"amount":5}\n',
        "notes.ndjson": '{"note_id":1,"text":"first"}\n{"note_id":2,"text":"second"}\n',
        "settings.json": '{"setting_id":1,"enabled":true}',
        "groups.csv": "group_id,label\n1,A\n",
        "teams.csv": "team_id,label\n1,Core\n",
    }
    paths = []
    for filename, content in files.items():
        path = tmp_path / filename
        path.write_text(content)
        paths.append(path)

    database = tmp_path / "modellm.duckdb"
    names = load_raw(database, paths)
    profiles = profile(database, names)
    links = relationships(database, profiles)

    assert names == ["accounts", "events", "notes", "settings", "groups", "teams"]
    assert {name: item["rows"] for name, item in profiles.items()} == {
        "accounts": 2,
        "events": 2,
        "notes": 2,
        "settings": 1,
        "groups": 1,
        "teams": 1,
    }
    assert any(link["from"] == "events.account_id" and link["to"] == "accounts.account_id" for link in links)
    with duckdb.connect(str(database), read_only=True) as con:
        assert con.execute("SELECT sum(amount) FROM raw.events").fetchone()[0] == 17


def test_wrapped_record_array_has_clear_error(tmp_path: Path):
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text('{"data":[{"id":1},{"id":2}]}')
    other = tmp_path / "other.csv"
    other.write_text("id\n1\n")

    with pytest.raises(ValueError, match="wrapped record array"):
        load_raw(tmp_path / "modellm.duckdb", [wrapped, other])
