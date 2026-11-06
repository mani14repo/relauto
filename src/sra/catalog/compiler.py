"""Compile the OIPReqs docs into a reviewable YAML catalog, and load it back.

    python -m sra.catalog            # compile docs/OIPReqs -> catalog/
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .. import config
from .docx_reader import parse_template
from .models import Rubric, Template
from .playbooks import Playbook, parse_playbook, parse_titles
from .rubric import parse_rubric


def compile_catalog(templates_dir: Path = config.TEMPLATES_DIR, out_dir: Path = config.CATALOG_DIR) -> list[Template]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "templates").mkdir(exist_ok=True)
    templates = latest_versions([parse_template(p) for p in sorted(templates_dir.glob("*-AT-*.docx"))])
    playbook_files = sorted(config.PLAYBOOKS_DIR.glob("*-PB-*.docx"))
    if not templates or not playbook_files:   # never overwrite a good catalog with an empty one
        raise RuntimeError(f"found {len(templates)} templates in {templates_dir} and {len(playbook_files)} playbooks; "
                           "check SRA_DOCS_DIR and the docs/OIPReqs folder layout")
    for old in (out_dir / "templates").glob("*.yaml"):     # drop templates that no longer exist
        if old.stem not in {t.id for t in templates}:
            old.unlink()
    for t in templates:
        _dump(out_dir / "templates" / f"{t.id}.yaml", t.model_dump(mode="json"))
    rubric = parse_rubric(config.RUBRIC_DOCX, config.EVIDENCE_XLSX)
    _dump(out_dir / "rubric.yaml", rubric.model_dump(mode="json"))
    index = [{"id": t.id, "title": t.title, "domain": t.domain.value, "variant": t.variant,
              "priority": t.priority, "criteria": len(t.criteria)} for t in templates]
    _dump(out_dir / "index.yaml", {"templates": index, "total_criteria": sum(i["criteria"] for i in index)})
    playbooks = latest_versions([parse_playbook(p) for p in playbook_files])
    _dump(out_dir / "playbooks.yaml", {"playbooks": [pb.model_dump(mode="json") for pb in playbooks]})
    titles = parse_titles(config.STANDARDS_DIR) | {pb.id: pb.title for pb in playbooks}
    _dump(out_dir / "documents.yaml", {"titles": dict(sorted(titles.items()))})
    load_templates.cache_clear()
    load_rubric.cache_clear()
    load_playbooks.cache_clear()
    return templates


def latest_versions(docs: list) -> list:
    """Keep one document per ID: the highest version (a folder may hold both 'X' and 'X — V2')."""
    best: dict[str, object] = {}
    for d in docs:
        cur = best.get(d.id)
        if cur is None or int(d.version.lstrip("Vv") or 1) > int(cur.version.lstrip("Vv") or 1):
            best[d.id] = d
    return [best[k] for k in sorted(best)]


def _dump(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)


@lru_cache
def load_templates(catalog_dir: Path = config.CATALOG_DIR) -> dict[str, Template]:
    out = {}
    for p in sorted((catalog_dir / "templates").glob("*.yaml")):
        with open(p, encoding="utf-8") as f:
            t = Template.model_validate(yaml.safe_load(f))
        out[t.id] = t
    if not out:
        raise FileNotFoundError(f"no compiled templates in {catalog_dir}; run `python -m sra.catalog`")
    return out


@lru_cache
def load_rubric(catalog_dir: Path = config.CATALOG_DIR) -> Rubric:
    with open(catalog_dir / "rubric.yaml", encoding="utf-8") as f:
        return Rubric.model_validate(yaml.safe_load(f))


def template_path(template: Template) -> Path:
    return config.TEMPLATES_DIR / template.source_file


@lru_cache
def load_playbooks(catalog_dir: Path = config.CATALOG_DIR) -> dict[str, Playbook]:
    with open(catalog_dir / "playbooks.yaml", encoding="utf-8") as f:
        return {p["id"]: Playbook.model_validate(p) for p in yaml.safe_load(f)["playbooks"]}


@lru_cache
def load_titles(catalog_dir: Path = config.CATALOG_DIR) -> dict[str, str]:
    with open(catalog_dir / "documents.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["titles"]
