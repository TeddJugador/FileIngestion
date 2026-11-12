"""CloudWatch metrics (EMF via Powertools), one document per metric so each carries
exactly its own dimensions. Also the provider-error observer for the HTTP caller."""

from __future__ import annotations

from aws_lambda_powertools.metrics import MetricUnit, single_metric


class PowertoolsMetricsSink:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    def record(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
        with single_metric(
            name=name, unit=MetricUnit(unit), value=value, namespace=self._namespace
        ) as metric:
            for key, dim_value in dimensions.items():
                metric.add_dimension(name=key, value=dim_value)

    def provider_error(self, provider: str, status_code: int | None) -> None:
        """``ErrorObserver``: counts final provider failures (after retries)."""
        self.record("ProviderErrorCount", 1, "Count", {"Provider": provider})
