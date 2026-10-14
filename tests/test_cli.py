import pytest

from guardrail.cli import main


def test_global_options_work_before_or_after_the_command(tmp_path):
    assert main(["init-db", "--db", str(tmp_path / "after.db")]) == 0
    assert main(["--db", str(tmp_path / "before.db"), "init-db"]) == 0
    assert (tmp_path / "after.db").exists() and (tmp_path / "before.db").exists()


def test_unknown_options_are_refused_not_ignored(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "x.db"), "metrics", "--bogus"])


def test_backup_copies_every_database_file(tmp_path):
    import sqlite3

    db = tmp_path / "g.db"
    assert main(["--db", str(db), "ask", "My order O-5001 arrived with a cracked lantern. Refund the lantern?",
                 "--customer", "C-1001"]) == 0
    assert main(["--db", str(db), "backup", str(tmp_path / "bk")]) == 0
    [snapshot] = (tmp_path / "bk").iterdir()
    names = {p.name for p in snapshot.iterdir()}
    assert {"g.db", "g.db.checkpoints", "g.db.payments", "g.db.messages"} <= names
    refunded = sqlite3.connect(snapshot / "g.db").execute("SELECT refunded FROM orders WHERE id = 'O-5001'").fetchone()
    assert refunded == (49.99,)
