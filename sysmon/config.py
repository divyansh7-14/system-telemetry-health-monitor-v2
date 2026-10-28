"""Health rules configuration (brief task 3): load, validate and merge.

Sources, lowest to highest priority: built-in defaults <- config file <- CLI flags.

The file is YAML (.yaml/.yml) or JSON (.json); both accept the same keys:

    interval: 15
    top_n: 5
    thresholds:
      cpu_max: {warning: 75, critical: 85}   # or just `cpu_max: 85%` (critical only)
      disk_min_free: {warning: 20, critical: 15}
      swap_max: null                         # null / off disables a rule
    alerting: {consecutive: 2, recovery_margin: 5, repeat_every: 10}
    logging: {max_log_mb: 5, backup_count: 3}

Anything that cannot be understood raises ConfigError with one clear sentence:
the agent never starts with a half-understood configuration. Unknown keys are
only warnings (with a "did you mean" hint) so a newer config still loads.
"""

from __future__ import annotations

import difflib
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

INTERVAL_RANGE = (10, 30)


class ConfigError(Exception):
    """The configuration cannot be used. The message is shown to the user as-is."""


@dataclass(frozen=True)
class RuleSpec:
    """What a rule measures. ``higher_is_worse`` is False for 'minimum free' rules."""

    name: str
    higher_is_worse: bool
    description: str


RULES: dict[str, RuleSpec] = {
    spec.name: spec
    for spec in (
        RuleSpec("cpu_max", True, "total CPU use, % of the machine"),
        RuleSpec("memory_max", True, "RAM in use, %"),
        RuleSpec("swap_max", True, "swap / page file in use, %"),
        RuleSpec("disk_min_free", False, "free space on each disk, %"),
        RuleSpec("process_cpu_max", True, "CPU of any single top process, % of the machine"),
        RuleSpec("process_memory_max", True, "RAM of any single top process, % of total RAM"),
    )
}


@dataclass(frozen=True)
class Threshold:
    warning: float | None
    critical: float | None

    @property
    def enabled(self) -> bool:
        return self.warning is not None or self.critical is not None


@dataclass(frozen=True)
class AlertingConfig:
    consecutive: int = 2  # breaching cycles in a row before WARNING/CRITICAL is raised
    recovery_margin: float = 5.0  # points back inside the limit before it clears
    repeat_every: int = 10  # re-log an ongoing breach every N cycles (0 = never)


@dataclass(frozen=True)
class LoggingConfig:
    max_log_mb: float = 5.0
    backup_count: int = 3


DEFAULT_THRESHOLDS: dict[str, Threshold] = {  # floats, like values read from a file
    "cpu_max": Threshold(75.0, 85.0),
    "memory_max": Threshold(80.0, 90.0),
    "swap_max": Threshold(50.0, 80.0),
    "disk_min_free": Threshold(20.0, 15.0),
    "process_cpu_max": Threshold(50.0, 80.0),
    "process_memory_max": Threshold(25.0, 40.0),
}


