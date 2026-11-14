"""GitHub repository scan through the `gh` CLI: the files and repository signals the detectors already understand.

    scan("owner/name", ref="main", host="github.example.com") -> RepoScan

What is collected (each becomes one evidence file, stored like an upload, so the bundle parses, redacts and scores it):
- files from the commit's tarball: GitHub Actions workflows and other CI configuration, Kubernetes manifests (any YAML
  that declares apiVersion and kind), Helm charts (Chart.yaml, values*.yaml, templates/*.yaml), Dockerfiles and compose
  files, CODEOWNERS, dependabot.yml, runbooks and docs, and scanner output committed to the repository (SARIF, SBOMs);
- open Dependabot alerts (as a CycloneDX VEX file), open code-scanning alerts and open secret-scanning alerts (as
  SARIF; the secret value itself is never kept);
- a "repository signals" document: default branch, branch protection and rulesets, environments and their protection
  rules, recent releases and deployments.

Authentication is gh's own: `gh auth login`, or GH_TOKEN / GH_ENTERPRISE_TOKEN in the environment. No token is read,
passed or logged here. GitHub Enterprise hosts go through GH_HOST; only hosts listed in SRA_GITHUB_HOSTS (default: GH_HOST,
else github.com) can be scanned, so an assessor cannot send gh's Enterprise token to a host of their choosing.
Settings: SRA_GH (path to gh, default: gh on PATH), SRA_GITHUB_MAX_FILES (400), SRA_GITHUB_MAX_FILE_MB (5),
SRA_GITHUB_MAX_TOTAL_MB (50), SRA_GITHUB_MAX_ARCHIVE_MB (300), SRA_GITHUB_TIMEOUT (120 seconds per gh call).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import quote

from .. import config
from ..evidence.redact import redact
from ..progress import NullProgress, Progress

REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?$")
REF = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/@+-]{1,250}$")
URL = re.compile(r"^(?:https?://|git@)([^/:]+)[/:]([^/]+/[^/]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    """The scan could not be done as asked (bad repository or ref, a size limit, a gh failure). HTTP 422."""
    status = 422


class GhUnavailable(GitHubError):
    """gh is not installed or not signed in for the host. HTTP 503: an operator has to fix it, not the assessor."""
    status = 503


class ArchiveTooLarge(GitHubError):
    """gh's response was larger than the limit (a whole-commit archive of a big repository)."""


class GhApiError(GitHubError):
    def __init__(self, msg: str, http: int | None = None):
        super().__init__(msg)
        self.http = http


@dataclass
class RepoFile:
    name: str                 # evidence name: <host>/<owner>/<name>@<sha12>/<path>
    data: bytes
    category: str             # ci, k8s, helm, docker, codeowners, config, doc, scan, alerts, signals
    path: str = ""            # path in the repository ("" for alerts and the signals document)


@dataclass
class RepoScan:
    repository: str
    host: str
    ref: str
    sha: str
    files: list[RepoFile] = field(default_factory=list)
    alerts: dict[str, int | None] = field(default_factory=dict)   # None: not available (see notes)
    notes: list[str] = field(default_factory=list)
    skipped: int = 0          # matching files left out by the size and count limits
    code: dict | None = None  # the code-signals pass: files analysed, signals by category, capabilities found
    fetch: str = ""           # how the files were read: "archive" (one tarball) or "files" (one by one)

    def summary(self) -> dict:
        return {"repository": self.repository, "host": self.host, "ref": self.ref, "sha": self.sha,
                "files": dict(Counter(f.category for f in self.files)), "alerts": self.alerts, "notes": self.notes,
                "skipped": self.skipped, "code": self.code, "fetch": self.fetch}


# ------------------------------------------------------------------ gh
def _int(name: str, default: int) -> int:
    try:
        return int(config.setting(name, "") or default)
    except ValueError:
        return default


def default_host() -> str:
    return (config.setting("GH_HOST") or "github.com").strip().lower()


def allowed_hosts() -> list[str]:
    raw = config.setting("SRA_GITHUB_HOSTS") or ""
    hosts = [h.strip().lower() for h in raw.split(",") if h.strip()]
    return hosts or [default_host()]


def gh_binary() -> str:
    path = config.setting("SRA_GH") or shutil.which("gh")
    if not path or not os.path.exists(path):
        raise GhUnavailable("The GitHub CLI (gh) is not installed on this server"
                            + (f" (SRA_GH={path} does not exist)" if path else "")
                            + ". Install gh, or set SRA_GH to its path; see the README (GitHub repository scan).")
    return path


def _env(host: str) -> dict:
    env = dict(os.environ)
    env.update({"GH_HOST": host, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1",
                "NO_COLOR": "1", "CLICOLOR": "0"})
    return env


