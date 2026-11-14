"""GitHub sign-in through gh (connectors/github.py): status parsing, the device flow, opt-in token sign-in, sign-out,
and the gating that keeps an interactive sign-in off a shared server. gh is a fake executable with gh's output."""
import os
import sys
import time

import pytest

os.environ.setdefault("SRA_NO_APP", "1")

from fastapi.testclient import TestClient  # noqa: E402

from sra.api.app import create_app  # noqa: E402
from sra.connectors import github  # noqa: E402
from sra.store import FileStore  # noqa: E402

H = {"X-User": "alice@example.com"}
GOOD = "ghp_good" + "A" * 32
BAD = "ghp_bad" + "B" * 33

FAKE = r'''
import os, sys, time
args = sys.argv[1:]
state = os.environ["FAKE_GH_STATE"]
acct = os.path.join(state, "account")
def out(s): sys.stdout.write(s + "\n"); sys.stdout.flush()
def err(s): sys.stderr.write(s + "\n"); sys.stderr.flush()
if args[:2] == ["auth", "status"]:
    host = args[args.index("--hostname") + 1]
    if os.environ.get("GH_TOKEN") and host == "github.com":
        out(host); out("  ✓ Logged in to github.com account envbot (GH_TOKEN)"); out("  - Token: gho_************")
        sys.exit(0)
    if os.path.exists(acct):
        name = open(acct).read()
        out(host); out(f"  ✓ Logged in to {host} account {name} (keyring)"); out("  - Active account: true")
        out("  - Git operations protocol: https"); out("  - Token: gho_************************************")
        out("  - Token scopes: 'gist', 'read:org', 'repo'")
        sys.exit(0)
    err("You are not logged into any GitHub hosts. To log in, run: gh auth login"); sys.exit(1)
if args[:2] == ["auth", "login"] and "--web" in args:
    if os.environ.get("FAKE_GH_MODE") == "old_code":
        err("! First copy your one-time code: AB12-CD34")
    elif os.environ.get("FAKE_GH_MODE") == "no_code":
        err("! Something gh has not said before")
    else:                                      # gh 2.104
        err("! One-time code (AB12-CD34) copied to clipboard")
    err("Open this URL to continue in your web browser: https://github.com/login/device")
    if os.environ.get("FAKE_GH_MODE") == "deny":
        time.sleep(0.3); err("X Authorization was denied by the user"); sys.exit(1)
    for _ in range(int(os.environ.get("FAKE_GH_WAIT", "100"))):
        if os.path.exists(os.path.join(state, "approve")):
            open(acct, "w").write("octocat"); err("✓ Authentication complete."); err("✓ Logged in as octocat"); sys.exit(0)
        time.sleep(0.1)
    err("error: timed out waiting for authorization"); sys.exit(1)
if args[:2] == ["auth", "login"] and "--with-token" in args:
    tok = sys.stdin.read().strip()
    open(os.path.join(state, "argv"), "w").write(" ".join(args))
    if tok.startswith("ghp_good"):
        open(acct, "w").write("tokenbot"); sys.exit(0)
    err(f"error validating token: HTTP 401: Bad credentials (https://api.github.com/) {tok}"); sys.exit(1)
if args[:2] == ["auth", "logout"]:
    open(os.path.join(state, "argv"), "w").write(" ".join(args))
    os.remove(acct); err("✓ Logged out of github.com account octocat"); sys.exit(0)
err("unknown command"); sys.exit(1)
'''


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    script = tmp_path / "fake_gh.py"
    script.write_text(FAKE, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "gh.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "gh"
        exe.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("SRA_GH", str(exe))
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("PYTHONUTF8", "1")             # the fake writes UTF-8, as gh does
    # set (not deleted): a developer's .env fills in variables that are missing from the environment
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST", "SRA_GITHUB_HOSTS",
                 "FAKE_GH_MODE"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SRA_GH_TOKEN_LOGIN", "false")
    monkeypatch.setenv("SRA_GH_INTERACTIVE_LOGIN", "true")
    github._LOGINS.clear()
    yield state
    for s in github._LOGINS.values():
        github._stop(s, "cancelled", None)
    github._LOGINS.clear()


def _until(pred, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("condition not met in time")


# ------------------------------------------------------------------ status parsing
def test_parse_auth_status_current_and_old_gh():
    new = ("github.com\n  ✓ Logged in to github.com account octocat (keyring)\n  - Active account: false\n"
           "  ✓ Logged in to github.com account hubot (keyring)\n  - Active account: true\n  - Token: gho_****\n"
           "  - Token scopes: 'repo', 'read:org'\n")
    assert github.parse_auth_status(new, "github.com") == {"signed_in": True, "account": "hubot", "source": "gh",
                                                           "scopes": ["repo", "read:org"]}
    old = "github.com\n  ✓ Logged in to github.com as octocat (/home/u/.config/gh/hosts.yml)\n  ✓ Token: *******************\n"
    assert github.parse_auth_status(old, "github.com")["account"] == "octocat"
    env = "  ✓ Logged in to ghe.corp.example account svc-sre (GH_ENTERPRISE_TOKEN)\n"
    assert github.parse_auth_status(env, "ghe.corp.example")["source"] == "environment"
    bad = "github.com\n  X Failed to log in to github.com account octocat (keyring)\n  - The token in keyring is invalid.\n"
    st = github.parse_auth_status(bad, "github.com")
    assert st["signed_in"] is False and st["account"] == "octocat" and "no longer valid" in st["error"]
    assert github.parse_auth_status("You are not logged into any GitHub hosts.", "github.com")["signed_in"] is False


def test_auth_status_never_returns_the_token_line(gh):
    (gh / "account").write_text("octocat")
    st = github.auth_status("github.com")
    assert (st["signed_in"], st["account"], st["source"], st["scopes"]) == (True, "octocat", "gh", ["gist", "read:org", "repo"])
    assert "gho_" not in repr(st) and st["interactive_login"] is True and st["token_login"] is False


def test_environment_token_turns_interactive_sign_in_off(gh, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "E" * 36)
    st = github.auth_status("github.com")
    assert (st["signed_in"], st["account"], st["source"], st["env_token"]) == (True, "envbot", "environment", "GH_TOKEN")
    assert st["interactive_login"] is False and ("E" * 36) not in repr(st)
    with pytest.raises(github.GitHubError, match="GH_TOKEN from its environment"):
        github.start_login("github.com", "alice")
    with pytest.raises(github.GitHubError, match="GH_TOKEN from its environment"):
        github.logout("github.com")


def test_interactive_sign_in_is_off_by_default(gh, monkeypatch):
    monkeypatch.setenv("SRA_GH_INTERACTIVE_LOGIN", "false")
    assert github.auth_status("github.com")["interactive_login"] is False
    with pytest.raises(github.GitHubError, match="SRA_GH_INTERACTIVE_LOGIN=true"):
        github.start_login("github.com", "alice")


def test_sign_in_only_for_allowed_hosts(gh):
    with pytest.raises(github.GitHubError, match="not one this server scans"):
        github.start_login("evil.example.com", "alice")


# ------------------------------------------------------------------ device flow
def test_device_flow_shows_the_code_then_completes(gh):
    v = github.start_login("github.com", "alice")
    assert (v["state"], v["code"], v["url"]) == ("pending", "AB12-CD34", "https://github.com/login/device")
    assert 0 < v["expires_in"] <= 900
    assert github.start_login("github.com", "bob")["code"] == "AB12-CD34"      # one sign-in at a time per host
    (gh / "approve").write_text("")
    done = _until(lambda: (github.login_view("github.com") or {}).get("state") == "done" and github.login_view("github.com"))
    assert done["account"] == "octocat"
    assert github.auth_status("github.com")["signed_in"] is True


@pytest.mark.parametrize("line", ["! One-time code (WDJB-MJHT) copied to clipboard", "! First copy your one-time code: WDJB-MJHT"])
def test_device_code_both_gh_wordings(line):
    assert github.DEVICE_CODE.search(line).group(1) == "WDJB-MJHT"


def test_device_flow_older_gh_wording(gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MODE", "old_code")
    assert github.start_login("github.com", "alice")["code"] == "AB12-CD34"


def test_device_flow_without_a_readable_code_fails_instead_of_waiting(gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MODE", "no_code")
    monkeypatch.setenv("FAKE_GH_WAIT", "50")
    v = github.start_login("github.com", "alice", wait=2)
    assert v["state"] == "failed" and "one-time code" in v["error"]
    time.sleep(0.5)
    assert github.login_view("github.com")["state"] == "failed"


def test_device_flow_denied(gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MODE", "deny")
    github.start_login("github.com", "alice")
    v = _until(lambda: github.login_view("github.com")["state"] == "failed" and github.login_view("github.com"))
    assert "denied" in v["error"] and "AB12-CD34" not in v["error"]


def test_device_flow_cancel(gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_WAIT", "20")
    assert github.start_login("github.com", "alice")["state"] == "pending"
    assert github.cancel_login("github.com")["state"] == "cancelled"
    time.sleep(0.5)
    assert github.login_view("github.com")["state"] == "cancelled"


# ------------------------------------------------------------------ token, sign-out
def test_token_sign_in_is_a_second_opt_in(gh, monkeypatch):
    with pytest.raises(github.GitHubError, match="SRA_GH_TOKEN_LOGIN"):
        github.login_with_token("github.com", GOOD)
    monkeypatch.setenv("SRA_GH_TOKEN_LOGIN", "true")
    st = github.login_with_token("github.com", GOOD)
    assert st["signed_in"] and st["account"] == "tokenbot" and GOOD not in repr(st)
    assert GOOD not in (gh / "argv").read_text()                               # on stdin, never on the command line
    with pytest.raises(github.GitHubError) as e:
        github.login_with_token("github.com", BAD)
    assert BAD not in str(e.value) and "Bad credentials" in str(e.value)
    with pytest.raises(github.GitHubError, match="does not look like"):
        github.login_with_token("github.com", "short")


def test_logout(gh):
    (gh / "account").write_text("octocat")
    st = github.logout("github.com")
    assert st["signed_in"] is False
    assert (gh / "argv").read_text() == "auth logout --hostname github.com --user octocat"


# ------------------------------------------------------------------ API and CLI
@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(store=FileStore(tmp_path / "store"), run_jobs_inline=True))


def test_api_sign_in_flow(gh, client, monkeypatch, caplog):
    c = client
    s = c.get("/api/github/status").json()
    assert s["interactive_login"] is True and s["token_login"] is False
    assert c.get("/api/github/auth?host=github.com").json()["signed_in"] is False
    r = c.post("/api/github/login", json={"host": "github.com"}, headers=H)
    assert r.status_code == 202 and r.json()["code"] == "AB12-CD34"
    (gh / "approve").write_text("")
    _until(lambda: c.get("/api/github/auth").json()["login"]["state"] == "done")
    a = c.get("/api/github/auth").json()
    assert a["signed_in"] and a["account"] == "octocat" and a["source"] == "gh"
    assert c.post("/api/github/logout", json={}, headers=H).json()["signed_in"] is False

    monkeypatch.setenv("SRA_GH_TOKEN_LOGIN", "true")
    with caplog.at_level("INFO"):
        r = c.post("/api/github/login/token", json={"host": "github.com", "token": GOOD}, headers=H)
        assert r.status_code == 200 and r.json()["account"] == "tokenbot" and GOOD not in r.text
        r = c.post("/api/github/login/token", json={"token": BAD}, headers=H)
        assert r.status_code == 422 and BAD not in r.text
        r = c.post("/api/github/login/token", json={"token": "x"}, headers=H)
        assert r.status_code == 422 and '"x"' not in r.text
    assert GOOD not in caplog.text and BAD not in caplog.text


def test_api_sign_in_refused_on_a_shared_server(gh, client, monkeypatch):
    monkeypatch.setenv("SRA_GH_INTERACTIVE_LOGIN", "false")
    r = client.post("/api/github/login", json={}, headers=H)
    assert r.status_code == 422 and "SRA_GH_INTERACTIVE_LOGIN" in r.json()["detail"]
    assert client.post("/api/github/logout", json={}, headers=H).status_code == 422


def test_cli_status(gh, capsys):
    from sra.cli import main
    with pytest.raises(SystemExit) as e:
        main(["github", "status"])
    assert e.value.code == 1 and '"signed_in": false' in capsys.readouterr().out
    (gh / "account").write_text("octocat")
    with pytest.raises(SystemExit) as e:
        main(["github", "status"])
    assert e.value.code == 0 and '"account": "octocat"' in capsys.readouterr().out
    with pytest.raises(SystemExit, match="--out <folder> or --assessment"):
        main(["github", "acme/orders"])
