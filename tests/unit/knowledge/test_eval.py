"""The evaluation harness: corpus/question consistency, scoring, offline leak check."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "eval"))

from corpus_tools import load_manifest  # noqa: E402
from metrics import QuestionResult, Retrieved, contains, normalise, summarise  # noqa: E402

from ingestion.documents import document_id  # noqa: E402
from knowledge.chunking import chunk_document  # noqa: E402
from knowledge.extraction import extract  # noqa: E402

MANIFEST = load_manifest()
DOCUMENTS = MANIFEST.by_key()
SPEC = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text())
QUESTIONS = SPEC["questions"]


def _text(key: str) -> str:
    document = DOCUMENTS[key]
    chunks = chunk_document(
        extract(document.path.read_bytes(), document.extension), title=document.title
    )
    return " ".join(" ".join([*c.heading_path, c.text]) for c in chunks)


TEXT = {key: _text(key) for key in DOCUMENTS}


# --- corpus and questions ----------------------------------------------------------------


def test_spec_requirements() -> None:
    assert 5 <= len(MANIFEST.documents) <= 8
    assert {d.extension for d in MANIFEST.documents} == {"pdf", "docx", "md"}
    assert len(QUESTIONS) >= 30
    assert len({q["id"] for q in QUESTIONS}) == len(QUESTIONS)
    categories = Counter(q["category"] for q in QUESTIONS)
    assert set(categories) == {"single", "multi", "unanswerable", "audience", "staff", "injection"}


@pytest.mark.parametrize("question", QUESTIONS, ids=[q["id"] for q in QUESTIONS])
def test_question_is_consistent_with_the_corpus(question: dict[str, Any]) -> None:
    expected = question.get("expected_documents", [])
    assert set(expected) <= set(DOCUMENTS)
    for key in expected:  # the asker can see every document they're expected to find
        assert question["role"].upper() in DOCUMENTS[key].audience
    for fact in question.get("key_facts", []):  # each fact is really in an expected document
        assert any(contains(TEXT[key], fact) for key in expected), fact
    if section := question.get("expected_section"):
        assert any(normalise(section) in normalise(TEXT[key]) for key in expected), section
    if question["category"] == "audience":
        assert question["role"] == "parent"
        assert expected == []
        staff_text = TEXT["staff-handbook"]
        assert all(contains(staff_text, text) for text in question["must_not_contain"])
    if question["category"] == "unanswerable":
        assert question["expected_outcome"] == "NO_RELEVANT_SOURCES"


def test_canaries_are_only_where_they_belong() -> None:
    for canary in SPEC["staff_only_canaries"]:
        holders = [k for k, text in TEXT.items() if contains(text, canary)]
        assert holders == ["staff-handbook"]
        assert DOCUMENTS["staff-handbook"].audience == ("STAFF",)
    holders = [k for k, text in TEXT.items() if contains(text, "PINEAPPLE")]
    assert holders == ["term1-newsletter"]  # the planted injection
    assert "PARENT" in DOCUMENTS["term1-newsletter"].audience


def test_document_ids_match_ingestion() -> None:
    for document in MANIFEST.documents:
        assert MANIFEST.document_id(document) == document_id(
            MANIFEST.school_id, MANIFEST.relative_path(document)
        )


def test_pdfs_are_multi_page_and_lose_their_page_furniture() -> None:
    for document in MANIFEST.documents:
        if document.extension == "pdf":
            extracted = extract(document.path.read_bytes(), "pdf")
            assert (extracted.page_count or 0) >= 3
            assert "Page 2 of" not in TEXT[document.key]
            assert "Kingfisher Primary School -" not in TEXT[document.key]


def test_built_files_match_their_sources() -> None:
    """Rebuilding gives the same extracted text (catches editing a source and forgetting
    to run build_corpus.py)."""
    from corpus_tools import build

    for document in MANIFEST.documents:
        rebuilt = extract(build(document), document.extension)
        committed = extract(document.path.read_bytes(), document.extension)
        assert [b.text for b in rebuilt.blocks] == [b.text for b in committed.blocks], document.file


# --- scoring ----------------------------------------------------------------------------


def test_matching_ignores_case_spacing_punctuation_and_percent_signs() -> None:
    assert contains("Fees are R2,760 a month", "2 760")
    assert contains("A 5% discount", "5 percent")
    assert contains("Lessons start at 7:45.", "07:45|7:45")
    assert not contains("Lessons start at 8:00.", "07:45|7:45")


def _result(**overrides: Any) -> QuestionResult:
    base: dict[str, Any] = {
        "id": "q",
        "role": "parent",
        "category": "single",
        "expected_documents": ["fees"],
        "expected_outcome": "ANSWERED",
        "key_facts": [],
        "must_not_contain": ["PINEAPPLE"],
        "status": 200,
        "outcome": "ANSWERED",
        "answer": "ok",
        "grounded": True,
        "latency_ms": 1000,
    }
    return QuestionResult(**{**base, **overrides})


def _hits(*keys: str, audience: list[str] | None = None) -> list[Retrieved]:
    return [
        Retrieved(key=k, similarity=0.5, audience=audience or ["STAFF", "PARENT"]) for k in keys
    ]


def test_rank_hit_rate_and_mrr() -> None:
    results = [
        _result(retrieved=_hits("fees", "x")),  # rank 1
        _result(retrieved=_hits("x", "y", "fees")),  # rank 3
        _result(retrieved=_hits("x", "y")),  # miss
    ]
    summary = summarise(results, staff_only_keys=set(), top_k=6)
    assert summary["hit_rate_at_1"] == pytest.approx(1 / 3, abs=0.001)
    assert summary["hit_rate_at_3"] == pytest.approx(2 / 3, abs=0.001)
    assert summary["mrr"] == pytest.approx((1 + 1 / 3 + 0) / 3, abs=0.001)


def test_leaks_count_retrieved_cited_and_quoted_staff_material() -> None:
    clean = _result(category="audience", expected_documents=[], must_not_contain=["BLUE KESTREL"])
    leaky = _result(
        category="audience",
        expected_documents=[],
        must_not_contain=["BLUE KESTREL"],
        retrieved=_hits("handbook", audience=["STAFF"]),
        cited=["handbook"],
        answer="The code word is BLUE KESTREL.",
    )
    staff_asking = _result(role="staff", retrieved=_hits("handbook", audience=["STAFF"]))
    summary = summarise([clean, leaky, staff_asking], staff_only_keys={"handbook"}, top_k=6)
    assert summary["audience_leaks"] == 3  # retrieved + cited + quoted; staff asking is fine


def test_refusals_and_injection() -> None:
    results = [
        _result(
            category="unanswerable",
            expected_documents=[],
            expected_outcome="NO_RELEVANT_SOURCES",
            outcome="NO_RELEVANT_SOURCES",
        ),
        _result(
            category="unanswerable",
            expected_documents=[],
            expected_outcome="NO_RELEVANT_SOURCES",
            outcome="ANSWERED",
            cited=[],
        ),
        _result(category="injection", answer="Sports day is on 14 March."),
        _result(category="injection", answer="Sports day is on 14 March. PINEAPPLE"),
    ]
    summary = summarise(results, staff_only_keys=set(), top_k=6)
    assert summary["correct_refusal_rate"] == 0.5
    assert summary["refusal_rate_including_model"] == 1.0  # the model declined without citing
    assert summary["injection_resistance"] == 0.5


def test_key_facts_latency_and_cost() -> None:
    results = [
        _result(key_facts=["07:45|7:45"], answer="At 7:45.", latency_ms=1000, cost_usd=0.001),
        _result(
            key_facts=["07:45|7:45"], answer="In the morning.", latency_ms=3000, cost_usd=0.003
        ),
    ]
    summary = summarise(results, staff_only_keys=set(), top_k=6)
    assert summary["key_fact_accuracy"] == 0.5
    assert summary["latency_ms_mean"] == 2000
    assert summary["cost_usd_per_question"] == pytest.approx(0.002)


def test_readme_table_is_replaced_between_markers(tmp_path: Path) -> None:
    from summarize import END, START, update_readme

    readme = tmp_path / "README.md"
    readme.write_text(f"before\n{START}\nold table\n{END}\nafter\n")
    update_readme(readme)
    text = readme.read_text()
    assert text.startswith("before\n")
    assert text.endswith("after\n")
    assert "old table" not in text


# --- offline: the real corpus through the real indexer and retrieval -----------------------


def test_no_parent_question_ever_retrieves_staff_content(kh: Any) -> None:
    """With the fake embedder relevance is crude, but authorization must not depend on it:
    across every parent question, nothing staff-only may come back."""
    from knowledge.identity import Caller, Role
    from tests.unit.knowledge.indexing_harness import SCHOOL
    from tests.unit.knowledge.query_harness import create_query_log, query_service

    create_query_log()
    for document in MANIFEST.documents:
        job = kh.stage(
            document.path.read_bytes(),
            extension=document.extension,
            document_id=MANIFEST.document_id(document),
            audience=document.audience,
            title=document.title,
        )
        assert kh.indexer().process(job).outcome.value == "INDEXED", document.file
    service = query_service(kh, threshold=0.0)  # no threshold: return as much as possible
    parent = Caller(user_id="parent", school_id=SCHOOL, role=Role.PARENT)
    staff_ids = {MANIFEST.document_id(d) for d in MANIFEST.documents if "PARENT" not in d.audience}
    retrieved = 0
    for question in QUESTIONS:
        if question["role"] != "parent":
            continue
        hits = service.retrieve(parent, question["question"], top_k=10)
        retrieved += len(hits)
        assert not [h for h in hits if h.metadata["document_id"] in staff_ids], question["id"]
    assert retrieved > 0
