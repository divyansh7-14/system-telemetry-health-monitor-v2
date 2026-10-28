from pathlib import Path

import pytest

from sysmon.config import (
    Config,
    ConfigError,
    Threshold,
    describe,
    load_config,
    parse_config,
)

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------- shipped files
def test_shipped_yaml_equals_builtin_defaults():
    cfg = load_config(ROOT / "config" / "config.yaml")
    default = Config()
    assert cfg.thresholds == default.thresholds
    assert (cfg.interval, cfg.top_n, cfg.alerting, cfg.logging) == (
        default.interval,
        default.top_n,
        default.alerting,
        default.logging,
    )
    assert cfg.warnings == ()


def test_yaml_and_json_examples_are_identical():
    y = load_config(ROOT / "config" / "config.yaml")
    j = load_config(ROOT / "config" / "config.example.json")
    assert (y.interval, y.top_n, y.thresholds, y.alerting, y.logging) == (
        j.interval,
        j.top_n,
        j.thresholds,
        j.alerting,
        j.logging,
    )


def test_no_file_means_defaults():
    cfg = load_config(None)
    assert cfg.source is None and cfg.thresholds["cpu_max"] == Threshold(75, 85)


# ------------------------------------------------------- forms of thresholds
def test_brief_style_percent_strings(tmp_path):
    """The brief writes `cpu_max: 85%` and `disk_min_free: 15%`."""
    p = write(tmp_path, "c.yaml", "thresholds:\n  cpu_max: 85%\n  disk_min_free: 15%\n")
    cfg = load_config(p)
    assert cfg.thresholds["cpu_max"] == Threshold(None, 85)  # bare value = critical only
    assert cfg.thresholds["disk_min_free"] == Threshold(None, 15)
    assert cfg.thresholds["memory_max"] == Threshold(80, 90)  # untouched rules keep defaults


def test_percent_strings_inside_levels_and_json(tmp_path):
    p = write(tmp_path, "c.json", '{"thresholds": {"cpu_max": {"warning": "70%", "critical": 90}}}')
    assert load_config(p).thresholds["cpu_max"] == Threshold(70, 90)


@pytest.mark.parametrize("off", ["null", "off", "false", "~"])
def test_rule_can_be_disabled(tmp_path, off):
    p = write(tmp_path, "c.yaml", f"thresholds:\n  swap_max: {off}\n")
    assert not load_config(p).thresholds["swap_max"].enabled


def test_warning_only(tmp_path):
    p = write(tmp_path, "c.yaml", "thresholds:\n  memory_max: {warning: 70}\n")
    assert load_config(p).thresholds["memory_max"] == Threshold(70, None)


def test_empty_file_uses_defaults_with_warning(tmp_path):
    cfg = load_config(write(tmp_path, "c.yaml", "# nothing here\n"))
    assert cfg.thresholds == Config().thresholds
    assert any("empty" in w for w in cfg.warnings)


def test_utf8_bom_from_notepad(tmp_path):
    p = tmp_path / "c.json"
    p.write_bytes(b'\xef\xbb\xbf{"interval": 20}')
    assert load_config(p).interval == 20


# ------------------------------------------------------------------ warnings
def test_typo_warns_with_suggestion_and_keeps_default(tmp_path):
    p = write(tmp_path, "c.yaml", "thresholds:\n  cpu_maxx: 50\nalertng: {}\n")
    cfg = load_config(p)
    assert cfg.thresholds["cpu_max"] == Threshold(75, 85)
    text = " | ".join(cfg.warnings)
    assert (
        "unknown setting 'thresholds.cpu_maxx' ignored (did you mean 'thresholds.cpu_max'?)" in text
    )
    assert "did you mean 'alerting'" in text


