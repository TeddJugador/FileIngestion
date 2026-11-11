"""The worker's metrics reach CloudWatch as EMF with exactly the intended dimensions."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ingestion.handlers.etl_worker import PowertoolsMetricsSink


def _emf(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]


def test_each_metric_is_its_own_emf_document(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "ingestion")
    sink = PowertoolsMetricsSink("SchoolSystem/Ingestion")
    sink.record("ValidRecordsCount", 3, "Count", {"DatasetType": "marks"})
    sink.record(
        "ImportsFailedCount", 1, "Count", {"DatasetType": "marks", "ReasonCode": "UNREADABLE"}
    )
    sink.record("PipelineLatency", 512.0, "Milliseconds", {"DatasetType": "marks"})

    valid, failed, latency = _emf(capsys)
    for doc in (valid, failed, latency):
        (directive,) = doc["_aws"]["CloudWatchMetrics"]
        assert directive["Namespace"] == "SchoolSystem/Ingestion"

    assert valid["ValidRecordsCount"] in (3.0, [3.0])  # EMF allows a value or a list
    assert valid["DatasetType"] == "marks"
    assert "ReasonCode" not in valid  # ReasonCode only on ImportsFailedCount

    (failed_dims,) = failed["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert {"DatasetType", "ReasonCode"} <= set(failed_dims)
    assert failed["ReasonCode"] == "UNREADABLE"

    (latency_metric,) = latency["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    assert latency_metric == {"Name": "PipelineLatency", "Unit": "Milliseconds"}
