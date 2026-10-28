"""Keep the README and the script that tests it on Windows in sync."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_tagged_command_appears_verbatim_in_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "readme_check.ps1").read_text(encoding="utf-8")
    tagged = [
        ln.split("  # README")[0].strip()
        for ln in script.splitlines()
        if ln.rstrip().endswith("# README")
    ]
    assert len(tagged) >= 12
    missing = [cmd for cmd in tagged if cmd not in readme]
    assert not missing, f"README no longer contains: {missing}"


def test_readme_has_no_unfilled_placeholders():
    assert "SAMPLE_" not in (ROOT / "README.md").read_text(encoding="utf-8")


def test_files_the_readme_links_to_exist():
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(((?!http|#)[^)]+)\)", readme):
        assert (ROOT / target).exists(), f"README links to missing {target}"


def _block_after(readme: str, anchor: str, lang: str) -> str:
    start = readme.index(f"```{lang}\n", readme.index(anchor)) + len(f"```{lang}\n")
    return readme[start : readme.index("\n```", start)]


def test_readme_excerpts_match_the_committed_samples():
    """Every sample shown in the README is copied exactly from samples/ (nothing invented)."""
    import json

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    samples = ROOT / "samples"
    for name in ("alerts.log", "agent.log"):
        shown = _block_after(readme, f"[`samples/{name}`](samples/{name}):", "text")
        assert shown == (samples / name).read_text(encoding="utf-8").rstrip(), name
    host = _block_after(readme, "[`samples/host_info.json`](samples/host_info.json):", "json")
    assert json.loads(host) == json.loads((samples / "host_info.json").read_text(encoding="utf-8"))

    shown = json.loads(_block_after(readme, "of [`samples/metrics.jsonl`]", "json"))
    rows = [
        json.loads(x) for x in (samples / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    real = rows[shown["cycle"] - 1]
    for key, value in shown.items():  # lists were shortened for reading, never changed
        if isinstance(value, list):
            assert value == real[key][: len(value)], key
        elif isinstance(value, dict):
            for k, v in value.items():
                assert (
                    (v == real[key][k][: len(v)]) if isinstance(v, list) else v == real[key][k]
                ), k
        else:
            assert value == real[key], key

    cycle = _block_after(readme, "Each cycle prints a short summary", "text")
    console = (samples / "console_output.txt").read_text(encoding="utf-8")
    assert cycle in console
