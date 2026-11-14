"""GitHub repository scan (connectors/github.py) with gh mocked: file selection, alerts, signals, API, CLI, gh errors."""
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("SRA_NO_APP", "1")

from fastapi.testclient import TestClient  # noqa: E402

from sra.api.app import create_app  # noqa: E402
from sra.connectors import github  # noqa: E402
from sra.evidence.bundle import EvidenceBundle  # noqa: E402
from sra.store import FileStore  # noqa: E402

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "orders_batch"
H = {"X-User": "alice@example.com"}
SHA = "0123456789abcdef0123456789abcdef01234567"
FAKE_SECRET = "ghp_" + "Z" * 36

REPO_FILES = {
    ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
                                "      - run: pytest\n      - run: trivy fs .\n",
    ".github/CODEOWNERS": "* @acme/orders-sre\n",
    ".github/dependabot.yml": "version: 2\nupdates:\n  - package-ecosystem: npm\n    directory: /\n",
    "k8s/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: orders\nspec:\n  replicas: 1\n"
                           "  template:\n    spec:\n      containers:\n        - name: app\n          image: orders:1\n",
    "deploy/helm/orders/Chart.yaml": "apiVersion: v2\nname: orders\nversion: 0.1.0\n",
    "deploy/helm/orders/values.yaml": "replicaCount: 2\n",
    "deploy/helm/orders/templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
                                                    "  name: {{ .Release.Name }}\nspec:\n  replicas: {{ .Values.replicaCount }}\n",
    "Dockerfile": "FROM python:3.13\nCOPY . /app\n",
    "Dockerfile.prod": "FROM python:3.13-slim\nUSER 1000\n",
    "docs/runbook.md": "# Orders runbook\n\nLast reviewed: 2026-10-01\n\nRecovery steps: restart the pods.\n"
                       "db_password=hunter2xyz\n",
    "README.md": "# Orders API\n",
    "LICENSE.md": "MIT\n",
    "config/app.yaml": "timeout: 5\n",
    "tests/fixtures/k8s.yaml": "apiVersion: v1\nkind: Pod\n",
    "node_modules/x/README.md": "# x\n",
    "src/main.py": "import requests\n\ndef stock(sku):\n    return requests.get('https://stock.internal/' + sku)\n",
}


def tarball(files=REPO_FILES) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"acme-orders-{SHA[:7]}/{path}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


DEPENDABOT = [
    {"number": 1, "state": "open", "dependency": {"package": {"ecosystem": "npm", "name": "lodash"}, "manifest_path": "package-lock.json"},
     "security_advisory": {"ghsa_id": "GHSA-aaaa-bbbb-cccc", "cve_id": "CVE-2021-23337", "summary": "Command injection in lodash",
                           "severity": "high"},
     "security_vulnerability": {"package": {"ecosystem": "npm", "name": "lodash"}, "severity": "critical",
                                "vulnerable_version_range": "< 4.17.21", "first_patched_version": {"identifier": "4.17.21"}}},
    {"number": 2, "state": "open", "dependency": {"package": {"ecosystem": "pip", "name": "jinja2"}, "manifest_path": "requirements.txt"},
     "security_advisory": {"ghsa_id": "GHSA-dddd-eeee-ffff", "cve_id": None, "summary": "XSS in jinja2", "severity": "medium",
                           "identifiers": [{"type": "GHSA", "value": "GHSA-dddd-eeee-ffff"}]},
     "security_vulnerability": {"severity": "medium", "vulnerable_version_range": "< 3.1.3", "first_patched_version": None}},
]
CODE_SCANNING = [
    {"number": 7, "tool": {"name": "CodeQL"}, "rule": {"id": "py/sql-injection", "severity": "error", "security_severity_level": "high",
                                                      "description": "SQL query built from user-controlled sources"},
     "most_recent_instance": {"message": {"text": "This query depends on a user-provided value."},
                              "location": {"path": "src/db.py", "start_line": 42}}},
]
SECRET_SCANNING = [{"number": 3, "secret_type": "github_personal_access_token", "secret_type_display_name": "GitHub Personal Access Token",
                    "secret": FAKE_SECRET, "state": "open"}]


