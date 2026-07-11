import logging

from tmux_agents import theme, state


def test_dark_defaults_cover_all_states():
    assert set(theme.DARK_DEFAULTS) == {
        state.RUNNING, state.WAITING, state.IDLE,
        state.BACKGROUND, state.SLEEPING, state.ERRORED, state.STARTING,
    }
    for v in theme.DARK_DEFAULTS.values():
        assert v.startswith("#") and len(v) == 7


def test_light_defaults_cover_all_states():
    assert set(theme.LIGHT_DEFAULTS) == {
        state.RUNNING, state.WAITING, state.IDLE,
        state.BACKGROUND, state.SLEEPING, state.ERRORED, state.STARTING,
    }
    for v in theme.LIGHT_DEFAULTS.values():
        assert v.startswith("#") and len(v) == 7


def test_palette_derives_ansi_fg_as_truecolor_escape():
    p = theme.Palette.from_hex(theme.DARK_DEFAULTS)
    assert p.ansi_fg[state.RUNNING] == "\x1b[38;2;135;175;95m"
    assert p.ansi_fg[state.ERRORED] == "\x1b[38;2;255;95;95m"


def test_palette_derives_ansi_bg_as_truecolor_escape():
    p = theme.Palette.from_hex(theme.DARK_DEFAULTS)
    assert p.ansi_bg[state.RUNNING] == "\x1b[48;2;135;175;95m"


def test_palette_keeps_raw_hex_for_tmux_format():
    p = theme.Palette.from_hex(theme.DARK_DEFAULTS)
    assert p.fg[state.IDLE] == "#5fafff"


def test_palette_contrast_fg_is_black_for_light_colors_white_for_dark():
    p = theme.Palette.from_hex(theme.DARK_DEFAULTS)
    assert p.selected_fg[state.WAITING] == "#000000"
    assert p.selected_fg[state.ERRORED] == "#ffffff"
    assert p.ansi_selected_fg[state.WAITING] == "\x1b[38;2;0;0;0m"
    assert p.ansi_selected_fg[state.ERRORED] == "\x1b[38;2;255;255;255m"


def test_palette_light_defaults_all_get_white_contrast_fg():
    p = theme.Palette.from_hex(theme.LIGHT_DEFAULTS)
    for code in theme.LIGHT_DEFAULTS:
        assert p.selected_fg[code] == "#ffffff"


def test_load_returns_dark_defaults_when_file_missing(tmp_config_dir):
    p = theme.load()
    assert p.fg == theme.DARK_DEFAULTS


def test_load_light_mode(tmp_config_dir):
    (tmp_config_dir / "theme.toml").write_text('mode = "light"\n')
    p = theme.load()
    assert p.fg == theme.LIGHT_DEFAULTS


def test_load_explicit_dark_mode(tmp_config_dir):
    (tmp_config_dir / "theme.toml").write_text('mode = "dark"\n')
    p = theme.load()
    assert p.fg == theme.DARK_DEFAULTS


def test_load_unknown_mode_falls_back_to_dark_with_warning(tmp_config_dir, caplog):
    (tmp_config_dir / "theme.toml").write_text('mode = "purple"\n')
    with caplog.at_level(logging.WARNING, logger="tmux_agents.theme"):
        p = theme.load()
    assert p.fg == theme.DARK_DEFAULTS
    messages = " ".join(r.message for r in caplog.records)
    assert "mode" in messages and "purple" in messages


def test_load_missing_mode_defaults_to_dark(tmp_config_dir):
    (tmp_config_dir / "theme.toml").write_text("")
    p = theme.load()
    assert p.fg == theme.DARK_DEFAULTS


def test_load_merges_color_overrides(tmp_config_dir):
    (tmp_config_dir / "theme.toml").write_text(
        'mode = "dark"\n'
        '[colors]\n'
        'waiting = "#ff00ff"\n'
    )
    p = theme.load()
    assert p.fg[state.WAITING] == "#ff00ff"
    assert p.fg[state.RUNNING] == theme.DARK_DEFAULTS[state.RUNNING]


def test_load_rejects_invalid_hex_with_warning(tmp_config_dir, caplog):
    (tmp_config_dir / "theme.toml").write_text(
        '[colors]\n'
        'waiting = "not-a-color"\n'
    )
    with caplog.at_level(logging.WARNING, logger="tmux_agents.theme"):
        p = theme.load()
    assert p.fg[state.WAITING] == theme.DARK_DEFAULTS[state.WAITING]
    messages = " ".join(r.message for r in caplog.records)
    assert "waiting" in messages and "not-a-color" in messages


def test_load_accepts_3_char_hex_only_if_rrggbb(tmp_config_dir, caplog):
    (tmp_config_dir / "theme.toml").write_text(
        '[colors]\n'
        'idle = "#abc"\n'
    )
    with caplog.at_level(logging.WARNING, logger="tmux_agents.theme"):
        p = theme.load()
    assert p.fg[state.IDLE] == theme.DARK_DEFAULTS[state.IDLE]
    messages = " ".join(r.message for r in caplog.records)
    assert "idle" in messages


def test_get_palette_caches_across_calls(tmp_config_dir):
    theme.reset_cache()
    first = theme.get_palette()
    second = theme.get_palette()
    assert first is second


def test_reset_cache_causes_reload(tmp_config_dir):
    theme.reset_cache()
    first = theme.get_palette()
    (tmp_config_dir / "theme.toml").write_text('mode = "light"\n')
    assert theme.get_palette() is first
    theme.reset_cache()
    second = theme.get_palette()
    assert second is not first
    assert second.fg == theme.LIGHT_DEFAULTS


