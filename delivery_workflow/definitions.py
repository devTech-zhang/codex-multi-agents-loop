from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import DEFAULT_WORKFLOW_ID, PACKAGE_ROOT, WORKFLOW_FILE


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
    data = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    if workflow_id != data["id"]:
        raise KeyError(f"unknown workflow: {workflow_id}")
    return WorkflowDefinition(
        workflow_id=data["id"],
        name=data["name"],
        version=data["version"],
        steps=list(data["steps"]),
        root=PACKAGE_ROOT,
    )


def list_workflows() -> list[dict[str, str]]:
    data = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    return [{"id": data["id"], "name": data["name"], "version": data["version"]}]
