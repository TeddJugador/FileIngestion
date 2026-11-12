"""Rebuild the README's evaluation table from eval/results/*.json.

python eval/summarize.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START, END = "<!-- eval-results:start -->", "<!-- eval-results:end -->"
COLUMNS = [
    ("hit_rate_at_1", "Hit @1"),
    ("hit_rate_at_6", "Hit @6"),
    ("mrr", "MRR"),
    ("key_fact_accuracy", "Key facts"),
    ("correct_refusal_rate", "Correct refusals"),
    ("audience_leaks", "Leaks"),
    ("injection_resistance", "Injection"),
    ("latency_ms_mean", "Latency (ms)"),
    ("cost_usd_per_question", "Cost / question"),
]


def table() -> str:
    reports = [
        json.loads(p.read_text()) for p in sorted((ROOT / "eval" / "results").glob("*.json"))
    ]
    if not reports:
        return "_No evaluation runs yet: see `eval/run_eval.py`._"
    header = "| Configuration | " + " | ".join(name for _, name in COLUMNS) + " |"
    rows = [header, "|" + "---|" * (len(COLUMNS) + 1)]
    for report in sorted(reports, key=lambda r: r["ran_at"]):
        summary = report["summary"]
        cells = []
        for key, _ in COLUMNS:
            value = summary.get(key)
            if value is None:
                cells.append("n/a")
            elif key == "cost_usd_per_question":
                cells.append(f"${value:.5f}")
            else:
                cells.append(str(value))
        rows.append(f"| {report['label']} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def update_readme(readme: Path = ROOT / "README.md") -> None:
    text = readme.read_text()
    start, end = text.index(START) + len(START), text.index(END)
    readme.write_text(text[:start] + "\n" + table() + "\n" + text[end:])


if __name__ == "__main__":
    update_readme()
    print(table())