class FakeGh:
    """Routes the gh calls the scan makes. `fail` maps a path prefix (or 'auth') to (message, HTTP status)."""

    def __init__(self, fail=None, size_kb=1200, tarball_too_large=False, files=None, bad_blobs=()):
        self.calls = []
        self.fail = fail or {}
        self.size_kb, self.tarball_too_large = size_kb, tarball_too_large
        self.files = REPO_FILES if files is None else files
        self.bad_blobs = set(bad_blobs)
        self.blobs = {hashlib.sha1(p.encode()).hexdigest(): t.encode() for p, t in self.files.items()}
        self.hosts = set()

    def __call__(self, args, host, max_bytes=None, timeout=None):
        self.calls.append(args)
        self.hosts.add(host)
        if args[0] == "auth":
            if "auth" in self.fail:
                raise github.GhApiError("gh auth failed: You are not logged into any GitHub hosts")
            return b""
        fields = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
        if fields:                          # gh sends -f fields of a GET as the query string
            assert args[args.index("--method") + 1] == "GET"
        path = args[-1] + ("?" + "&".join(fields) if fields else "")
        for prefix, (msg, code) in self.fail.items():
            if prefix != "auth" and path.startswith(prefix):
                raise github.GhApiError(f"gh api failed: {msg} (HTTP {code})", code)
        page = "--paginate" in args
        routes = {
            "repos/acme/orders": {"full_name": "acme/orders", "default_branch": "main", "private": True, "visibility": "private",
                                  "archived": False, "pushed_at": "2026-11-10T10:00:00Z", "size": self.size_kb},
            "repos/acme/orders/commits/main": {"sha": SHA},
            "repos/acme/orders/commits/release%2F1.2": {"sha": SHA},
            "repos/acme/orders/dependabot/alerts?state=open&per_page=100": DEPENDABOT,
            "repos/acme/orders/code-scanning/alerts?state=open&per_page=100": CODE_SCANNING,
            "repos/acme/orders/secret-scanning/alerts?state=open&per_page=100": SECRET_SCANNING,
            "repos/acme/orders/secret-scanning/alerts/3/locations?per_page=1":
                [{"type": "commit", "details": {"path": "config/settings.py", "start_line": 9}}],
            "repos/acme/orders/branches/main": {"name": "main", "protected": True,
                                                "protection": {"required_status_checks": {"contexts": ["ci/build"]}}},
            "repos/acme/orders/rules/branches/main": [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}],
            "repos/acme/orders/environments": {"total_count": 1, "environments": [
                {"name": "production", "protection_rules": [{"type": "required_reviewers", "reviewers": [{}, {}]}],
                 "deployment_branch_policy": {"protected_branches": True}}]},
            "repos/acme/orders/releases?per_page=10": [{"tag_name": "v1.2.0", "name": "1.2.0", "published_at": "2026-11-01T00:00:00Z",
                                                        "prerelease": False}],
            "repos/acme/orders/deployments?per_page=10": [{"id": 55, "environment": "production", "ref": "v1.2.0",
                                                           "created_at": "2026-11-02T00:00:00Z"}],
            "repos/acme/orders/deployments/55/statuses?per_page=1": [{"state": "success"}],
        }
        if path == f"repos/acme/orders/git/trees/{SHA}?recursive=1":
            return json.dumps({"sha": SHA, "truncated": False, "tree": [
                {"path": "deploy", "type": "tree", "mode": "040000", "sha": "0" * 40},
                {"path": "link", "type": "blob", "mode": "120000", "sha": "1" * 40, "size": 4}] + [
                {"path": p, "type": "blob", "mode": "100644", "sha": hashlib.sha1(p.encode()).hexdigest(), "size": len(t.encode())}
                for p, t in self.files.items()]}).encode()
        if path.startswith("repos/acme/orders/git/blobs/"):
            assert "Accept: application/vnd.github.raw+json" in args
            data = self.blobs.get(path.rsplit("/", 1)[1])
            if data is None or path.rsplit("/", 1)[1] in self.bad_blobs:
                raise github.GhApiError("gh api failed: Not Found (HTTP 404)", 404)
            return data
        if path == f"repos/acme/orders/tarball/{SHA}":
            if self.tarball_too_large:
                raise github.ArchiveTooLarge("gh api: the response is larger than 300 MB (SRA_GITHUB_MAX_ARCHIVE_MB)")
            return tarball()
        if path not in routes:
            raise github.GhApiError("gh api failed: Not Found (HTTP 404)", 404)
        body = routes[path]
        if page and isinstance(body, list) and len(body) > 1:      # two pages, printed one after the other like gh does
            return (json.dumps(body[:1]) + "\n" + json.dumps(body[1:])).encode()
        return json.dumps(body).encode()


