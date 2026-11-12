"""Synthesize the whole app once per session, with cdk-nag enforcing.

``app.synth()`` runs the cdk-nag validation plugin, so any unacknowledged finding fails
every test in this package. Templates are read from the cloud assembly (not
``Template.from_stack``) so the tests assert on exactly what ``cdk synth`` produces.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from app import build_stacks

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def cdk_context(**overrides: Any) -> dict[str, Any]:
    """The context ``cdk synth`` would use (cdk.json), plus overrides."""
    context: dict[str, Any] = json.loads((PROJECT_ROOT / "cdk.json").read_text())["context"]
    return {**context, **overrides}


@dataclass(frozen=True)
class Synthesized:
    shared: Template
    ingestion: Template
    knowledge: Template
    shared_json: dict[str, Any]
    ingestion_json: dict[str, Any]
    knowledge_json: dict[str, Any]


@pytest.fixture(scope="session")
def fake_layer_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Stand-in for build/layer so infra tests don't need the real arm64 build."""
    root = tmp_path_factory.mktemp("layer")
    (root / "python").mkdir()
    (root / "python" / "placeholder.txt").write_text("test layer")
    return root


SynthFn = Callable[..., dict[str, dict[str, Any]]]


@pytest.fixture(scope="session")
def synth(fake_layer_dir: Path) -> SynthFn:
    """Synthesize the whole app with the given context overrides; templates by stack."""

    def run(**context: Any) -> dict[str, dict[str, Any]]:
        mp = pytest.MonkeyPatch()
        # Environment-agnostic synth: account/region must come out as pseudo parameters.
        mp.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
        mp.delenv("CDK_DEFAULT_REGION", raising=False)
        try:
            app = cdk.App(context=cdk_context(**context))
            stacks = build_stacks(
                app, layer_asset_path=fake_layer_dir, knowledge_layer_asset_path=fake_layer_dir
            )
            assembly = app.synth()  # raises if cdk-nag reports anything unacknowledged
            return {
                name: dict(assembly.get_stack_by_name(stack.stack_name).template)
                for name, stack in (
                    ("shared", stacks.shared),
                    ("ingestion", stacks.ingestion),
                    ("knowledge", stacks.knowledge),
                )
            }
        finally:
            mp.undo()

    return run


@pytest.fixture(scope="session")
def synthesized(synth: SynthFn) -> Iterator[Synthesized]:
    templates = synth()
    return Synthesized(
        shared=Template.from_json(templates["shared"]),
        ingestion=Template.from_json(templates["ingestion"]),
        knowledge=Template.from_json(templates["knowledge"]),
        shared_json=templates["shared"],
        ingestion_json=templates["ingestion"],
        knowledge_json=templates["knowledge"],
    )


def resources_of_type(template: dict[str, Any], resource_type: str) -> dict[str, Any]:
    return {k: v for k, v in template["Resources"].items() if v["Type"] == resource_type}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
