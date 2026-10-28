import logging
import os

from sysmon.logs import LineWriter, LineWriterHandler


def files(tmp_path, name="m.log"):
    return sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(name))


def test_rotation_keeps_at_most_backup_count(tmp_path):
    w = LineWriter(tmp_path / "m.log", max_bytes=100, backup_count=2)
    for i in range(30):
        assert w.write(f"line {i:02d} " + "x" * 30 + "\n")
    assert files(tmp_path) == ["m.log", "m.log.1", "m.log.2"]
    for name in files(tmp_path):
        assert (tmp_path / name).stat().st_size <= 100
    # newest lines in m.log, older in .1, oldest kept in .2; nothing lost between them
    lines = []
    for name in ["m.log.2", "m.log.1", "m.log"]:
        lines += (tmp_path / name).read_text().splitlines()
    nums = [int(x.split()[1]) for x in lines]
    assert nums == list(range(nums[0], 30))


def test_no_rotation_without_limit(tmp_path):
    w = LineWriter(tmp_path / "m.log")
    for _ in range(100):
        w.write("x" * 50 + "\n")
    assert files(tmp_path) == ["m.log"]


def test_line_bigger_than_limit_still_written_whole(tmp_path):
    w = LineWriter(tmp_path / "m.log", max_bytes=10, backup_count=1)
    w.write("a" * 50 + "\n")
    w.write("b" * 50 + "\n")
    assert (tmp_path / "m.log").read_text() == "b" * 50 + "\n"
    assert (tmp_path / "m.log.1").read_text() == "a" * 50 + "\n"


def test_rename_refused_keeps_appending_and_retries(tmp_path, monkeypatch):
    """Windows: another program has the file open, so it cannot be renamed."""
    reports = []
    w = LineWriter(tmp_path / "m.log", max_bytes=20, backup_count=2, report=reports.append)
    real_replace = os.replace
    blocked = [True]

    def replace(src, dst):
        if blocked[0]:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    for i in range(5):
        assert w.write(f"line {i} xxxxxxxxx\n")
    assert files(tmp_path) == ["m.log"]  # nothing lost, file just grew
    assert len((tmp_path / "m.log").read_text().splitlines()) == 5
    assert sum("cannot rotate" in r for r in reports) == 1  # reported once
    blocked[0] = False
    w.write("after\n")
    assert files(tmp_path) == ["m.log", "m.log.1"]
    assert any("rotating" in r and "works again" in r for r in reports)


def test_deleted_folder_is_recreated(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    w = LineWriter(folder / "m.log")
    w.write("one\n")
    (folder / "m.log").unlink()
    folder.rmdir()
    assert w.write("two\n")
    assert (folder / "m.log").read_text() == "two\n"


def test_handler_writes_agent_log_and_never_recurses(tmp_path, capsys):
    target = tmp_path / "agent.log"
    h = LineWriterHandler(target, max_bytes=None, backup_count=1)
    logger = logging.getLogger("sysmon.test_handler")
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    try:
        logger.info("hello %s", "world")
        assert "INFO    sysmon.test_handler: hello world" in target.read_text()
        target.unlink()
        target.mkdir()  # now unwritable: the handler must report on stderr, not via logging
        logger.info("lost")
        logger.info("lost again")
    finally:
        logger.removeHandler(h)
    err = capsys.readouterr().err
    assert err.count("cannot write") == 1