@pytest.fixture()
def fake_gh(monkeypatch):
    fake = FakeGh(fail={"repos/acme/orders/branches/main/protection": ("Not Found", 404)})
    monkeypatch.setattr(github, "_run", fake)
    monkeypatch.setattr(github, "gh_binary", lambda: "gh")
    monkeypatch.setenv("SRA_GITHUB_HOSTS", "")
    monkeypatch.setenv("GH_HOST", "")
    return fake


# ------------------------------------------------------------------ selection and parsing
@pytest.mark.parametrize("path,want", [
    (".github/workflows/ci.yml", "ci"), (".gitlab-ci.yml", "ci"), ("Jenkinsfile", "ci"),
    (".github/CODEOWNERS", "codeowners"), ("CODEOWNERS", "codeowners"), ("src/CODEOWNERS", None),
    ("Dockerfile", "docker"), ("build.Dockerfile", "docker"), ("services/api/Dockerfile.prod", "docker"),
    ("docker-compose.yml", "config"), (".github/dependabot.yml", "config"),
    ("charts/orders/templates/svc.yaml", "helm"), ("charts/orders/values-prod.yaml", "helm"), ("charts/orders/Chart.yaml", "helm"),
    ("k8s/deploy.yaml", "?yaml"), ("docs/runbook.md", "doc"), ("ops/dr-plan.pdf", "doc"), ("README.md", "doc"),
    ("src/notes/runbook-orders.md", "doc"), ("src/notes/readme.md", None), ("CHANGELOG.md", None),
    ("reports/trivy-results.json", "scan"), ("codeql.sarif", "scan"), ("node_modules/a/Dockerfile", None),
    ("tests/fixtures/k8s.yaml", None), ("src/app.py", None),
])
def test_classify_path(path, want):
    assert github.classify_path(path, {"charts/orders"}) == want


def test_parse_target():
    assert github.parse_target("acme/orders") == ("acme/orders", "github.com")
    assert github.parse_target("https://github.com/acme/orders.git") == ("acme/orders", "github.com")
    with pytest.raises(github.GitHubError, match="expected owner/name"):
        github.parse_target("acme")
    with pytest.raises(github.GitHubError, match="not one this server scans"):
        github.parse_target("acme/orders", "evil.example.com")


def test_enterprise_host_through_gh_host(fake_gh, monkeypatch):
    monkeypatch.setenv("SRA_GITHUB_HOSTS", "github.com,ghe.corp.example")
    assert github.parse_target("git@ghe.corp.example:acme/orders.git") == ("acme/orders", "ghe.corp.example")
    res = github.scan("acme/orders", host="ghe.corp.example", alerts=False, signals=False)
    assert fake_gh.hosts == {"ghe.corp.example"}               # every gh call ran with GH_HOST set to it
    assert res.files[0].name.startswith(f"ghe.corp.example/acme/orders@{SHA[:12]}/")
    assert ["auth", "status", "--hostname", "ghe.corp.example"] in fake_gh.calls