def _run(args: list[str], host: str, max_bytes: int | None = None, timeout: int | None = None) -> bytes:
    """Run gh and return stdout. stdout is never logged (it can be file content); stderr goes into the error message
    only after redaction, and gh never prints tokens there."""
    gh = gh_binary()
    timeout = timeout or _int("SRA_GITHUB_TIMEOUT", 120)
    try:
        proc = subprocess.Popen([gh, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_env(host))
    except OSError as exc:
        raise GhUnavailable(f"The GitHub CLI (gh) could not be started: {exc.strerror or exc}") from None
    err: list[bytes] = []
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    reader.start()
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    out, n, too_big = [], 0, False
    try:
        while chunk := proc.stdout.read(1 << 16):
            n += len(chunk)
            if max_bytes and n > max_bytes:
                too_big = True
                proc.kill()
                break
            out.append(chunk)
        proc.wait()
    finally:
        timed_out = not timer.is_alive() and not too_big
        timer.cancel()
        reader.join(5)
    if too_big:
        raise ArchiveTooLarge(f"gh {args[0]}: the response is larger than {max_bytes // 1024 // 1024} MB "
                          "(SRA_GITHUB_MAX_ARCHIVE_MB)")
    if proc.returncode != 0:
        if timed_out:
            raise GhApiError(f"gh {args[0]} did not finish within {timeout} s (SRA_GITHUB_TIMEOUT)")
        msg = redact(b"".join(err).decode("utf-8", "replace")).strip().splitlines()
        text = " ".join(m.strip() for m in msg if m.strip())[:400] or f"exit code {proc.returncode}"
        m = re.search(r"HTTP (\d{3})", text)
        raise GhApiError(f"gh {args[0]} failed: {text}", int(m.group(1)) if m else None)
    return b"".join(out)


def check_auth(host: str) -> None:
    gh_binary()
    try:
        _run(["auth", "status", "--hostname", host], host, timeout=30)
    except GhUnavailable:
        raise
    except GitHubError:
        token = "GH_TOKEN" if host == "github.com" else "GH_ENTERPRISE_TOKEN"
        raise GhUnavailable(f"gh is not signed in to {host}. Run `gh auth login --hostname {host}` on the server, or set "
                            f"{token} in its environment (from the platform's secret store).") from None


def _json_values(raw: bytes) -> list:
    """gh --paginate prints one JSON document per page; decode them all."""
    text, dec, pos, out = raw.decode("utf-8", "replace"), json.JSONDecoder(), 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return out
        val, pos = dec.raw_decode(text, pos)
        out.append(val)


class Gh:
    def __init__(self, host: str):
        self.host = host

    def api(self, path: str, paginate: bool = False, **params):
        """GET path; query parameters go as -f fields (gh puts them in the query string for GET), so no '&' or '?'
        ever reaches a shell or the command line as part of the path."""
        args = ["api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
        if params:
            args += ["--method", "GET"] + [x for k, v in params.items() for x in ("-f", f"{k}={v}")]
        vals = _json_values(_run(args + (["--paginate"] if paginate else []) + [path], self.host))
        if paginate:
            return [x for v in vals for x in (v if isinstance(v, list) else [v])]
        return vals[0] if vals else None

    def raw(self, path: str, max_bytes: int, accept: str = "application/vnd.github+json") -> bytes:
        return _run(["api", "-H", f"Accept: {accept}", path], self.host, max_bytes=max_bytes)


SCANNED = re.compile(r"^(?P<host>[A-Za-z0-9.-]+(?::\d+)?)/(?P<repo>[^/]+/[^/@]+)@(?P<sha>[0-9a-f]{12})/(?P<path>.+)$")


def scanned_from(name: str) -> dict | None:
    """Where an evidence file came from, when a repository scan added it (its name is <host>/<owner>/<name>@<sha12>/<path>)."""
    m = SCANNED.match(name or "")
    if not m or "." not in m.group("host"):
        return None
    return {"label": f"{m.group('host')}/{m.group('repo')}@{m.group('sha')}", "host": m.group("host"),
            "repository": m.group("repo"), "sha": m.group("sha")}


# ------------------------------------------------------------------ inputs
def parse_target(repo: str, host: str | None = None) -> tuple[str, str]:
    """(owner/name, host) from 'owner/name' or a clone URL; the host must be one this server may scan."""
    repo = (repo or "").strip()
    m = URL.match(repo)
    if m:
        url_host, repo = m.group(1).lower(), m.group(2)
        if host and host.strip().lower() != url_host:
            raise GitHubError(f"The URL names host {url_host} but the host field says {host}")
        host = url_host
    if not REPO.match(repo):
        raise GitHubError(f"Not a GitHub repository: {repo!r} (expected owner/name)")
    host = (host or default_host()).strip().lower()
    if not HOST.match(host):
        raise GitHubError(f"Not a host name: {host!r}")
    if host not in allowed_hosts():
        raise GitHubError(f"Host {host} is not one this server scans (SRA_GITHUB_HOSTS: {', '.join(allowed_hosts())})")
    return repo, host


# ------------------------------------------------------------------ file selection
SKIP_DIRS = {".git", "node_modules", "vendor", "third_party", "third-party", "bower_components", "dist", "build",
             "target", "__pycache__", ".venv", "venv", "site-packages", "testdata", "fixtures", "__fixtures__", "__tests__"}
CI_FILES = {".gitlab-ci.yml", ".gitlab-ci.yaml", "azure-pipelines.yml", "azure-pipelines.yaml", "cloudbuild.yaml",
            "cloudbuild.yml", "jenkinsfile"}
DOC_EXT = {".md", ".rst", ".adoc", ".txt"}
DOC_BINARY_EXT = {".pdf", ".docx"}
DOC_DIRS = {"doc", "docs", "documentation", "runbook", "runbooks", "playbook", "playbooks", "wiki", "ops", "operations",
            "sre", "adr", "adrs", "architecture", "dr", "oncall", "on-call"}
DOC_WORDS = re.compile(r"runbook|playbook|postmortem|post-mortem|incident|disaster|recovery|\bdr\b|\bslo|\bsla\b|on-?call|"
                       r"architecture|capacity|load[-_ ]?test|operations|observability|monitoring|alerting|security|sop",
                       re.I)
DOC_NOISE = re.compile(r"^(license|licence|changelog|changes|history|contributing|code_of_conduct|authors|notice)", re.I)
SCAN_NAME = re.compile(r"(sbom|bom|cyclonedx|cdx|trivy|grype|sarif|codeql)[^/]*\.json$", re.I)
K8S_DOC = re.compile(r"^apiVersion:\s*\S+", re.M)
K8S_KIND = re.compile(r"^kind:\s*\S+", re.M)


def classify_path(path: str, chart_dirs: set[str]) -> str | None:
    """The category of a repository file worth scanning, '?yaml' for YAML that is kept only if it is a manifest, or None."""
    p = PurePosixPath(path)
    parts = [x.lower() for x in p.parts]
    if any(x in SKIP_DIRS for x in parts[:-1]):
        return None
    base, ext, low = parts[-1], p.suffix.lower(), path.lower()
    if low.startswith(".github/workflows/") and ext in (".yml", ".yaml"):
        return "ci"
    if base in CI_FILES:
        return "ci"
    if low in (".github/dependabot.yml", ".github/dependabot.yaml"):
        return "config"
    if base == "codeowners" and len(parts) <= 2 and (len(parts) == 1 or parts[0] in (".github", "docs")):
        return "codeowners"
    if base.startswith(("dockerfile", "containerfile")) or base.endswith(".dockerfile"):
        return "docker"
    if re.fullmatch(r"(docker-)?compose(\.[\w-]+)?\.ya?ml", base):
        return "config"
    chart = next((d for d in sorted(chart_dirs, key=len, reverse=True) if d == "" or low.startswith(d + "/")), None)
    if chart is not None and ext in (".yaml", ".yml"):
        rel = low[len(chart) + 1:] if chart else low
        if rel in ("chart.yaml",) or re.fullmatch(r"values[\w.-]*\.ya?ml", rel) or rel.startswith("templates/"):
            return "helm"
    if ext in (".yaml", ".yml"):
        return "?yaml"
    if ext == ".sarif" or SCAN_NAME.search(base):
        return "scan"
    if ext in DOC_EXT | DOC_BINARY_EXT and not DOC_NOISE.match(base):
        in_doc_dir = any(x in DOC_DIRS for x in parts[:-1])
        if in_doc_dir or DOC_WORDS.search(base) or (len(parts) == 1 and ext in DOC_EXT) or low in (".github/security.md",):
            return "doc"
    return None


Reader = Callable[[list[str]], dict[str, bytes]]
BATCH = 32
K8S_HINT = re.compile(r"(^|/)(k8s|kube|kubernetes|manifests?|deploy(ment)?s?|kustomize|overlays|base|helm|charts?|ops|infra|"
                      r"platform|gitops|argocd|flux|environments?|clusters?)(/|$)", re.I)


def collect_files(archive: bytes, prefix: str, notes: list[str], code: dict | None = None,
                  progress: Progress | None = None) -> tuple[list[RepoFile], int]:
    """Pick the files to keep from a gh tarball. Members are read in memory and never extracted to disk."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")
        members = [m for m in tar.getmembers() if m.isfile()]
    except (tarfile.TarError, EOFError) as exc:
        raise GitHubError(f"The repository archive could not be read: {exc}") from None
    paths = {}
    for m in members:
        rel = m.name.split("/", 1)[1] if "/" in m.name else ""      # drop the <owner>-<name>-<sha>/ top folder
        if rel and not rel.startswith("/") and ".." not in PurePosixPath(rel).parts:
            paths[rel] = m
    return collect({r: m.size for r, m in paths.items()}, lambda want: {r: tar.extractfile(paths[r]).read() for r in want},
                   prefix, notes, code, progress=progress)


def collect(sizes: dict[str, int], read: Reader, prefix: str, notes: list[str], code: dict | None = None,
            per_file: bool = False, progress: Progress | None = None) -> tuple[list[RepoFile], int]:
    """Choose the files worth scanning from a commit's file list (path -> size) and read them through `read`, in
    batches, stopping at the limits. The same choice is made whether the files come from one archive or one by one.
    With `code` ({}), source files are also read into code["files"] as (path, text) for the code-signals pass (tests
    only by path, to be counted), and code["left_out"] counts those over the limits. They are never stored."""
    max_files = _int("SRA_GITHUB_MAX_FILES", 400)
    max_file = _int("SRA_GITHUB_MAX_FILE_MB", 5) * 1024 * 1024
    max_total = _int("SRA_GITHUB_MAX_TOTAL_MB", 50) * 1024 * 1024
    max_probes = _int("SRA_GITHUB_MAX_YAML_PROBES", 300 if per_file else 100000)
    parents = (PurePosixPath(r).parent.as_posix().lower() for r in sizes if PurePosixPath(r).name.lower() == "chart.yaml")
    chart_dirs = {"" if d == "." else d for d in parents}
    order = {"ci": 0, "codeowners": 1, "config": 2, "docker": 3, "helm": 4, "?yaml": 5, "scan": 6, "doc": 7}
    picked = [(c, r) for c, r in ((classify_path(r, chart_dirs), r) for r in sizes) if c]
    picked.sort(key=lambda x: (order.get(x[0], 9), 0 if x[0] != "?yaml" or K8S_HINT.search(x[1]) else 1, x[1]))
    too_big = [r for _, r in picked if sizes[r] > max_file]
    picked = [(c, r) for c, r in picked if sizes[r] <= max_file]
    yaml = [x for x in picked if x[0] == "?yaml"]
    if len(yaml) > max_probes:                      # one by one, only the likeliest manifests are opened
        drop = {r for _, r in yaml[max_probes:]}
        picked = [x for x in picked if x[1] not in drop]
        notes.append(f"{len(drop)} other YAML file(s) not opened to look for Kubernetes manifests "
                     f"(SRA_GITHUB_MAX_YAML_PROBES={max_probes})")
    progress = progress or NullProgress()
    progress.phase("Reading files", total=len(picked) if per_file else 0,
                   msg=f"{len(picked)} configuration, manifest and document file(s) to look at")
    out, total, skipped, i = [], 0, 0, 0
    while i < len(picked):
        if len(out) >= max_files or total >= max_total:
            skipped += sum(1 for c, _ in picked[i:] if c != "?yaml")
            break
        batch = picked[i:i + BATCH]
        progress.check()
        got = read([r for _, r in batch])
        i += BATCH
        progress.items_done(min(i, len(picked)), batch[-1][1])
        for cat, rel in batch:
            data = got.get(rel)
            if data is None:
                continue
            if cat == "?yaml":
                text = data.decode("utf-8", "replace")
                if not (K8S_DOC.search(text) and K8S_KIND.search(text)):
                    continue
                cat = "k8s"
            if len(out) >= max_files or total + len(data) > max_total:
                skipped += 1
                continue
            out.append(RepoFile(f"{prefix}/{rel}", data, cat, rel))
            total += len(data)
    if code is not None:
        _collect_code(sizes, read, code, notes, per_file, progress)
    if too_big:
        notes.append(f"{len(too_big)} file(s) over {max_file // 1024 // 1024} MB not scanned (SRA_GITHUB_MAX_FILE_MB): "
                     + ", ".join(too_big[:5]) + (" …" if len(too_big) > 5 else ""))
    if skipped:
        notes.append(f"{skipped} matching file(s) left out by the limits (SRA_GITHUB_MAX_FILES={max_files}, "
                     f"SRA_GITHUB_MAX_TOTAL_MB={max_total // 1024 // 1024})")
    return out, skipped + len(too_big)


def _collect_code(sizes: dict[str, int], read: Reader, code: dict, notes: list[str], per_file: bool,
                  progress: Progress) -> None:
    from ..evidence.code_signals import GENERATED, is_test, language
    max_files = _int("SRA_GITHUB_MAX_CODE_FILES", 800 if per_file else 3000)
    max_file = _int("SRA_GITHUB_MAX_CODE_FILE_KB", 1024) * 1024
    max_total = _int("SRA_GITHUB_MAX_CODE_MB", 60) * 1024 * 1024
    files = code.setdefault("files", [])
    wanted, left_out, total = [], 0, 0
    # the team's own code first: shallow paths before deep ones, so a cap keeps the services and drops the corners
    for rel in sorted(sizes, key=lambda r: (r.count("/"), r)):
        parts = [x.lower() for x in PurePosixPath(rel).parts]
        if not language(rel) or any(x in SKIP_DIRS for x in parts[:-1]) or parts[-1].endswith((".min.js", ".d.ts")) \
                or GENERATED.search(rel):
            continue
        if is_test(rel):
            files.append((rel, ""))                  # counted as a test, not read
            continue
        if sizes[rel] > max_file or len(wanted) >= max_files or total + sizes[rel] > max_total:
            left_out += 1
            continue
        wanted.append(rel)
        total += sizes[rel]
    progress.phase("Reading source code", total=len(wanted) if per_file else 0,
                   msg=f"{len(wanted)} source file(s) to analyse")
    for i in range(0, len(wanted), BATCH):
        progress.check()
        got = read(wanted[i:i + BATCH])
        progress.items_done(min(i + BATCH, len(wanted)), wanted[min(i + BATCH, len(wanted)) - 1])
        files += [(r, got[r].decode("utf-8", "replace")) for r in wanted[i:i + BATCH] if r in got]
    if left_out:
        notes.append(f"{left_out} source file(s) not analysed (SRA_GITHUB_MAX_CODE_FILES={max_files}, "
                     f"SRA_GITHUB_MAX_CODE_FILE_KB={max_file // 1024}, SRA_GITHUB_MAX_CODE_MB={max_total // 1024 // 1024})")
    code["left_out"] = left_out


def tree_files(gh: "Gh", repo: str, sha: str, notes: list[str]) -> dict[str, tuple[int, str]]:
    """The commit's files from the git trees API: path -> (size, blob sha). No content is downloaded."""
    tree = gh.api(f"repos/{repo}/git/trees/{sha}", recursive=1) or {}
    if tree.get("truncated"):
        notes.append("GitHub listed only part of this very large repository (git trees API limit); files it left out "
                     "were not considered")
    return {e["path"]: (int(e.get("size") or 0), e["sha"]) for e in tree.get("tree") or []
            if e.get("type") == "blob" and e.get("mode") != "120000" and e.get("path") and ".." not in e["path"].split("/")}


def blob_reader(gh: "Gh", repo: str, blobs: dict[str, tuple[int, str]], notes: list[str], stats: dict) -> Reader:
    """Read files one by one (git blobs API, raw), several at a time (SRA_GITHUB_FETCH_WORKERS, default 8)."""
    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, min(_int("SRA_GITHUB_FETCH_WORKERS", 8), 16))
    cap = _int("SRA_GITHUB_MAX_FILE_MB", 5) * 1024 * 1024 + 1024

    def one(rel: str):
        try:
            return rel, gh.raw(f"repos/{repo}/git/blobs/{blobs[rel][1]}", cap, accept="application/vnd.github.raw+json")
        except GitHubError as exc:
            stats["failed"] = stats.get("failed", 0) + 1
            stats.setdefault("first_error", str(exc)[:200])
            return rel, None

    def read(paths: list[str]) -> dict[str, bytes]:
        with ThreadPoolExecutor(workers) as ex:
            got = {r: d for r, d in ex.map(one, [p for p in paths if p in blobs]) if d is not None}
        stats["fetched"] = stats.get("fetched", 0) + len(got)
        return got
    return read


# ------------------------------------------------------------------ alerts
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


def _unavailable(what: str, exc: GhApiError) -> str:
    why = {403: "no access (the token needs security alert read access, or the feature is off for this repository)",
           404: "not enabled for this repository, or no access"}.get(exc.http or 0, str(exc))
    return f"{what}: not available ({f'HTTP {exc.http}: ' if exc.http else ''}{why})"


def dependabot_vex(alerts: list[dict], repo: str) -> dict:
    """Open Dependabot alerts as CycloneDX (components + vulnerabilities), which the bundle parses as scanner output."""
    comps: dict[str, dict] = {}
    vulns = []
    for a in alerts:
        dep, adv, vul = a.get("dependency") or {}, a.get("security_advisory") or {}, a.get("security_vulnerability") or {}
        pkg = dep.get("package") or vul.get("package") or {}
        name, eco = pkg.get("name", ""), pkg.get("ecosystem", "")
        ref = f"{eco}:{name}@{dep.get('manifest_path', '')}"
        comps.setdefault(ref, {"type": "library", "bom-ref": ref, "name": name,
                               "version": vul.get("vulnerable_version_range", ""), "group": eco})
        ids = adv.get("identifiers") or []
        cve = adv.get("cve_id") or next((i.get("value") for i in ids if i.get("type") == "CVE"), None)
        fixed = (vul.get("first_patched_version") or {}).get("identifier", "")
        vulns.append({"id": cve or adv.get("ghsa_id") or f"dependabot-{a.get('number')}",
                      "description": redact(adv.get("summary") or "")[:300],
                      "ratings": [{"severity": (vul.get("severity") or adv.get("severity") or "unknown").lower()}],
                      "affects": [{"ref": ref}], "recommendation": fixed,
                      "properties": [{"name": "github:alert", "value": str(a.get("number", ""))},
                                     {"name": "github:manifest", "value": dep.get("manifest_path", "")}]})
    return {"bomFormat": "CycloneDX", "specVersion": "1.5",
            "metadata": {"tools": [{"vendor": "GitHub", "name": "dependabot"}], "component": {"type": "application", "name": repo}},
            "components": list(comps.values()), "vulnerabilities": vulns}


def code_scanning_sarif(alerts: list[dict]) -> dict:
    runs: dict[str, dict] = {}
    for a in alerts:
        tool = (a.get("tool") or {}).get("name") or "code-scanning"
        run = runs.setdefault(tool, {"tool": {"driver": {"name": tool, "rules": []}}, "results": []})
        rule = a.get("rule") or {}
        rid = rule.get("id") or f"alert-{a.get('number')}"
        if not any(r["id"] == rid for r in run["tool"]["driver"]["rules"]):
            run["tool"]["driver"]["rules"].append({
                "id": rid, "shortDescription": {"text": redact(rule.get("description") or rid)[:300]},
                "properties": {"security-severity": rule.get("security_severity_level") or rule.get("severity") or "warning"}})
        inst = a.get("most_recent_instance") or {}
        loc = inst.get("location") or {}
        run["results"].append({
            "ruleId": rid, "level": {"error": "error", "warning": "warning", "note": "note"}.get(rule.get("severity"), "warning"),
            "message": {"text": redact((inst.get("message") or {}).get("text") or rule.get("description") or rid)[:300]},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": loc.get("path", "")},
                                                "region": {"startLine": loc.get("start_line") or 1}}}] if loc.get("path") else []})
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": list(runs.values()) or
            [{"tool": {"driver": {"name": "code-scanning", "rules": []}}, "results": []}]}


