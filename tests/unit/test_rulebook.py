from __future__ import annotations

import json

import pytest

from scaletrace.rules import RulebookError, load_config, load_rulebook, ConfigError
from tests.conftest import REPO_ROOT

CORE_RULES = {"DB-001", "DB-002", "DB-003", "CMP-001", "CMP-002", "MEM-001", "MEM-002", "EXT-001"}
TEAM_RULEBOOK = REPO_ROOT / "02_initial_rules" / "ScaleTrace_rulebook_v0.1.json"


def test_bundled_rulebook_loads():
    rulebook = load_rulebook()
    assert rulebook.rulebook_version == "0.1.0"
    assert len(rulebook.rules) == 16
    assert {r.id for r in rulebook.rules if r.status == "core"} == CORE_RULES


@pytest.mark.skipif(not TEAM_RULEBOOK.exists(), reason="team rulebook folder not present")
def test_bundled_copy_matches_team_rulebook():
    bundled = load_rulebook().model_dump()
    team = load_rulebook(TEAM_RULEBOOK).model_dump()
    assert bundled == team


def _write_rulebook(tmp_path, mutate):
    data = load_rulebook().model_dump(mode="json")
    mutate(data)
    path = tmp_path / "rulebook.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_duplicate_rule_ids_rejected(tmp_path):
    path = _write_rulebook(tmp_path, lambda d: d["rules"].append(dict(d["rules"][0])))
    with pytest.raises(RulebookError, match="duplicate rule id"):
        load_rulebook(path)


def test_unknown_status_value_rejected(tmp_path):
    path = _write_rulebook(tmp_path, lambda d: d["implementation_contract"]["status_values"].append("confirmed"))
    with pytest.raises(RulebookError, match="status_values"):
        load_rulebook(path)


def test_unknown_severity_rejected(tmp_path):
    def mutate(d):
        d["rules"][0]["severity"] = "critical"

    with pytest.raises(RulebookError):
        load_rulebook(_write_rulebook(tmp_path, mutate))


def test_missing_rulebook_file(tmp_path):
    with pytest.raises(RulebookError, match="cannot read"):
        load_rulebook(tmp_path / "nope.json")


def test_example_config_loads():
    config = load_config(REPO_ROOT / "examples" / "scaletrace_config.example.json")
    assert config.bounded_loop_max_iterations == 10


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"not_a_setting": 1}', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