def test_scan_collects_files_alerts_and_signals(fake_gh):
    res = github.scan("acme/orders")
    assert res.sha == SHA and res.ref == "main"
    paths = {f.path: f.category for f in res.files if f.path}
    assert paths == {".github/workflows/ci.yml": "ci", ".github/CODEOWNERS": "codeowners", ".github/dependabot.yml": "config",
                     "k8s/deployment.yaml": "k8s", "deploy/helm/orders/Chart.yaml": "helm", "deploy/helm/orders/values.yaml": "helm",
                     "deploy/helm/orders/templates/deployment.yaml": "helm", "Dockerfile": "docker", "Dockerfile.prod": "docker",
                     "docs/runbook.md": "doc", "README.md": "doc"}
    assert res.alerts == {"dependabot": 2, "code_scanning": 1, "secret_scanning": 1}
    # the source code is analysed in memory: one signals file, no source file stored
    assert res.code["files"] == 1 and res.code["by_category"] == {"latency": 1}
    code = json.loads(next(f for f in res.files if f.category == "code").data)
    assert code["format"] == "sra.code-signals/v1" and code["signals"][0]["rule"] == "py-requests-no-timeout"
    assert code["signals"][0]["occurrences"] == [{"path": "src/main.py", "line": 4,
                                                  "snippet": "return requests.get('https://stock.internal/' + sku)"}]
    assert not any(f.path == "src/main.py" for f in res.files)
    assert all(FAKE_SECRET.encode() not in f.data for f in res.files)        # the secret itself is never kept
    signals = next(f for f in res.files if f.category == "signals").data.decode()
    assert "Protected: yes" in signals and "ci/build" in signals and "required_approving_review_count 2" in signals
    assert "production: 2 required reviewer(s), protected branches only" in signals
    assert "| v1.2.0 |" in signals and "| production | v1.2.0 | 2026-11-02T00:00:00Z | success |" in signals
    assert "Dependabot: 2" in signals
    assert any(c[-1] == f"repos/acme/orders/tarball/{SHA}" for c in fake_gh.calls)


def test_scanned_files_are_detected_as_the_right_types(fake_gh):
    res = github.scan("acme/orders")
    b = EvidenceBundle()
    items = {f.path or f.name.rsplit("/", 1)[1]: b.ingest(f.name, f.data) for f in res.files}
    assert not b.errors
    kind = {k: (v.doc_type.value if v.doc_type else v.kind.value) for k, v in items.items()}
    assert kind[".github/workflows/ci.yml"] == "ci_config" and items[".github/workflows/ci.yml"].meta["tool"] == "github_actions"
    assert kind["k8s/deployment.yaml"] == "k8s_manifest"
    assert kind["Dockerfile"] == kind["Dockerfile.prod"] == kind[".github/CODEOWNERS"] == "other"
    assert kind["deploy/helm/orders/templates/deployment.yaml"] == "other"   # Helm templates are not plain YAML
    assert kind["docs/runbook.md"] == "runbook" and items["docs/runbook.md"].reviewed_at.date().isoformat() == "2026-10-01"
    assert "hunter2xyz" not in items["docs/runbook.md"].text                   # redacted on ingest, as for uploads
    assert items["docs/runbook.md"].meta["raw_secret_scan"]
    for alert_file, fmt in (("github-dependabot-alerts.cdx.json", "cyclonedx"), ("github-code-scanning-alerts.sarif.json", "sarif"),
                            ("github-secret-scanning-alerts.sarif.json", "sarif")):
        assert kind[alert_file] == "scan" and items[alert_file].meta["format"] == fmt
    by_rule = {f.rule_id: f for f in b.scan_findings}
    lodash = by_rule["CVE-2021-23337"]
    assert (lodash.tool, lodash.severity, lodash.package, lodash.fixed_version) == ("dependabot", "Critical", "lodash", "4.17.21")
    assert by_rule["GHSA-dddd-eeee-ffff"].severity == "Medium"
    sql = by_rule["py/sql-injection"]
    assert (sql.tool, sql.severity, sql.location) == ("CodeQL", "High", "src/db.py:42")
    assert by_rule["secret:github_personal_access_token"].location == "config/settings.py:9"
    assert kind["github-repository-signals.md"] in {"other", "security"}