def secret_scanning_sarif(alerts: list[dict], locations: dict[int, dict]) -> dict:
    """Open secret-scanning alerts. Only the secret type and where it is are kept, never the secret."""
    results = []
    for a in alerts:
        loc = locations.get(a.get("number")) or {}
        kind = a.get("secret_type") or "secret"
        results.append({"ruleId": f"secret:{kind}", "level": "error",
                        "message": {"text": (a.get("secret_type_display_name") or kind)[:200]},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": loc.get("path", "")},
                                                            "region": {"startLine": loc.get("start_line") or 1}}}]
                        if loc.get("path") else []})
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "github-secret-scanning", "rules": []}}, "results": results}]}


# ------------------------------------------------------------------ repository signals
def _yes(v) -> str:
    return "yes" if v else "no"


def _protection_lines(p: dict) -> list[str]:
    out = []
    rv = p.get("required_pull_request_reviews")
    if rv:
        out.append(f"- Required pull request reviews: {rv.get('required_approving_review_count', 0)} approving review(s); "
                   f"code owner review required: {_yes(rv.get('require_code_owner_reviews'))}; "
                   f"stale reviews dismissed: {_yes(rv.get('dismiss_stale_reviews'))}")
    else:
        out.append("- Required pull request reviews: none")
    sc = p.get("required_status_checks")
    if sc:
        checks = sc.get("contexts") or [c.get("context") for c in sc.get("checks") or []]
        out.append(f"- Required status checks: {', '.join(c for c in checks if c) or 'none listed'} "
                   f"(branch must be up to date: {_yes(sc.get('strict'))})")
    else:
        out.append("- Required status checks: none")
    ea = p.get("enforce_admins")
    out.append(f"- Enforced for administrators: {_yes(ea.get('enabled') if isinstance(ea, dict) else ea)}")
    for key, label in (("allow_force_pushes", "Force pushes allowed"), ("allow_deletions", "Branch deletion allowed"),
                       ("required_linear_history", "Linear history required"),
                       ("required_signatures", "Signed commits required")):
        v = p.get(key)
        if v is not None:
            out.append(f"- {label}: {_yes(v.get('enabled') if isinstance(v, dict) else v)}")
    return out