# -------------------------------------------------------------------- errors
@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        (
            "c.yaml",
            "thresholds:\n  cpu_max: {warning: 90, critical: 80}\n",
            "must be lower than critical",
        ),
        (
            "c.yaml",
            "thresholds:\n  disk_min_free: {warning: 10, critical: 15}\n",
            "must be higher than critical",
        ),
        (
            "c.yaml",
            "thresholds:\n  cpu_max: {warning: 85, critical: 85}\n",
            "must be lower than critical",
        ),
        ("c.yaml", "thresholds:\n  cpu_max: 150\n", "must be a percentage 0-100"),
        ("c.yaml", "thresholds:\n  cpu_max: high\n", "expected a number like 85 or '85%'"),
        ("c.yaml", "thresholds:\n  cpu_max: yes\n", "expected a number"),
        (
            "c.yaml",
            "thresholds:\n  cpu_max: {warn: 50}\n",
            "only 'warning' and 'critical' are allowed",
        ),
        ("c.yaml", "thresholds: [cpu_max]\n", "thresholds: must be a mapping"),
        ("c.yaml", "interval: 5\n", "interval: must be between 10 and 30, got 5"),
        ("c.yaml", "interval: 12.5\n", "interval: must be a whole number"),
        ("c.yaml", "top_n: 0\n", "top_n: must be between 1 and 100"),
        (
            "c.yaml",
            "alerting: {consecutive: 0}\n",
            "alerting.consecutive: must be between 1 and 20",
        ),
        ("c.yaml", "- just\n- a list\n", "top level must be a mapping"),
        ("c.yaml", "thresholds:\n  cpu_max: {warning: 75\n", "invalid YAML at line"),
        ("c.json", '{"thresholds": {"cpu_max": 85,}}', "invalid JSON at line 1, column"),
        ("c.txt", "interval: 15\n", "must be a .yaml, .yml or .json file"),
    ],
)
def test_invalid_configs_give_one_clear_error(tmp_path, name, text, message):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, name, text))
    assert message in str(exc.value)
    assert "\n" not in str(exc.value)  # one line, shown to the user as-is


def test_missing_file_is_an_error_not_silent_defaults(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "typo.yaml")


# ---------------------------------------------------------------- precedence
def test_cli_overrides_file_which_overrides_defaults(tmp_path):
    cfg = load_config(
        write(tmp_path, "c.yaml", "interval: 20\ntop_n: 7\nlogging: {backup_count: 4}\n")
    )
    assert (cfg.interval, cfg.top_n, cfg.logging.backup_count) == (20, 7, 4)
    cli = cfg.with_overrides(interval=10, top_n=None, max_log_mb=0.5, backup_count=None)
    assert (cli.interval, cli.top_n) == (10, 7)
    assert (cli.logging.max_log_mb, cli.logging.backup_count) == (0.5, 4)


def test_parse_config_rejects_non_mapping():
    with pytest.raises(ConfigError):
        parse_config(42)


def test_describe_mentions_direction_and_off(tmp_path):
    cfg = load_config(write(tmp_path, "c.yaml", "thresholds:\n  swap_max: off\n"))
    text = "\n".join(describe(cfg))
    assert "cpu_max            : WARNING > 75%, CRITICAL > 85%" in text
    assert "disk_min_free      : WARNING < 20%, CRITICAL < 15%" in text
    assert "swap_max           : off" in text


def test_value_errors_name_the_file(tmp_path):
    p = write(tmp_path, "mine.yaml", "interval: 99\n")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(exc.value).startswith(f"{p}: interval: must be between 10 and 30")


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        (
            "d.yaml",
            "thresholds:\n  cpu_max: 50\n  memory_max: 80\n  cpu_max: 95\n",
            "'cpu_max' is set twice (lines 2 and 4)",
        ),
        ("d.json", '{"interval": 10, "interval": 30}', "'interval' is set twice"),
    ],
)
def test_setting_written_twice_is_an_error(tmp_path, name, text, message):
    p = write(tmp_path, name, text)
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(exc.value).startswith(str(p)) and message in str(exc.value)


def test_non_percent_settings_do_not_suggest_percent(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, "c.yaml", "interval: 15s\n"))
    assert "expected a number, got '15s'" in str(exc.value)
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "c2.yaml", "interval: 15%\n"))


def test_recovery_margin_may_be_written_as_percent(tmp_path):
    cfg = load_config(write(tmp_path, "c.yaml", "alerting: {recovery_margin: 3%}\n"))
    assert cfg.alerting.recovery_margin == 3.0


def test_limits_are_always_floats(tmp_path):
    """metrics.jsonl showed 'limit': 20 next to 'limit': 50.0; keep the type consistent."""
    for cfg in (Config(), load_config(write(tmp_path, "c.yaml", "thresholds:\n  cpu_max: 85\n"))):
        for t in cfg.thresholds.values():
            assert all(v is None or type(v) is float for v in (t.warning, t.critical))