def test_alerts_not_available_are_noted(monkeypatch):
    fake = FakeGh(fail={"repos/acme/orders/code-scanning": ("Advanced Security must be enabled", 403),
                        "repos/acme/orders/secret-scanning": ("Not Found", 404)})
    monkeypatch.setattr(github, "_run", fake)
    monkeypatch.setattr(github, "gh_binary", lambda: "gh")
    res = github.scan("acme/orders", signals=True)
    assert res.alerts == {"dependabot": 2, "code_scanning": None, "secret_scanning": None}
    assert any(n.startswith("Code-scanning alerts: not available (HTTP 403") for n in res.notes)
    assert not any(f.name.endswith("code-scanning-alerts.sarif.json") for f in res.files)
    signals = next(f for f in res.files if f.category == "signals").data.decode()
    assert "Code scanning: not available" in signals and "## Not collected" in signals


def test_limits(fake_gh, monkeypatch):
    monkeypatch.setenv("SRA_GITHUB_MAX_FILES", "3")
    res = github.scan("acme/orders", alerts=False, signals=False, code=False)
    assert len(res.files) == 3 and res.skipped == 8
    assert any("left out by the limits" in n for n in res.notes)


def test_ref_with_slash_and_bad_ref(fake_gh):
    assert github.scan("acme/orders", ref="release/1.2", alerts=False, signals=False).ref == "release/1.2"
    with pytest.raises(github.GitHubError, match="Not a git ref"):
        github.scan("acme/orders", ref="--upload-pack=x")
    with pytest.raises(github.GitHubError, match="was not found"):
        github.scan("acme/orders", ref="nope")


def _picked(res):
    return sorted((f.path, f.category, f.data) for f in res.files if f.path)


def test_large_repository_is_read_file_by_file_with_the_same_result(fake_gh, monkeypatch):
    small = github.scan("acme/orders", alerts=False, signals=False)
    assert small.fetch == "archive"
    big = FakeGh(size_kb=2_000_000)                                   # ~2 GB with history: no archive is tried
    monkeypatch.setattr(github, "_run", big)
    res = github.scan("acme/orders", alerts=False, signals=False)
    assert res.fetch == "files" and _picked(res) == _picked(small)
    code = lambda r: json.loads(next(f for f in r.files if f.category == "code").data)  # noqa: E731
    assert code(res)["signals"] == code(small)["signals"] and code(res)["practice"] == code(small)["practice"]
    paths = [c[-1] for c in big.calls]
    assert not any("/tarball/" in p for p in paths)
    blobs = [p for p in paths if "/git/blobs/" in p]
    # 11 chosen files, config/app.yaml opened to see whether it is a manifest, and the 1 source file; nothing else
    assert len(blobs) == len(set(blobs)) == 13
    assert any(n.startswith("Read 13 of 16 files one by one (the repository is too large") for n in res.notes)


def test_falls_back_when_the_archive_is_too_large(monkeypatch):
    fake = FakeGh(tarball_too_large=True)
    monkeypatch.setattr(github, "_run", fake)
    monkeypatch.setattr(github, "gh_binary", lambda: "gh")
    res = github.scan("acme/orders", alerts=False, signals=False)
    assert res.fetch == "files" and len([f for f in res.files if f.path]) == 11
    assert any("over 300 MB; files were read one by one" in n for n in res.notes)
    monkeypatch.setenv("SRA_GITHUB_FETCH", "archive")
    with pytest.raises(github.ArchiveTooLarge):
        github.scan("acme/orders", alerts=False, signals=False)