def signals_document(repo: str, host: str, ref: str, sha: str, meta: dict, gh: Gh, alerts: dict, notes: list[str],
                     now: datetime) -> str:
    branch = meta.get("default_branch") or ref
    lines = [f"# GitHub repository signals: {repo}", "",
             f"Source: gh api on {host}, commit {sha} (ref {ref}). Last updated: {now.date().isoformat()}", "",
             "## Repository", "",
             f"- Default branch: {branch}; visibility: {meta.get('visibility') or ('private' if meta.get('private') else 'public')}; "
             f"archived: {_yes(meta.get('archived'))}; last push: {meta.get('pushed_at') or 'unknown'}"]
    saa = meta.get("security_and_analysis") or {}
    if saa:
        lines.append("- Security and analysis: " + "; ".join(f"{k.replace('_', ' ')}: {(v or {}).get('status', 'unknown')}"
                                                            for k, v in sorted(saa.items())))

    lines += ["", f"## Branch protection ({branch})", ""]
    try:
        b = gh.api(f"repos/{repo}/branches/{quote(branch, safe='')}") or {}
        lines.append(f"- Protected: {_yes(b.get('protected'))}")
        try:
            lines += _protection_lines(gh.api(f"repos/{repo}/branches/{quote(branch, safe='')}/protection") or {})
        except GhApiError as exc:
            if b.get("protected"):
                checks = ((b.get("protection") or {}).get("required_status_checks") or {}).get("contexts") or []
                lines.append(f"- Required status checks: {', '.join(checks) or 'none listed'}")
                lines.append(f"- Protection details: not readable ({'needs admin access' if exc.http in (403, 404) else exc})")
    except GhApiError as exc:
        notes.append(_unavailable("Branch protection", exc))
        lines.append("- Not available")
    try:
        rules = gh.api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}", paginate=True) or []
        if rules:
            lines.append("- Rulesets: " + "; ".join(
                r.get("type", "?") + (f" ({', '.join(f'{k} {v}' for k, v in (r.get('parameters') or {}).items() if not isinstance(v, (list, dict)))})"
                                      if r.get("parameters") else "") for r in rules))
        else:
            lines.append("- Rulesets: none apply to this branch")
    except GhApiError:
        pass                                   # older GitHub Enterprise versions have no rulesets API

    lines += ["", "## Environments", ""]
    try:
        envs = (gh.api(f"repos/{repo}/environments") or {}).get("environments") or []
        for e in envs:
            rules = []
            for r in e.get("protection_rules") or []:
                if r.get("type") == "required_reviewers":
                    rules.append(f"{len(r.get('reviewers') or [])} required reviewer(s)")
                elif r.get("type") == "wait_timer":
                    rules.append(f"wait timer {r.get('wait_timer')} min")
                elif r.get("type") == "branch_policy":
                    rules.append("deployment branch policy")
            pol = e.get("deployment_branch_policy") or {}
            if pol.get("protected_branches"):
                rules.append("protected branches only")
            lines.append(f"- {e.get('name')}: {', '.join(rules) or 'no protection rules'}")
        if not envs:
            lines.append("- None configured")
    except GhApiError as exc:
        notes.append(_unavailable("Environments", exc))
        lines.append("- Not available")

    lines += ["", "## Releases (most recent 10)", ""]
    try:
        rels = gh.api(f"repos/{repo}/releases", per_page=10) or []
        if rels:
            lines += ["| Tag | Name | Published | Pre-release |", "|---|---|---|---|"]
            lines += [f"| {r.get('tag_name')} | {redact(r.get('name') or '')[:80]} | {r.get('published_at') or 'draft'} | "
                      f"{_yes(r.get('prerelease'))} |" for r in rels]
        else:
            lines.append("- No releases")
    except GhApiError as exc:
        notes.append(_unavailable("Releases", exc))
        lines.append("- Not available")

    lines += ["", "## Deployments (most recent 10)", ""]
    try:
        deps = gh.api(f"repos/{repo}/deployments", per_page=10) or []
        if deps:
            lines += ["| Environment | Ref | Created | Latest status |", "|---|---|---|---|"]
            for d in deps:
                try:
                    st = gh.api(f"repos/{repo}/deployments/{int(d.get('id'))}/statuses", per_page=1) or []
                    state = st[0].get("state") if st else "none"
                except (GhApiError, TypeError, ValueError):
                    state = "unknown"
                lines.append(f"| {d.get('environment')} | {d.get('ref')} | {d.get('created_at')} | {state} |")
        else:
            lines.append("- No deployments recorded through the GitHub Deployments API")
    except GhApiError as exc:
        notes.append(_unavailable("Deployments", exc))
        lines.append("- Not available")

    label = {"dependabot": "Dependabot", "code_scanning": "Code scanning", "secret_scanning": "Secret scanning"}
    lines += ["", "## Security alerts (open)", ""]
    lines += [f"- {label[k]}: {'not available' if v is None else v}" for k, v in alerts.items()]
    if notes:
        lines += ["", "## Not collected", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ scan
def _fetch(gh: Gh, repo: str, sha: str, meta: dict, prefix: str, notes: list[str], code: dict | None,
           progress: Progress) -> tuple[list[RepoFile], int, str]:
    """Read the commit's files: one tarball when the repository is small enough, otherwise (or when the tarball turns
    out too large) the file list from the git trees API and then only the chosen files, one by one.
    SRA_GITHUB_FETCH = auto (default) | archive | files."""
    mode = (config.setting("SRA_GITHUB_FETCH") or "auto").strip().lower()
    limit = _int("SRA_GITHUB_MAX_ARCHIVE_MB", 300) * 1024 * 1024
    big = int(meta.get("size") or 0) * 1024 > limit * 0.8      # GitHub's size (KB) includes history: a hint only
    if mode == "archive" or (mode == "auto" and not big):
        try:
            progress.phase("Downloading the archive", msg=f"Downloading the commit as one archive (up to {limit // 1024 // 1024} MB)")
            archive = gh.raw(f"repos/{repo}/tarball/{sha}", limit)
            out, skipped = collect_files(archive, prefix, notes, code, progress)
            return out, skipped, "archive"
        except ArchiveTooLarge:
            if mode == "archive":
                raise
            notes.append(f"The archive of this commit is over {limit // 1024 // 1024} MB; files were read one by one")
            progress.warn(notes[-1])
            if code is not None:
                code.clear()
    progress.phase("Listing files", msg="Listing the commit's files (git trees API); only the chosen ones are downloaded")
    blobs = tree_files(gh, repo, sha, notes)
    progress.info(f"{len(blobs)} files in the commit")
    stats: dict = {}
    out, skipped = collect({r: v[0] for r, v in blobs.items()}, blob_reader(gh, repo, blobs, notes, stats), prefix,
                           notes, code, per_file=True, progress=progress)
    notes.append(f"Read {stats.get('fetched', 0)} of {len(blobs)} files one by one "
                 + ("(the repository is too large for one archive)" if big else "(SRA_GITHUB_FETCH)")
                 + (f"; {stats['failed']} could not be read, e.g. {stats['first_error']}" if stats.get("failed") else ""))
    return out, skipped, "files"


def scan(repo: str, ref: str | None = None, host: str | None = None, *, files: bool = True, alerts: bool = True,
         signals: bool = True, code: bool = True, now: datetime | None = None,
         progress: Progress | None = None) -> RepoScan:
    progress = progress or NullProgress()
    repo, host = parse_target(repo, host)
    if ref is not None:
        ref = ref.strip()
        if ref and not REF.match(ref):
            raise GitHubError(f"Not a git ref: {ref!r}")
    progress.phase("Connecting", msg=f"Checking gh's sign-in for {host}")
    check_auth(host)
    gh = Gh(host)
    progress.phase("Resolving the commit", msg=f"Looking up {repo}" + (f" at {ref}" if ref else ""))
    try:
        meta = gh.api(f"repos/{repo}") or {}
    except GhApiError as exc:
        if exc.http in (403, 404):
            raise GitHubError(f"Repository {repo} was not found on {host}, or the signed-in account cannot read it") from None
        raise
    ref = ref or meta.get("default_branch") or "HEAD"
    try:
        sha = (gh.api(f"repos/{repo}/commits/{quote(ref, safe='')}") or {}).get("sha", "")
    except GhApiError as exc:
        if exc.http in (404, 422):
            raise GitHubError(f"Ref {ref!r} was not found in {repo}") from None
        raise
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GitHubError(f"Could not resolve {ref!r} in {repo} to a commit")
    full = meta.get("full_name") or repo
    prefix = f"{host}/{full}@{sha[:12]}"
    result = RepoScan(repository=full, host=host, ref=ref, sha=sha)

    if files:
        sources: dict | None = {} if code else None
        progress.info(f"{full} at {ref} is commit {sha[:12]}")
        result.files, result.skipped, result.fetch = _fetch(gh, repo, sha, meta, prefix, result.notes, sources, progress)
        if sources is not None:
            from ..evidence import code_signals
            progress.phase("Analysing source code", msg=f"Looking for reliability signals in {len(sources.get('files', []))} file(s)")
            doc = code_signals.analyze(sources.get("files", []), full, sha, sources.get("left_out", 0))
            sources.clear()                                    # the code itself is not kept
            result.code = {"files": doc["files_scanned"], "signals": sum(x["count"] for x in doc["signals"]),
                           "by_category": dict(Counter(x["category"] for x in doc["signals"] for _ in range(x["count"]))),
                           "capabilities": sorted(doc["capabilities"])}
            result.files.append(RepoFile(f"{prefix}/github-code-signals.json", json.dumps(doc, indent=1).encode(), "code"))

    if alerts:
        progress.phase("Security alerts", msg="Reading open Dependabot, code-scanning and secret-scanning alerts")
        try:
            dep = gh.api(f"repos/{repo}/dependabot/alerts", paginate=True, state="open", per_page=100)
            result.alerts["dependabot"] = len(dep)
            result.files.append(RepoFile(f"{prefix}/github-dependabot-alerts.cdx.json",
                                         json.dumps(dependabot_vex(dep, full), indent=1).encode(), "alerts"))
        except GhApiError as exc:
            result.alerts["dependabot"] = None
            result.notes.append(_unavailable("Dependabot alerts", exc))
        try:
            cs = gh.api(f"repos/{repo}/code-scanning/alerts", paginate=True, state="open", per_page=100)
            result.alerts["code_scanning"] = len(cs)
            result.files.append(RepoFile(f"{prefix}/github-code-scanning-alerts.sarif.json",
                                         json.dumps(code_scanning_sarif(cs), indent=1).encode(), "alerts"))
        except GhApiError as exc:
            result.alerts["code_scanning"] = None
            result.notes.append(_unavailable("Code-scanning alerts", exc))
        try:
            ss = gh.api(f"repos/{repo}/secret-scanning/alerts", paginate=True, state="open", per_page=100)
            for a in ss:                       # the secret itself is dropped at once and never stored
                a.pop("secret", None)
            locs = {}
            for a in ss[:25]:
                try:
                    found = gh.api(f"repos/{repo}/secret-scanning/alerts/{int(a.get('number'))}/locations", per_page=1) or []
                    if found and found[0].get("type") == "commit":
                        locs[a.get("number")] = found[0].get("details") or {}
                except (GhApiError, TypeError, ValueError):
                    pass
            result.alerts["secret_scanning"] = len(ss)
            result.files.append(RepoFile(f"{prefix}/github-secret-scanning-alerts.sarif.json",
                                         json.dumps(secret_scanning_sarif(ss, locs), indent=1).encode(), "alerts"))
        except GhApiError as exc:
            result.alerts["secret_scanning"] = None
            result.notes.append(_unavailable("Secret-scanning alerts", exc))

    if signals:
        progress.phase("Repository signals", msg="Reading branch protection, environments, releases and deployments")
        doc = signals_document(full, host, ref, sha, meta, gh, result.alerts, result.notes, now or datetime.now(timezone.utc))
        result.files.append(RepoFile(f"{prefix}/github-repository-signals.md", doc.encode(), "signals"))
    return result


# ------------------------------------------------------------------ sign-in (gh auth)
# The server is shared: a sign-in stored in its gh config acts for every user of the app, and on CAP the container's
# config is lost on restart. So production reads GH_TOKEN / GH_ENTERPRISE_TOKEN from an injected secret, and the
# interactive sign-in below is only offered when SRA_GH_INTERACTIVE_LOGIN=true (local and dev) and no environment token
# is set for the host. The app never asks for a GitHub password: it uses gh's device flow (a one-time code the user
# enters on GitHub). Pasting a token is a second opt-in (SRA_GH_TOKEN_LOGIN=true); the token goes to gh on stdin, into
# gh's own credential store, and is never logged, returned or kept by the app.
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# gh before 2.6x: "! First copy your one-time code: ABCD-1234"; gh 2.104: "! One-time code (ABCD-1234) copied to clipboard"
DEVICE_CODE = re.compile(r"one-time code\b\W*([A-Z0-9]{4}-[A-Z0-9]{4})\b", re.I)
DEVICE_URL = re.compile(r"(https?://[^\s]+/login/device[^\s]*)")
STATUS_LINE = re.compile(r"^\s*(?P<mark>\S+)\s+(?:Logged in|Failed to log in) to (?P<host>\S+?) (?:account|as) (?P<user>[^\s(]+)"
                         r"(?:\s+\((?P<src>[^)]*)\))?", re.M)
SCOPES = re.compile(r"Token scopes:\s*(.+)")
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_.-]{20,255}$")
LOGIN_SECONDS = 900                     # GitHub device codes expire after 15 minutes


