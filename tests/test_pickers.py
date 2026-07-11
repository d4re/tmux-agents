from unittest.mock import MagicMock
import subprocess
import pytest
from tmux_agents import pickers


def test_pick_one_returns_iterfzf_choice(monkeypatch):
    import iterfzf

    calls = []

    def fake_iterfzf(items, *, prompt, **_):
        calls.append((list(items), prompt))
        return "two"

    monkeypatch.setattr(iterfzf, "iterfzf", fake_iterfzf)
    assert pickers.pick_one(["one", "two", "three"], prompt="pick> ") == "two"
    assert calls == [(["one", "two", "three"], "pick> ")]


def test_pick_one_returns_none_on_cancel(monkeypatch):
    import iterfzf

    monkeypatch.setattr(iterfzf, "iterfzf", lambda items, *, prompt, **_: None)
    assert pickers.pick_one(["a"], prompt="p> ") is None


def test_prompt_yes_no_default_true_orders_yes_first(monkeypatch):
    import iterfzf

    captured = {}

    def fake_iterfzf(items, *, prompt, **_):
        captured["items"] = list(items)
        return "yes"

    monkeypatch.setattr(iterfzf, "iterfzf", fake_iterfzf)
    assert pickers.prompt_yes_no("prune? > ", default=True) is True
    assert captured["items"] == ["yes", "no"]


def test_prompt_yes_no_default_false_orders_no_first(monkeypatch):
    import iterfzf

    captured = {}

    def fake_iterfzf(items, *, prompt, **_):
        captured["items"] = list(items)
        return "no"

    monkeypatch.setattr(iterfzf, "iterfzf", fake_iterfzf)
    assert pickers.prompt_yes_no("force? > ", default=False) is False
    assert captured["items"] == ["no", "yes"]


def test_prompt_yes_no_cancel_raises(monkeypatch):
    import iterfzf

    monkeypatch.setattr(iterfzf, "iterfzf", lambda items, *, prompt, **_: None)
    with pytest.raises(pickers.Cancelled):
        pickers.prompt_yes_no("p> ", default=True)


def _stub_fzf(monkeypatch, returncode, stdout):
    def fake_run(cmd, input=None, capture_output=False, text=False):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


# fzf --print-query stdout layout: <query line>\n<matched line if any>\n
# returncode 0 = match (validator skipped); returncode 1 = no match (validator runs).
@pytest.mark.parametrize(
    "candidates,fzf_returncode,fzf_stdout,expected,validator",
    [
        # Match: select an existing candidate (validator bypassed).
        (["foo", "bar"], 0, "foo\nfoo\n", "foo", None),
        # Typed brand-new value with no match (validator would have rejected " " but bypassed here? no, this is no-validator).
        (["foo", "bar"], 1, "brand-new\n", "brand-new", None),
        # Empty query with candidates: fzf default-highlights the first.
        (["[no branch]", "foo"], 0, "\n[no branch]\n", "[no branch]", None),
        # Empty query with no candidates: returns None.
        ([], 1, "\n", None, None),
        # Selecting a candidate bypasses the validator even when value would fail it.
        (
            ["weird name"],
            0,
            "weird name\nweird name\n",
            "weird name",
            lambda s: " " not in s,
        ),
    ],
)
def test_pick_or_create_single_pass(
    monkeypatch,
    candidates,
    fzf_returncode,
    fzf_stdout,
    expected,
    validator,
):
    _stub_fzf(monkeypatch, fzf_returncode, fzf_stdout)
    assert (
        pickers.pick_or_create(
            candidates,
            prompt="x> ",
            validator=validator,
        )
        == expected
    )


def test_pick_or_create_passes_candidates_as_stdin(monkeypatch):
    captured = {}

    def fake_run(cmd, input=None, capture_output=False, text=False):
        captured["input"] = input
        return MagicMock(returncode=0, stdout="foo\nfoo\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    pickers.pick_or_create(["foo", "bar"], prompt="branch> ")
    assert captured["input"] == "foo\nbar\n"


def test_pick_or_create_validator_rejects_typed_then_accepts(monkeypatch, capsys):
    responses = iter(
        [
            MagicMock(returncode=1, stdout="bad name\n", stderr=""),
            MagicMock(returncode=1, stdout="good-name\n", stderr=""),
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: next(responses))
    result = pickers.pick_or_create(
        ["foo"],
        prompt="name> ",
        validator=lambda s: " " not in s,
    )
    assert result == "good-name"
    err = capsys.readouterr().err
    assert "invalid input" in err and "bad name" in err


def test_pick_or_create_cancel_raises(monkeypatch):
    def fake_run(cmd, input=None, capture_output=False, text=False):
        return MagicMock(returncode=130, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(pickers.Cancelled):
        pickers.pick_or_create(["foo"], prompt="x> ")