def test_file_by_file_limits_and_failures(monkeypatch):
    files = {f"cfg/app{i:03}.yaml": "timeout: 5\n" for i in range(50)}
    files.update({"k8s/deploy.yaml": REPO_FILES["k8s/deployment.yaml"], "README.md": "# x\n"})
    fake = FakeGh(files=files, bad_blobs=[hashlib.sha1(b"README.md").hexdigest()])
    monkeypatch.setattr(github, "_run", fake)
    monkeypatch.setattr(github, "gh_binary", lambda: "gh")
    monkeypatch.setenv("SRA_GITHUB_FETCH", "files")
    monkeypatch.setenv("SRA_GITHUB_MAX_YAML_PROBES", "10")
    res = github.scan("acme/orders", alerts=False, signals=False, code=False)
    assert [f.path for f in res.files] == ["k8s/deploy.yaml"]          # the k8s/ path is opened first; README failed
    assert sum("/git/blobs/" in c[-1] for c in fake.calls) == 11        # 10 YAML probes + README, not all 51 files
    assert any("41 other YAML file(s) not opened" in n for n in res.notes)
    assert any("1 could not be read" in n for n in res.notes)


def test_json_values_reads_paginated_output():
    assert github._json_values(b'[1, 2]\n[3]\n') == [[1, 2], [3]]
    assert github._json_values(b"") == []


# ------------------------------------------------------------------ gh itself (a fake gh executable)
FAKE_GH = r'''
import os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_GH_MODE", "ok")
if args[:2] == ["auth", "status"]:
    if mode == "logged_out":
        sys.stderr.write("You are not logged into any GitHub hosts. To log in, run: gh auth login\n")
        sys.exit(1)
    sys.exit(0)
if mode == "api_error":
    sys.stderr.write("gh: Bad credentials ghp_''' + "Q" * 36 + r''' (HTTP 401)\n")
    sys.exit(1)
if args[-1].startswith("repos/acme/orders/tarball/"):
    sys.stdout.buffer.write(b"x" * 300000)
    sys.exit(0)
sys.stdout.write('{"host": "%s", "prompt": "%s"}' % (os.environ.get("GH_HOST"), os.environ.get("GH_PROMPT_DISABLED")))
'''