@dataclass(frozen=True)
class Config:
    interval: int = 15
    top_n: int = 5
    thresholds: dict[str, Threshold] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    alerting: AlertingConfig = field(default_factory=AlertingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source: str | None = None  # file it came from; None = built-in defaults
    warnings: tuple[str, ...] = ()

    def with_overrides(self, **overrides: Any) -> Config:
        """Apply CLI flags (None means 'not given')."""
        given = {k: v for k, v in overrides.items() if v is not None}
        logging_keys = {k: given.pop(k) for k in ("max_log_mb", "backup_count") if k in given}
        cfg = replace(self, **given)
        if logging_keys:
            cfg = replace(cfg, logging=replace(cfg.logging, **logging_keys))
        return cfg


# ------------------------------------------------------------------- loading
def load_config(path: str | Path | None) -> Config:
    """Load and validate a config file. ``None`` returns the built-in defaults."""
    if path is None:
        return Config()
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in (".yaml", ".yml", ".json"):
        raise ConfigError(f"{p}: config must be a .yaml, .yml or .json file")
    try:
        text = p.read_text(encoding="utf-8-sig")  # -sig: Notepad may add a BOM
    except FileNotFoundError:
        raise ConfigError(f"{p}: config file not found") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{p}: cannot read config file ({exc})") from None

    if suffix == ".json":
        try:
            data = (
                json.loads(text, object_pairs_hook=_no_duplicate_json_keys)
                if text.strip()
                else None
            )
        except ConfigError as exc:
            raise ConfigError(f"{p}: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{p}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from None
    else:
        try:
            data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
        except _DuplicateKey as exc:
            raise ConfigError(f"{p}: {exc}") from None
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
            raise ConfigError(f"{p}: invalid YAML{where}: {problem}") from None

    warnings: list[str] = []
    if data is None:
        warnings.append(f"{p} is empty; using built-in defaults")
        data = {}
    try:
        return parse_config(data, source=str(p), warnings=warnings)
    except ConfigError as exc:
        raise ConfigError(f"{p}: {exc}") from None


def parse_config(data: Any, source: str | None = None, warnings: list[str] | None = None) -> Config:
    warnings = list(warnings or [])
    if not isinstance(data, dict):
        raise ConfigError("top level must be a mapping of settings")
    _warn_unknown(data, {"interval", "top_n", "thresholds", "alerting", "logging"}, "", warnings)

    defaults = Config()
    interval = _int(data, "interval", defaults.interval, *INTERVAL_RANGE)
    top_n = _int(data, "top_n", defaults.top_n, 1, 100)

    thresholds = dict(DEFAULT_THRESHOLDS)
    raw_t = data.get("thresholds", {})
    if raw_t is None:
        raw_t = {}
    if not isinstance(raw_t, dict):
        raise ConfigError("thresholds: must be a mapping of rule names")
    _warn_unknown(raw_t, set(RULES), "thresholds.", warnings)
    for name, value in raw_t.items():
        if name in RULES:
            thresholds[name] = _threshold(RULES[name], value)

    alerting = _section(data, "alerting", AlertingConfig, warnings)
    alerting = AlertingConfig(
        consecutive=_int(alerting, "consecutive", AlertingConfig.consecutive, 1, 20, "alerting."),
        recovery_margin=_number(
            alerting,
            "recovery_margin",
            AlertingConfig.recovery_margin,
            0,
            50,
            "alerting.",
            percent=True,
        ),
        repeat_every=_int(
            alerting, "repeat_every", AlertingConfig.repeat_every, 0, 10_000, "alerting."
        ),
    )
    logging_ = _section(data, "logging", LoggingConfig, warnings)
    logging_cfg = LoggingConfig(
        max_log_mb=_number(
            logging_, "max_log_mb", LoggingConfig.max_log_mb, 0.01, 1024, "logging."
        ),
        backup_count=_int(logging_, "backup_count", LoggingConfig.backup_count, 1, 100, "logging."),
    )
    return Config(
        interval=interval,
        top_n=top_n,
        thresholds=thresholds,
        alerting=alerting,
        logging=logging_cfg,
        source=source,
        warnings=tuple(warnings),
    )


# ------------------------------------------------------- duplicate settings
class _DuplicateKey(Exception):
    pass


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a setting written twice (PyYAML keeps the last one silently)."""

    def construct_mapping(self, node, deep=False):
        seen: dict = {}
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise _DuplicateKey(
                    f"'{key}' is set twice (lines {seen[key]} and {key_node.start_mark.line + 1}); "
                    "remove one"
                )
            seen[key] = key_node.start_mark.line + 1
        return super().construct_mapping(node, deep=deep)


def _no_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ConfigError(f"'{key}' is set twice in the same object; remove one")
        out[key] = value
    return out


# ---------------------------------------------------------------- validation
def _warn_unknown(data: dict, known: set[str], prefix: str, warnings: list[str]) -> None:
    for key in data:
        if key in known:
            continue
        hint = difflib.get_close_matches(str(key), sorted(known), n=1)
        suggestion = f" (did you mean '{prefix}{hint[0]}'?)" if hint else ""
        warnings.append(f"unknown setting '{prefix}{key}' ignored{suggestion}")


def _section(data: dict, name: str, cls: type, warnings: list[str]) -> dict:
    raw = data.get(name, {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: must be a mapping")
    _warn_unknown(raw, set(cls.__dataclass_fields__), f"{name}.", warnings)
    return raw


def _as_number(value: Any, where: str, percent: bool = True) -> float:
    """A number. Percentages may be written '85%' (the brief writes `cpu_max: 85%`)."""
    if isinstance(value, bool):  # YAML yes/no/true/false are not numbers
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if isinstance(value, str):
        text = value.strip()
        if percent:
            text = text.removesuffix("%").strip()
        try:
            value = float(text)
        except ValueError:
            example = "a number like 85 or '85%'" if percent else "a number"
            raise ConfigError(f"{where}: expected {example}, got {value!r}") from None
    if not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _number(
    data: dict, key: str, default: float, lo: float, hi: float, prefix: str = "", percent=False
) -> float:
    if key not in data or data[key] is None:
        return default
    value = _as_number(data[key], prefix + key, percent)
    if not lo <= value <= hi:
        raise ConfigError(f"{prefix}{key}: must be between {lo:g} and {hi:g}, got {value:g}")
    return value


def _int(data: dict, key: str, default: int, lo: int, hi: int, prefix: str = "") -> int:
    value = _number(data, key, default, lo, hi, prefix)
    if value != int(value):
        raise ConfigError(f"{prefix}{key}: must be a whole number, got {value:g}")
    return int(value)


_OFF = {"off", "none", "disabled", "false"}


def _threshold(spec: RuleSpec, value: Any) -> Threshold:
    where = f"thresholds.{spec.name}"
    if (
        value is None
        or value is False
        or (isinstance(value, str) and value.strip().lower() in _OFF)
    ):
        return Threshold(None, None)  # rule disabled
    if isinstance(value, dict):
        unknown = set(value) - {"warning", "critical"}
        if unknown:
            raise ConfigError(
                f"{where}: only 'warning' and 'critical' are allowed, got {sorted(unknown)}"
            )
        warning = (
            None
            if value.get("warning") is None
            else _as_number(value["warning"], f"{where}.warning")
        )
        critical = (
            None
            if value.get("critical") is None
            else _as_number(value["critical"], f"{where}.critical")
        )
    else:
        warning, critical = None, _as_number(value, where)  # bare number = critical only

    for label, v in (("warning", warning), ("critical", critical)):
        if v is not None and not 0 <= v <= 100:
            raise ConfigError(f"{where}.{label}: must be a percentage 0-100, got {v:g}")
    if warning is not None and critical is not None:
        if spec.higher_is_worse and not warning < critical:
            raise ConfigError(
                f"{where}: warning ({warning:g}) must be lower than critical ({critical:g}) "
                "because a higher value is worse"
            )
        if not spec.higher_is_worse and not warning > critical:
            raise ConfigError(
                f"{where}: warning ({warning:g}) must be higher than critical ({critical:g}) "
                "because it is a minimum free space"
            )
    return Threshold(warning, critical)


def describe(cfg: Config) -> list[str]:
    """Human-readable summary printed at start-up."""
    lines = []
    for name, spec in RULES.items():
        t = cfg.thresholds[name]
        if not t.enabled:
            lines.append(f"  {name:<19}: off")
            continue
        op = ">" if spec.higher_is_worse else "<"
        parts = []
        if t.warning is not None:
            parts.append(f"WARNING {op} {t.warning:g}%")
        if t.critical is not None:
            parts.append(f"CRITICAL {op} {t.critical:g}%")
        lines.append(f"  {name:<19}: " + ", ".join(parts))
    a = cfg.alerting
    lines.append(
        f"  alerting           : after {a.consecutive} cycle(s) in a row, clears "
        f"{a.recovery_margin:g} pts inside the limit, repeat every "
        + (f"{a.repeat_every} cycles" if a.repeat_every else "never")
    )
    return lines
