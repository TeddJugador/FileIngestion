"""Synthesize the whole app once per session, with cdk-nag enforcing.

``app.synth()`` runs the cdk-nag validation plugin, so any unacknowledged finding fails
every test in this package. Templates are read from the cloud assembly (not
``Template.from_stack``) so the tests assert on exactly what ``cdk synth`` produces.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from app import build_stacks


@dataclass(frozen=True)
class Synthesized:
    shared: Template
    ingestion: Template
    shared_json: dict[str, Any]
    ingestion_json: dict[str, Any]


@pytest.fixture(scope="session")
def fake_layer_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Stand-in for build/layer so infra tests don't need the real arm64 build."""
    root = tmp_path_factory.mktemp("layer")
    (root / "python").mkdir()
    (root / "python" / "placeholder.txt").write_text("test layer")
    return root


@pytest.fixture(scope="session")
def synthesized(fake_layer_dir: Path) -> Iterator[Synthesized]:
    mp = pytest.MonkeyPatch()
    # Environment-agnostic synth: account/region must come out as pseudo parameters.
    mp.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
    mp.delenv("CDK_DEFAULT_REGION", raising=False)
    app = cdk.App()
    stacks = build_stacks(app, layer_asset_path=fake_layer_dir)
    assembly = app.synth()  # raises if cdk-nag reports anything unacknowledged
    shared_json = assembly.get_stack_by_name(stacks.shared.stack_name).template
    ingestion_json = assembly.get_stack_by_name(stacks.ingestion.stack_name).template
    yield Synthesized(
        shared=Template.from_json(shared_json),
        ingestion=Template.from_json(ingestion_json),
        shared_json=shared_json,
        ingestion_json=ingestion_json,
    )
    mp.undo()


def resources_of_type(template: dict[str, Any], resource_type: str) -> dict[str, Any]:
    return {k: v for k, v in template["Resources"].items() if v["Type"] == resource_type}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