def env_token_name(host: str) -> str | None:
    """The environment variable that gives gh its token for this host, if one is set (its name only, never its value)."""
    names = ("GH_TOKEN", "GITHUB_TOKEN") if host == "github.com" else ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    return next((n for n in names if config.setting(n)), None)


def interactive_login_allowed(host: str) -> bool:
    return config.flag("SRA_GH_INTERACTIVE_LOGIN", False) and not env_token_name(host)


def token_login_allowed(host: str) -> bool:
    return interactive_login_allowed(host) and config.flag("SRA_GH_TOKEN_LOGIN", False)


def _host_or_default(host: str | None) -> str:
    host = (host or default_host()).strip().lower()
    if not HOST.match(host) or host not in allowed_hosts():
        raise GitHubError(f"Host {host} is not one this server scans (SRA_GITHUB_HOSTS: {', '.join(allowed_hosts())})")
    return host


def _exec(args: list[str], host: str, stdin: str | None = None, timeout: int = 60) -> tuple[int, str]:
    """Run a short gh command; (exit code, stdout + stderr without colour codes). The caller decides what to show."""
    gh = gh_binary()
    try:
        p = subprocess.run([gh, *args], input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=_env(host))
    except subprocess.TimeoutExpired:
        raise GhApiError(f"gh {' '.join(args[:2])} did not finish within {timeout} s") from None
    except OSError as exc:
        raise GhUnavailable(f"The GitHub CLI (gh) could not be started: {exc.strerror or exc}") from None
    return p.returncode, ANSI.sub("", (p.stdout or "") + "\n" + (p.stderr or ""))


