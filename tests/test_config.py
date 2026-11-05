"""The .env.example file loads cleanly and reproduces the code defaults."""
import os
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
