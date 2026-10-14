import pytest

from guardrail.cli import main


def test_global_options_work_before_or_after_the_command(tmp_path):
    assert main(["init-db", "--db", str(tmp_path / "after.db")]) == 0
    assert main(["--db", str(tmp_path / "before.db"), "init-db"]) == 0
    assert (tmp_path / "after.db").exists() and (tmp_path / "before.db").exists()


def test_unknown_options_are_refused_not_ignored(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "x.db"), "metrics", "--bogus"])
