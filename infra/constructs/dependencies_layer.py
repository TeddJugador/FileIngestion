"""Shared arm64 dependencies layer (Powertools, pydantic, ijson, python-ulid).

The layer is built outside CDK by ``scripts/build_layer.sh`` (pip cross-install for
manylinux2014_aarch64, no Docker). The Python version lives in ``.lambda-python-version``
so the build script and the Lambda runtime can't drift apart.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import RemovalPolicy
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_VERSION = (PROJECT_ROOT / ".lambda-python-version").read_text().strip()

# Python 3.14 is the newest runtime that is GA on Lambda *and* supported by Powertools
# at build time (Lambda's python3.15 is a public preview and Powertools stops at 3.14).
LAMBDA_RUNTIME = lambda_.Runtime(f"python{PYTHON_VERSION}", lambda_.RuntimeFamily.PYTHON)
LAMBDA_ARCHITECTURE = lambda_.Architecture.ARM_64

DEFAULT_LAYER_ASSET_PATH = PROJECT_ROOT / "build" / "layer"


class DependenciesLayer(Construct):
    """Wraps the pre-built layer directory as a Lambda LayerVersion."""

    def __init__(self, scope: Construct, construct_id: str, *, asset_path: str | Path) -> None:
        super().__init__(scope, construct_id)

        path = Path(asset_path)
        if not (path / "python").is_dir():
            raise FileNotFoundError(
                f"Dependencies layer not found at {path}/python. "
                "Run scripts/build_layer.sh before `cdk synth`."
            )

        self.layer = lambda_.LayerVersion(
            self,
            "Layer",
            code=lambda_.Code.from_asset(str(path)),
            compatible_runtimes=[LAMBDA_RUNTIME],
            compatible_architectures=[LAMBDA_ARCHITECTURE],
            description=(
                f"Ingestion deps (Powertools, pydantic, ijson, python-ulid) "
                f"for python{PYTHON_VERSION}/arm64"
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
