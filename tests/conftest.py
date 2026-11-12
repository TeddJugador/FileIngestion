"""Integration tests run only when asked for by name (``pytest -m integration``).

A default ``-m "not integration"`` in pyproject.toml is replaced, not extended, by any
``-m`` on the command line, so ``pytest -m "not slow"`` would run them. Deselecting them
here keeps them opt-in whatever other marker expression is used.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "integration" in (config.option.markexpr or ""):
        return
    selected, deselected = [], []
    for item in items:
        (deselected if item.get_closest_marker("integration") else selected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
