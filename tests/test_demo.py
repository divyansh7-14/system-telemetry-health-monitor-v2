"""scripts/demo.py must only ever delete its own output files."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.mark.parametrize(
    "name",
    [
        "metrics.jsonl",
        "metrics.jsonl.3",
        "alerts.log",
        "alerts.log.1",
        "agent.log",
        "agent.log.12",
        "host_info.json",
        "console_output.txt",
        "sysmon.lock",
        "sysmon.stop",
    ],
)
def test_own_outputs_are_cleaned(name):
    assert demo.is_demo_output(name)


@pytest.mark.parametrize(
    "name",
    [
        "thesis.docx",
        "notes.txt",
        "metrics.jsonl.bak",
        "alerts.log.old",
        "my_metrics.jsonl",
        "agent.log.1.zip",
        ".gitkeep",
        "config.yaml",
    ],
)
def test_other_files_are_never_touched(name):
    assert not demo.is_demo_output(name)
