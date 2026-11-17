"""Regression: ISSUE-004 - in a git worktree with no .env the backtest tried localhost:5432
and the error only said "check DB_HOST ...", hiding that no settings were loaded at all.
Found by /qa on 2026-11-17
Report: .gstack/qa-reports/qa-report-backtest-cli-2026-11-17.md
"""
from marketscanner import config
from marketscanner.backtest.__main__ import run
from tests.test_backtest_cli import BASE, TODAY


class DeadStore:
    def session_counts(self, *a): raise ConnectionError("refused")
    def load(self, *a): raise ConnectionError("refused")


def test_missing_env_file_is_named_with_the_settings_in_use(tmp_path, capsys, monkeypatch):
    missing = tmp_path / "nowhere" / ".env"
    monkeypatch.setattr(config, "ENV_FILE", missing)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.setitem(config.DB, "host", "localhost")
    assert run(BASE + ["--gallery", "none"], store=DeadStore(), today=TODAY, out_root=tmp_path / "out") == 2
    err = capsys.readouterr().err
    assert "Using DB_HOST=localhost DB_PORT=" in err
    assert f"No .env found at {missing}" in err and "worktree" in err


def test_present_env_file_gives_no_missing_env_hint(tmp_path, capsys, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DB_HOST=db.example\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setitem(config.DB, "host", "db.example")
    assert run(BASE + ["--gallery", "none"], store=DeadStore(), today=TODAY, out_root=tmp_path / "out") == 2
    err = capsys.readouterr().err
    assert "Using DB_HOST=db.example" in err and "No .env found" not in err


def test_db_host_from_the_environment_gives_no_missing_env_hint(tmp_path, capsys, monkeypatch):
    # Docker / CI: no .env file, but DB_* come from the process environment
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "nowhere" / ".env")
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setitem(config.DB, "host", "postgres")
    assert run(BASE + ["--gallery", "none"], store=DeadStore(), today=TODAY, out_root=tmp_path / "out") == 2
    err = capsys.readouterr().err
    assert "Using DB_HOST=postgres" in err and "No .env found" not in err