def parse_auth_status(text: str, host: str) -> dict:
    """Account, where the credential comes from, and scopes from `gh auth status` (old and new gh output). The masked
    token line gh prints is never read."""
    found = [m for m in STATUS_LINE.finditer(text) if m.group("host").lower() == host]
    if not found:
        return {"signed_in": False, "account": None, "source": None, "scopes": []}
    blocks = [(m, text[m.end():found[i + 1].start() if i + 1 < len(found) else len(text)]) for i, m in enumerate(found)]
    pick, block = next(((m, b) for m, b in blocks if re.search(r"Active account:\s*true", b)), blocks[0])
    ok = "Failed to log in" not in pick.group(0)
    scopes = SCOPES.search(block)
    return {"signed_in": ok, "account": pick.group("user"),
            "source": "environment" if "TOKEN" in (pick.group("src") or "").upper() else "gh",
            "scopes": [s.strip(" '\"") for s in scopes.group(1).split(",") if s.strip(" '\"")] if scopes else [],
            **({} if ok else {"error": "the stored credential is no longer valid; sign in again"})}


def auth_status(host: str | None = None) -> dict:
    """The server's GitHub connection for one host: who and where the credential comes from, never the token."""
    host = _host_or_default(host)
    env = env_token_name(host)
    out = {"host": host, "signed_in": False, "account": None, "source": None, "scopes": [], "env_token": env,
           "interactive_login": interactive_login_allowed(host), "token_login": token_login_allowed(host),
           "login": login_view(host)}
    try:
        code, text = _exec(["auth", "status", "--hostname", host], host, timeout=30)
    except GitHubError as exc:
        return {**out, "error": str(exc)}
    out.update(parse_auth_status(text, host))
    if code != 0 and out["signed_in"]:
        out["signed_in"] = False
        out.setdefault("error", "gh reports a problem with this sign-in; sign in again")
    if env and out["account"]:
        out["source"] = "environment"
    return out