@pytest.fixture()
def gh_exe(tmp_path, monkeypatch):
    script = tmp_path / "fake_gh.py"
    script.write_text(FAKE_GH, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "gh.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "gh"
        exe.write_text(f"#!{sys.executable}\n" + FAKE_GH, encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("SRA_GH", str(exe))
    return exe


def test_gh_missing(monkeypatch):
    monkeypatch.setenv("SRA_GH", "")
    monkeypatch.setattr(github.shutil, "which", lambda _: None)
    with pytest.raises(github.GhUnavailable, match="GitHub CLI \\(gh\\) is not installed"):
        github.scan("acme/orders")


def test_gh_not_signed_in(gh_exe, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MODE", "logged_out")
    with pytest.raises(github.GhUnavailable, match="gh is not signed in to github.com.*GH_TOKEN"):
        github.scan("acme/orders")


def test_gh_runs_with_host_and_no_prompts(gh_exe):
    assert github.Gh("ghe.corp.example").api("repos/acme/orders") == {"host": "ghe.corp.example", "prompt": "1"}


def test_gh_errors_are_redacted_and_carry_the_status(gh_exe, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MODE", "api_error")
    with pytest.raises(github.GhApiError) as e:
        github.Gh("github.com").api("repos/acme/orders")
    assert e.value.http == 401 and "Q" * 36 not in str(e.value) and "REDACTED" in str(e.value)


def test_gh_archive_size_limit(gh_exe):
    with pytest.raises(github.GitHubError, match="larger than"):
        github._run(["api", "repos/acme/orders/tarball/x"], "github.com", max_bytes=100000)


# ------------------------------------------------------------------ API and CLI
@pytest.fixture()
def client(tmp_path):
    store = FileStore(tmp_path)
    return TestClient(create_app(store=store, run_jobs_inline=True)), store


def _assessment(c) -> str:
    profile = yaml.safe_load((SCENARIO / "profile.yaml").read_text(encoding="utf-8"))
    return c.post("/api/assessments", json={"profile": profile}, headers=H).json()["assessment"]["id"]


def test_api_scan_then_run_scores_it(client, fake_gh):
    c, store = client
    aid = _assessment(c)
    r = c.post(f"/api/assessments/{aid}/github", json={"repository": "acme/orders"}, headers=H)
    assert r.status_code == 202, r.text
    j = r.json()                                   # a job, like the automated pass (run inline in tests)
    assert j["state"] == "done" and j["job"]["job"] == "github_scan" and j["job"]["repository"] == "acme/orders"
    msgs = [x["msg"] for x in j["log"]]
    assert msgs[0].startswith("Scan of github.com/acme/orders queued by alice@example.com")
    for step in ("Looking up acme/orders", f"acme/orders at main is commit {SHA[:12]}", "Downloading the commit as one archive",
                 "file(s) to look at", "Looking for reliability signals in 1 file(s)", "Reading open Dependabot",
                 "Reading branch protection", "Adding 16 evidence file(s)"):
        assert any(step in m for m in msgs), (step, msgs)
    assert msgs[-1] == f"Finished: 16 evidence file(s) from acme/orders@{SHA[:12]}"
    out = j["summary"]["scan"]
    assert c.get(f"/api/assessments/{aid}/job").json()["summary"]["scan"]["sha"] == SHA
    assert out["sha"] == SHA and out["alerts"]["dependabot"] == 2 and out["evidence_added"] == 16 and out["fetch"] == "archive"
    assert out["files"] == {"ci": 1, "codeowners": 1, "config": 1, "k8s": 1, "helm": 3, "docker": 2, "doc": 2, "alerts": 3,
                            "signals": 1, "code": 1}

    ev = c.get(f"/api/assessments/{aid}/evidence").json()
    assert not ev["errors"]
    assert ev["repositories"] == [{"label": f"github.com/acme/orders@{SHA[:12]}", "host": "github.com", "repository": "acme/orders",
                                   "sha": SHA[:12], "files": 16}]
    kinds = {f["name"].split(f"@{SHA[:12]}/", 1)[1]: f["doc_type"] or f["kind"] for f in ev["files"]}
    assert kinds[".github/workflows/ci.yml"] == "ci_config" and kinds["k8s/deployment.yaml"] == "k8s_manifest"
    assert kinds["docs/runbook.md"] == "runbook" and kinds["github-dependabot-alerts.cdx.json"] == "scan"
    assert all(f["source"] == "github" for f in ev["files"])
    audit = [x for x in c.get(f"/api/assessments/{aid}/audit").json() if x["action"] == "github_scan"]
    assert audit and audit[0]["target"] == f"github.com/acme/orders@{SHA}"

    c.put(f"/api/assessments/{aid}/templates", json={"templates": ["SRE-AT-11", "SRE-AT-12"]}, headers=H)
    assert c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H).json()["state"] == "done"
    a = c.get(f"/api/assessments/{aid}").json()["assessment"]
    c01 = a["results"]["SRE-AT-11-C01"]
    assert c01["result"] == "Gap" and "cyclonedx" in c01["rationale"] and "sarif" in c01["rationale"]
    assert a["results"]["SRE-AT-11-C02"]["result"] == "Gap"              # the secret-scanning alert and the runbook password
    assert a["results"]["SRE-AT-11-C04"]["evidence_refs"]                  # the workflow was read as CI configuration
    detectors = {f["detector"] for f in a["findings"]}
    assert {"vulnerabilities:dependabot", "vulnerabilities:CodeQL"} <= detectors
    assert any("CVE-2021-23337" in f["title"] for f in a["findings"])
    assert "code_signals:py-requests-no-timeout" in detectors        # from the source code
    assert kinds["github-code-signals.json"] == "code_analysis"


@pytest.mark.parametrize("fail,body,status,detail", [
    ({"auth": ("", 0)}, {"repository": "acme/orders"}, 503, "not signed in"),
    ({}, {"repository": "acme/orders", "host": "evil.example.com"}, 422, "not one this server scans"),
    ({}, {"repository": "not-a-repo"}, 422, "expected owner/name"),
])
def test_api_scan_errors(client, monkeypatch, fail, body, status, detail):
    c, store = client
    monkeypatch.setattr(github, "_run", FakeGh(fail=fail))
    monkeypatch.setattr(github, "gh_binary", lambda: "gh")
    aid = _assessment(c)
    r = c.post(f"/api/assessments/{aid}/github", json=body, headers=H)
    assert r.status_code == status and detail in r.json()["detail"]
    assert store.evidence(aid) == []


def test_api_scan_failure_is_reported_in_the_job(client, monkeypatch):
    c, store = client
    monkeypatch.setattr(github, "_run", FakeGh(fail={"repos/acme/orders": ("Not Found", 404)}))
    monkeypatch.setattr(github, "gh_binary", lambda: "gh")
    aid = _assessment(c)
    j = c.post(f"/api/assessments/{aid}/github", json={"repository": "acme/orders"}, headers=H).json()
    assert j["state"] == "failed" and "was not found on github.com" in j["error"]
    assert j["log"][-1]["msg"].startswith("Scan failed: Repository acme/orders was not found")
    assert store.evidence(aid) == []


def test_api_scan_cancelled_adds_nothing(client, fake_gh, monkeypatch):
    c, store = client
    aid = _assessment(c)
    real = github.collect

    def cancel_midway(*a, **k):                      # the user presses Cancel while files are being read
        store.request_cancel(aid)
        return real(*a, **k)
    monkeypatch.setattr(github, "collect", cancel_midway)
    j = c.post(f"/api/assessments/{aid}/github", json={"repository": "acme/orders"}, headers=H).json()
    assert j["state"] == "cancelled" and j["log"][-1]["msg"] == "Cancelled. No evidence from this scan was added."
    assert store.evidence(aid) == [] and not store.cancel_requested(aid)


def test_api_scan_refused_while_a_job_runs(client, fake_gh):
    c, store = client
    aid = _assessment(c)
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
    store.set_job(aid, {"state": "running", "updated_at": now, "job": {"job": "github_scan"}, "log": []})
    r = c.post(f"/api/assessments/{aid}/github", json={"repository": "acme/orders"}, headers=H)
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    r = c.post(f"/api/assessments/{aid}/run", json={"llm": False}, headers=H)
    assert r.status_code == 409 and r.json()["detail"].startswith("A repository scan is already running")


def test_api_github_status(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("SRA_GH", "")
    monkeypatch.setenv("SRA_GITHUB_HOSTS", "")
    monkeypatch.setenv("GH_HOST", "")
    monkeypatch.setattr(github.shutil, "which", lambda _: None)
    s = c.get("/api/github/status").json()
    assert s["available"] is False and "not installed" in s["error"] and s["hosts"] == ["github.com"]


def test_cli_writes_an_evidence_folder(fake_gh, tmp_path, capsys):
    from sra.cli import main
    main(["github", "acme/orders", "--out", str(tmp_path / "ev"), "--no-signals"])
    root = tmp_path / "ev" / "acme" / f"orders@{SHA[:12]}"
    assert (root / ".github" / "workflows" / "ci.yml").exists() and (root / "github-dependabot-alerts.cdx.json").exists()
    assert "wrote 15 file(s)" in capsys.readouterr().out
