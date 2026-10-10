"""Fault knowledge base: what each catalog fault *means* (docs/KNOWLEDGE_BASE.md).

catalog.yaml says how a fault is injected (template, tier, limits). knowledge/<fault>.yaml says what it is
expected to do: hypothesis, expected symptoms (detector ids), the symptom signature Agent 2's fallback
matches against, grading keywords and related playbook fixes. Agents, score and report read faults only
through this registry, so adding or removing a fault is a data change, not a code change.

A catalog fault without a knowledge file still works: get() returns generic defaults (documented=False)
and `chaosctl kb` reports the gap.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

KB_DIR = Path(__file__).parent / "knowledge"
PLAYBOOK_PATH = Path(__file__).parent / "playbook.yaml"


class Signature(BaseModel):
    """Blind symptom pattern: matched against detector hits only, never against what was injected."""
    any_of: list[str] = Field(default_factory=list, description="matches if at least one of these detectors fired")
    all_of: list[str] = Field(default_factory=list, description="and all of these fired")
    root_cause: str
    priority: int = Field(0, description="higher wins when several signatures match")


class Symptoms(BaseModel):
    expected: list[str] = Field(default_factory=list, description="detector ids this fault should trigger")
    possible: list[str] = Field(default_factory=list, description="detector ids it may trigger")


class FaultKnowledge(BaseModel):
    fault: str
    alias: str | None = None
    title: str
    category: str = "uncategorized"
    summary: str = ""
    hypothesis: str | None = None
    symptoms: Symptoms = Field(default_factory=Symptoms)
    signature: Signature | None = None
    grading_keywords: list[str] = Field(default_factory=list)
    related_fixes: list[str] = Field(default_factory=list)
    documented: bool = True

    def keywords_for(self, target: str) -> list[str]:
        return self.grading_keywords or [target]


class FaultKB:
    def __init__(self, entries: dict[str, FaultKnowledge], catalog: dict | None = None):
        self.entries = entries
        self.catalog = catalog

    def get(self, fault: str) -> FaultKnowledge:
        if fault in self.entries:
            return self.entries[fault]
        return FaultKnowledge(fault=fault, title=fault.replace("_", " "), documented=False)

    def aliases(self) -> dict[str, str]:
        return {e.alias: f for f, e in self.entries.items() if e.alias}

    def resolve(self, name: str) -> str:
        return self.aliases().get(name.upper(), name)

    def signatures(self) -> list[tuple[str, Signature]]:
        sigs = [(f, e.signature) for f, e in self.entries.items() if e.signature]
        return sorted(sigs, key=lambda x: -x[1].priority)

    def match(self, detector_ids: set[str]) -> tuple[str, Signature] | None:
        """Best blind match of fired detectors against all known signatures."""
        for fault, sig in self.signatures():
            if set(sig.all_of) <= detector_ids and (not sig.any_of or set(sig.any_of) & detector_ids):
                return fault, sig
        return None

    def check(self, detector_ids: set[str] | None = None) -> list[str]:
        """Consistency problems between catalog, knowledge files, playbook and detectors."""
        problems = []
        cat = (self.catalog or {}).get("faults", {})
        fixes = set(yaml.safe_load(PLAYBOOK_PATH.read_text())["fixes"])
        for f in cat:
            if f not in self.entries:
                problems.append(f"catalog fault {f!r} has no knowledge/{f}.yaml (generic defaults are used)")
        seen_alias: dict[str, str] = {}
        for f, e in self.entries.items():
            if cat and f not in cat:
                problems.append(f"knowledge/{f}.yaml describes a fault that is not in catalog.yaml")
            if e.alias:
                if e.alias in seen_alias:
                    problems.append(f"alias {e.alias} used by both {seen_alias[e.alias]} and {f}")
                seen_alias[e.alias] = f
            for fx in e.related_fixes:
                if fx not in fixes:
                    problems.append(f"{f}: related fix {fx!r} is not in playbook.yaml")
            if detector_ids is not None:
                used = set(e.symptoms.expected) | set(e.symptoms.possible)
                if e.signature:
                    used |= set(e.signature.any_of) | set(e.signature.all_of)
                for d in sorted(used - detector_ids):
                    problems.append(f"{f}: unknown detector id {d!r}")
        return problems


def load_kb(catalog: dict | None = None, kb_dir: Path = KB_DIR) -> FaultKB:
    if catalog is None:
        from chaosctl.chaos.chaosmesh import load_catalog
        catalog = load_catalog()
    entries = {}
    for p in sorted(kb_dir.glob("*.yaml")):
        e = FaultKnowledge.model_validate(yaml.safe_load(p.read_text()))
        if e.fault != p.stem:
            raise ValueError(f"{p.name}: fault {e.fault!r} must match the file name")
        entries[e.fault] = e
    return FaultKB(entries, catalog)