@dataclass
class DeviceLogin:
    """One `gh auth login --web` run: gh prints a one-time code and the device URL, then waits for the user."""
    host: str
    actor: str
    state: str = "starting"           # starting, pending (code shown), done, failed, cancelled, expired
    code: str | None = None
    url: str | None = None
    error: str | None = None
    account: str | None = None
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    proc: subprocess.Popen | None = None
    ready: threading.Event = field(default_factory=threading.Event)

    def view(self) -> dict:
        left = max(0, LOGIN_SECONDS - int((datetime.now(timezone.utc) - self.started).total_seconds()))
        return {"host": self.host, "state": self.state, "code": self.code, "url": self.url, "error": self.error,
                "account": self.account, "started_at": self.started.isoformat(),
                "expires_in": left if self.state == "pending" else 0}


_LOGINS: dict[str, DeviceLogin] = {}
_LOGIN_LOCK = threading.Lock()


def login_view(host: str) -> dict | None:
    with _LOGIN_LOCK:
        s = _LOGINS.get(host)
    return s.view() if s else None


def _require_interactive(host: str) -> None:
    env = env_token_name(host)
    if env:
        raise GitHubError(f"This server signs in to {host} with {env} from its environment; interactive sign-in is off")
    if not config.flag("SRA_GH_INTERACTIVE_LOGIN", False):
        raise GitHubError("Interactive GitHub sign-in is off on this server (a sign-in here would act for every user). "
                          "Set GH_TOKEN / GH_ENTERPRISE_TOKEN from the secret store, or SRA_GH_INTERACTIVE_LOGIN=true for "
                          "local and dev use.")


