"""ApplicationProfile: the facts about an application that drive template selection and thresholds."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Trigger(StrEnum):
    NEW_PRODUCTION = "new_production"
    MAJOR_CHANGE = "major_change"
    ARCHITECTURE_CHANGE = "architecture_change"
    DEPLOYMENT = "deployment"
    POST_INCIDENT = "post_incident"
    PERIODIC = "periodic"


class Hosting(StrEnum):
    ON_PREM = "on_prem"
    CLOUD = "cloud"
    HYBRID = "hybrid"


class ApplicationProfile(BaseModel):
    name: str
    cmdb_id: str = ""
    aliases: list[str] = Field(default_factory=list, description="Other names/support groups used in ServiceNow data")
    tier: int | None = Field(None, ge=1, le=3, description="Service tier (CMDB-AT-10): 1 Critical, 2 Standard, 3")
    priority_class: str | None = Field(None, description="P1/P2/P3 application class")
    trigger: Trigger = Trigger.PERIODIC
    hosting: Hosting | None = None
    containerized: bool = False
    has_cicd: bool = False
    ci_tool: str | None = None
    external_integrations: bool = False
    cross_team_dependencies: bool = False
    vendors: bool = False
    regulated: bool = False
    batch_or_peak_load: bool = False
    has_reliability_automation: bool = False
    sre_toil_work: bool = False
    uses_pse_queue: bool = False
    service_catalog_scope: bool = False
    has_sla: bool = False
    eol_components: bool = False
    incidents_90d: int = 0
    p1_p2_incidents_90d: int = 0
    recurring_patterns: int = 0

    @classmethod
    def from_file(cls, data: bytes) -> tuple[dict, list[str]]:
        """Read a profile.yaml (or JSON) file. Returns (profile as JSON-ready dict, keys that were ignored).

        The name may be missing (the SRE fills it in); every other value must be valid, or a ValueError names it.
        """
        import yaml
        from pydantic import ValidationError
        try:
            raw = yaml.safe_load(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, yaml.YAMLError) as e:
            raise ValueError(f"not a readable YAML or JSON file: {str(e).splitlines()[0]}") from None
        if isinstance(raw, dict) and isinstance(raw.get("profile"), dict):
            raw = raw["profile"]
        if not isinstance(raw, dict):
            raise ValueError("the file must contain the profile fields as a mapping, like eval/scenarios/<app>/profile.yaml")
        known = {k: v for k, v in raw.items() if k in cls.model_fields}
        ignored = sorted(str(k) for k in raw if k not in cls.model_fields)
        if isinstance(known.get("aliases"), str):
            known["aliases"] = [s.strip() for s in known["aliases"].split(",") if s.strip()]
        known = {k: v for k, v in known.items() if v is not None or k in ("tier", "priority_class", "hosting", "ci_tool")}
        try:
            p = cls.model_validate({**known, "name": known.get("name") or "-"})
        except ValidationError as e:
            bad = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            raise ValueError(f"profile has invalid values: {bad}") from None
        out = p.model_dump(mode="json")
        out["name"] = str(known.get("name") or "")
        return out, ignored

    def enrich_from_evidence(self, bundle, as_of) -> "ApplicationProfile":
        """Fill counts and flags the evidence can prove (never clears what the SRE set)."""
        from datetime import timedelta

        from ..detectors.base import Context
        from ..evidence.model import Kind
        ctx = Context(app_name=self.name, aliases=self.aliases, as_of=as_of, cfg={})
        inc = [r for r in ctx.scoped(bundle.table("incident")) if r.get("_opened_at") and r["_opened_at"] >= as_of - timedelta(days=90)]
        upd = {}
        if inc:
            upd["incidents_90d"] = max(self.incidents_90d, len(inc))
            hi = [r for r in inc if str(r.get("priority", "")).strip()[:1] in ("1", "2")]
            upd["p1_p2_incidents_90d"] = max(self.p1_p2_incidents_90d, len(hi))
            from collections import Counter
            c = Counter((r.get("short_description") or "")[:60].lower() for r in inc)
            upd["recurring_patterns"] = max(self.recurring_patterns, sum(1 for k, n in c.items() if k and n >= 3))
        if bundle.of_kind(Kind.CI_CONFIG):
            upd["has_cicd"] = True
            tools = {i.meta.get("tool") for i in bundle.of_kind(Kind.CI_CONFIG)}
            if not self.ci_tool and tools:
                upd["ci_tool"] = sorted(t for t in tools if t)[0] if any(tools) else None
        if bundle.k8s:
            upd["containerized"] = True
        if bundle.table("cmdb_rel_ci"):
            upd["cross_team_dependencies"] = self.cross_team_dependencies or True
        return self.model_copy(update=upd)
