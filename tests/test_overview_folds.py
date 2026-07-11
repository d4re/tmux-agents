import json
from pathlib import Path

from tmux_agents import overview, paths, tmux


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_load_folds_missing_file_returns_empty(tmp_state_dir):
    assert overview.load_folds() == {}


def test_load_folds_malformed_json_returns_empty(tmp_state_dir):
    paths.folds_file().write_text("{not json")
    assert overview.load_folds() == {}


def test_load_folds_drops_non_string_keys_and_coerces_values(tmp_state_dir):
    paths.folds_file().write_text(json.dumps({"api": True, "web": "yes", "x": 0}))
    folds = overview.load_folds()
    assert folds == {"api": True, "web": True, "x": False}


def test_save_folds_round_trip(tmp_state_dir):
    overview.save_folds({"api": True, "web": False})
    assert _read_json(paths.folds_file()) == {"api": True, "web": False}
    assert overview.load_folds() == {"api": True, "web": False}


def test_gc_folds_drops_repos_not_in_window_list(monkeypatch, tmp_state_dir):
    monkeypatch.setattr(tmux, "list_windows", lambda s: [
        tmux.Window(id="@1", index=1, name="api:feat-x"),
    ])
    paths.folds_file().write_text(json.dumps({"api": True, "web": True, "infra": False}))
    folds = overview.load_folds_with_gc()
    assert folds == {"api": True}
    assert _read_json(paths.folds_file()) == {"api": True}