def start_login(host: str | None, actor: str, wait: float = 20) -> dict:
    """Start gh's device flow and return once gh has printed the code (or failed, or `wait` seconds passed)."""
    host = _host_or_default(host)
    _require_interactive(host)
    gh = gh_binary()
    with _LOGIN_LOCK:
        cur = _LOGINS.get(host)
        if cur and cur.state in ("starting", "pending"):
            return cur.view()               # one sign-in at a time per host
        s = _LOGINS[host] = DeviceLogin(host, actor)
    try:
        s.proc = subprocess.Popen([gh, "auth", "login", "--hostname", host, "--web", "--git-protocol", "https"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  env=_env(host), text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        s.state, s.error = "failed", f"gh could not be started: {exc.strerror or exc}"
        return s.view()
    threading.Thread(target=_follow_login, args=(s,), daemon=True).start()
    s.ready.wait(wait)
    if s.state == "starting":                 # gh is running but printed no code we can read: do not wait 15 minutes
        _stop(s, "failed", "gh did not show a one-time code it could be read from (unexpected gh output). "
                           "Run `sra github login` or `gh auth login` on the server instead.")
        s.state = "failed"
    return s.view()


def _follow_login(s: DeviceLogin) -> None:
    """Read gh's output for the code and URL (nothing else from it is kept), then record how the sign-in ended."""
    timer = threading.Timer(LOGIN_SECONDS, lambda: _stop(s, "expired", "The one-time code expired; start again"))
    timer.start()
    tail: list[str] = []
    try:
        for raw in s.proc.stdout:
            line = ANSI.sub("", raw).strip()
            if not line:
                continue
            tail = (tail + [line])[-3:]
            if m := DEVICE_CODE.search(line):
                s.code = m.group(1)
            if m := DEVICE_URL.search(line):
                s.url = m.group(1)
            if s.code and s.url and s.state == "starting":
                s.state = "pending"
                s.ready.set()
        rc = s.proc.wait()
    finally:
        timer.cancel()
    if s.state in ("cancelled", "expired"):
        s.ready.set()
        return
    if rc == 0:
        try:
            s.account = auth_status(s.host).get("account")
        except GitHubError:
            pass
        s.state = "done"
    else:
        s.state = "failed"
        s.error = redact(" ".join(t for t in tail if not DEVICE_CODE.search(t)))[:300] or f"gh exited with {rc}"
    s.ready.set()


def _stop(s: DeviceLogin, state: str, msg: str | None) -> None:
    if s.proc and s.proc.poll() is None:
        s.state, s.error = state, msg
        s.proc.kill()


def cancel_login(host: str | None) -> dict | None:
    host = _host_or_default(host)
    with _LOGIN_LOCK:
        s = _LOGINS.get(host)
    if s and s.state in ("starting", "pending"):
        _stop(s, "cancelled", None)
        s.state = "cancelled"
    return s.view() if s else None


def login_with_token(host: str | None, token: str) -> dict:
    """Hand a pasted token to `gh auth login --with-token` on stdin. Opt-in (SRA_GH_TOKEN_LOGIN); the token is never
    logged, returned or written anywhere but gh's credential store."""
    host = _host_or_default(host)
    _require_interactive(host)
    if not config.flag("SRA_GH_TOKEN_LOGIN", False):
        raise GitHubError("Signing in with a pasted token is off on this server (SRA_GH_TOKEN_LOGIN)")
    token = (token or "").strip()
    if not TOKEN_SHAPE.match(token):
        raise GitHubError("That does not look like a GitHub token")
    code, text = _exec(["auth", "login", "--hostname", host, "--with-token", "--git-protocol", "https"], host,
                       stdin=token + "\n", timeout=60)
    if code != 0:
        msg = redact(" ".join(line.strip() for line in text.replace(token, "[REDACTED]").splitlines() if line.strip()))
        raise GitHubError(f"gh did not accept the token: {msg[:300] or f'exit code {code}'}")
    return auth_status(host)


def logout(host: str | None) -> dict:
    """`gh auth logout` for the signed-in account. Only for a sign-in stored in gh (not an environment token)."""
    host = _host_or_default(host)
    _require_interactive(host)
    st = auth_status(host)
    if not st["account"]:
        return st
    args = ["auth", "logout", "--hostname", host, "--user", st["account"]]
    code, text = _exec(args, host, timeout=30)
    if code != 0 and "unknown flag" in text:          # gh before 2.40: one account per host and no --user
        code, text = _exec(args[:4], host, timeout=30)
    if code != 0:
        raise GhApiError(f"gh auth logout failed: {redact(' '.join(text.split()))[:300]}")
    return auth_status(host)
