"""The .env.example file loads cleanly and reproduces the code defaults."""
import os

import pytest
from pathlib import Path

from sra import config

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_matches_defaults(monkeypatch):
    keys = [line.split("=", 1)[0] for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    config.load_dotenv(ROOT / ".env.example")
    try:
        assert os.environ["LLM_MODEL"] == "qwen3.5:4b" and os.environ["LLM_THINK"] == "false"
        assert os.environ["DATABASE_URL"] == "" and os.environ["KAFKA_BOOTSTRAP_SERVERS"] == ""
        assert config.path_setting("SRA_DATA_DIR", Path("x")) == (ROOT / "data").resolve()
        assert config.path_setting("SRA_DOCS_DIR", Path("x")) == (ROOT / "docs" / "OIPReqs").resolve()
        from sra.llm.client import from_settings
        c = from_settings()
        assert c.model == "qwen3.5:4b" and c.think is False and c.num_ctx == 8192
    finally:
        for k in keys:
            os.environ.pop(k, None)


def test_dotenv_inline_comments_and_quotes(tmp_path, monkeypatch):
    for k in ("A1", "A2", "A3", "A4"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text('A1=value  # comment\nA2="with # hash"\nA3=http://h/p#frag\nA4=\n', encoding="utf-8")
    config.load_dotenv(f)
    assert os.environ["A1"] == "value" and os.environ["A2"] == "with # hash"
    assert os.environ["A3"] == "http://h/p#frag" and os.environ["A4"] == ""


# ---------------------------------------------------------------- database settings (DATABASE_URL or CAP-injected)
def _clean_db_env(monkeypatch):
    import os
    from sra import config
    monkeypatch.setattr(config, "load_dotenv", lambda path=None: None)
    for k in list(os.environ):
        if k == "DATABASE_URL" or k.startswith("CAP_POSTGRES") or k == "SRA_CAP_POSTGRES_VAR":
            monkeypatch.delenv(k)
    return config


def test_no_database_settings_means_local_files(monkeypatch):
    assert _clean_db_env(monkeypatch).database_dsn() is None


def test_database_url_wins_over_cap_settings(monkeypatch):
    config = _clean_db_env(monkeypatch)
    monkeypatch.setenv("CAP_POSTGRES_SRA_SRADB_JDBC_URI", "jdbc:postgresql://10.0.0.5:5432/sradb")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")
    assert config.database_dsn() == "postgresql://u@h/db"


def test_cap_injected_jdbc_uri_and_iam_user(monkeypatch):
    """The settings CAP injects for a provisioned database (as in docs/cog_cap_deployment.svg)."""
    from psycopg.conninfo import conninfo_to_dict
    config = _clean_db_env(monkeypatch)
    monkeypatch.setenv("CAP_POSTGRES_FSICCOG_COGDB_JDBC_URI", "jdbc:postgresql://127.0.0.1:5432/cogdb?sslmode=disable")
    monkeypatch.setenv("CAP_POSTGRES_USER", "sra@my-project.iam")
    d = conninfo_to_dict(config.database_dsn())
    assert d == {"host": "127.0.0.1", "port": "5432", "dbname": "cogdb", "user": "sra@my-project.iam", "sslmode": "disable"}
    assert "password" not in d                                           # IAM login through the proxy: no password


def test_cap_jdbc_variants(monkeypatch):
    from psycopg.conninfo import conninfo_to_dict
    config = _clean_db_env(monkeypatch)
    socket = "jdbc:postgresql:///appdb?cloudSqlInstance=p:r:i&socketFactory=com.google.cloud.sql.postgres.SocketFactory&user=svc"
    d = conninfo_to_dict(config.jdbc_to_conninfo(socket))
    assert (d["host"], d["port"], d["dbname"], d["user"]) == ("127.0.0.1", "5432", "appdb", "svc")   # through the sidecar proxy
    d = conninfo_to_dict(config.jdbc_to_conninfo("jdbc:postgresql://db.internal/app?ssl=true", "u ser", r"p'w\d"))
    assert d["sslmode"] == "require" and d["user"] == "u ser" and d["password"] == r"p'w\d" and d["port"] == "5432"
    with pytest.raises(ValueError):
        config.jdbc_to_conninfo("jdbc:mysql://h/db")
    monkeypatch.setenv("CAP_POSTGRES_A_X_JDBC_URI", "jdbc:postgresql://h/a")
    monkeypatch.setenv("CAP_POSTGRES_B_Y_JDBC_URI", "jdbc:postgresql://h/b")
    with pytest.raises(ValueError, match="SRA_CAP_POSTGRES_VAR"):
        config.database_dsn()
    monkeypatch.setenv("SRA_CAP_POSTGRES_VAR", "CAP_POSTGRES_B_Y_JDBC_URI")
    assert conninfo_to_dict(config.database_dsn())["dbname"] == "b"
