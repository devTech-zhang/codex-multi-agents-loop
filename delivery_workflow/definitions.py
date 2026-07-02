from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import AGENT_ROOT, DEFAULT_WORKFLOW_ID, PLUGIN_ROOT, WORKFLOW_ROOT


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    name: str
    version: str
    steps: list[dict[str, Any]]
    root: Path

    @property
    def first_step_id(self) -> str:
        return self.steps[0]["id"]

    def step(self, step_id: str) -> dict[str, Any]:
        for step in self.steps:
            if step["id"] == step_id:
                return step
        raise KeyError(f"unknown workflow step: {step_id}")

    def reference_text(self, step: dict[str, Any]) -> str:
        ref = step.get("ref")
        if not ref:
            return ""
        return (self.root / ref).read_text(encoding="utf-8")


def load_workflow(workflow_id: str = DEFAULT_WORKFLOW_ID) -> WorkflowDefinition:
    path = WORKFLOW_ROOT / f"{workflow_id}.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if workflow_id != data["id"]:
        raise KeyError(f"unknown workflow: {workflow_id}")
    return WorkflowDefinition(
        workflow_id=data["id"],
        name=data["name"],
        version=data["version"],
        steps=list(data["steps"]),
        root=PLUGIN_ROOT,
    )


def list_workflows() -> list[dict[str, str]]:
    workflows: list[dict[str, str]] = []
    for path in sorted(WORKFLOW_ROOT.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        workflows.append({"id": data["id"], "name": data["name"], "version": data["version"]})
    return workflows


def load_agent_profile(agent_id: str) -> dict[str, Any]:
    path = AGENT_ROOT / f"{agent_id}.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("id") != agent_id:
        raise KeyError(f"agent profile id mismatch: {agent_id}")
    return data


def list_agent_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for path in sorted(AGENT_ROOT.glob("*.toml")):
        profiles.append(tomllib.loads(path.read_text(encoding="utf-8")))
    return profiles
