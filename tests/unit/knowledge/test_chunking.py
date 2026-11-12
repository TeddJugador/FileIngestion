from __future__ import annotations

import itertools
import random
import re

import pytest

from knowledge.chunking import (
    MAX_TOKENS,
    OVERLAP_TOKENS,
    Chunk,
    ChunkingError,
    chunk_document,
    split_sentences,
)
from knowledge.extraction import Block, BlockKind, ExtractedDocument
from knowledge.tokens import estimate_tokens

WORDS = [
    "learners",
    "parents",
    "school",
    "fees",
    "uniform",
    "policy",
    "term",
    "assessment",
    "transport",
    "bus",
    "office",
    "principal",
    "grade",
    "class",
    "teacher",
    "sport",
    "library",
    "register",
    "absence",
    "late",
    "payment",
    "discount",
]


def _sentence(rng: random.Random, n: int) -> str:
    words = [rng.choice(WORDS) for _ in range(rng.randint(8, 22))]
    return f"Rule {n} says " + " ".join(words) + "."


def _prose(paragraphs: int, sentences: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    counter = iter(range(10_000))
    return [
        " ".join(_sentence(rng, next(counter)) for _ in range(sentences)) for _ in range(paragraphs)
    ]


def _doc(
    *sections: tuple[tuple[str, ...], list[str]], page: int | None = None
) -> ExtractedDocument:
    return ExtractedDocument(
        blocks=tuple(
            Block(text=p, heading_path=path, page=page) for path, paras in sections for p in paras
        )
    )


def _sentences_of(chunks: list[Chunk]) -> list[str]:
    return [s for c in chunks for s in split_sentences(c.text)]


# --- size, boundaries, overlap -------------------------------------------------------------


def test_chunks_respect_size_bounds() -> None:
    chunks = chunk_document(_doc((("Policy",), _prose(12, 9))), title="Code of Conduct")
    assert len(chunks) > 3
    assert all(20 <= c.tokens <= MAX_TOKENS for c in chunks)
    assert all(c.tokens >= 250 for c in chunks[:-1])  # every chunk but the last is well filled
    assert [c.chunk_no for c in chunks] == list(range(len(chunks)))


def test_no_mid_sentence_splits_in_normal_prose() -> None:
    chunks = chunk_document(_doc(((), _prose(10, 8))), title="T")
    for chunk in chunks:
        assert chunk.text[0].isupper()
        assert chunk.text.rstrip()[-1] in ".!?"
        assert all(re.fullmatch(r"Rule \d+ says [a-z ]+\.", s) for s in split_sentences(chunk.text))


def test_every_sentence_is_kept() -> None:
    paragraphs = _prose(10, 8)
    chunks = chunk_document(_doc(((), paragraphs)), title="T")
    source = {s for p in paragraphs for s in split_sentences(p)}
    assert source <= set(_sentences_of(chunks))


def test_consecutive_chunks_overlap_by_whole_sentences() -> None:
    chunks = chunk_document(_doc(((), _prose(10, 8))), title="T")
    for previous, current in itertools.pairwise(chunks):
        prev_sentences = split_sentences(previous.text)
        overlap = []
        for sentence in split_sentences(current.text):
            if sentence in prev_sentences:
                overlap.append(sentence)
            else:
                break
        assert overlap, "expected some overlap"
        assert overlap == prev_sentences[-len(overlap) :]  # the previous chunk's tail
        assert estimate_tokens(" ".join(overlap)) <= OVERLAP_TOKENS + 1


def test_chunks_never_cross_a_heading() -> None:
    chunks = chunk_document(
        _doc((("Fees",), _prose(4, 8, seed=1)), (("Uniform",), _prose(4, 8, seed=2))),
        title="Handbook",
    )
    assert {c.heading_path for c in chunks} == {("Fees",), ("Uniform",)}
    fee_sentences = {
        s for c in chunks if c.heading_path == ("Fees",) for s in split_sentences(c.text)
    }
    uniform_sentences = {
        s for c in chunks if c.heading_path == ("Uniform",) for s in split_sentences(c.text)
    }
    assert not fee_sentences & uniform_sentences  # no overlap across sections either


def test_paragraphs_are_kept_whole_when_a_chunk_is_already_full_enough() -> None:
    # Two paragraphs of ~350 tokens: the second starts a new chunk rather than being cut.
    first, second = _prose(2, 13, seed=3)
    chunks = chunk_document(_doc(((), [first, second])), title="T")
    assert chunks[0].text.endswith(split_sentences(first)[-1])
    assert split_sentences(second)[0] in chunks[1].text


# --- context and metadata ---------------------------------------------------------------


def test_context_prefix_on_the_embedded_text_only() -> None:
    (chunk,) = chunk_document(
        _doc((("Payment", "Late payment"), ["A penalty of two percent applies after 30 days."])),
        title="Fee Policy 2027",
    )
    assert chunk.context == "Fee Policy 2027 > Payment > Late payment"
    assert chunk.text == "A penalty of two percent applies after 30 days."
    assert chunk.embedding_document.prefixed_text == (
        "Fee Policy 2027 > Payment > Late payment\n\n"
        "A penalty of two percent applies after 30 days."
    )
    assert chunk.heading_path == ("Payment", "Late payment")


def test_page_ranges_follow_the_text() -> None:
    paragraphs = _prose(6, 8)
    document = ExtractedDocument(
        blocks=tuple(Block(text=p, page=i // 2 + 1) for i, p in enumerate(paragraphs)), page_count=3
    )
    chunks = chunk_document(document, title="T")
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 3
    for chunk in chunks:
        assert chunk.page_start is not None
        assert chunk.page_end is not None
        assert chunk.page_start <= chunk.page_end
    assert any(c.page_start != c.page_end for c in chunks)  # a chunk spanning pages


def test_non_pdf_chunks_have_no_pages() -> None:
    (chunk,) = chunk_document(
        _doc(((), ["Short policy text that is long enough to keep as a chunk."])), title="T"
    )
    assert (chunk.page_start, chunk.page_end) == (None, None)


# --- small chunks ----------------------------------------------------------------------


def test_tiny_sections_are_merged_not_lost() -> None:
    chunks = chunk_document(
        _doc((("Fees",), _prose(2, 6)), (("Contact",), ["Call 046 622 1234."])), title="Handbook"
    )
    contact = [c for c in chunks if "046 622 1234" in c.text]
    assert len(contact) == 1
    assert "Contact: Call 046 622 1234." in contact[0].text  # labelled with its heading


def test_tiny_first_section_merges_forward() -> None:
    chunks = chunk_document(
        _doc((("Contact",), ["Call 046 622 1234."]), (("Fees",), _prose(2, 6))), title="Handbook"
    )
    assert chunks[0].text.startswith("Contact: Call 046 622 1234.")


def test_a_short_document_keeps_its_only_chunk() -> None:
    (chunk,) = chunk_document(_doc(((), ["Closed Friday."])), title="Notice")
    assert chunk.text == "Closed Friday."
    assert chunk.tokens < 20


# --- awkward input ----------------------------------------------------------------------


def test_table_rows_are_never_split() -> None:
    rows = [
        f"Grade {g} | Term fee R{g}0 000 | Annual fee R{g}00 000 | Due on the 7th"
        for g in range(1, 120)
    ]
    document = ExtractedDocument(blocks=(Block(text="\n".join(rows), kind=BlockKind.TABLE),))
    chunks = chunk_document(document, title="Fees")
    assert len(chunks) > 1
    for chunk in chunks:
        assert all(line in rows for line in chunk.text.split("\n"))


def test_one_enormous_sentence_is_split_between_words() -> None:
    sentence = "Learners " + " ".join(["must"] * 2000) + " comply."
    chunks = chunk_document(_doc(((), [sentence])), title="T")
    assert len(chunks) > 1
    assert all(c.tokens <= MAX_TOKENS for c in chunks)
    assert all(not c.text.startswith(" ") and not c.text.endswith(" ") for c in chunks)


def test_unbroken_text_with_no_spaces_still_fits() -> None:
    chunks = chunk_document(_doc(((), ["x" * 10_000])), title="T")
    assert all(c.tokens <= MAX_TOKENS for c in chunks)
    assert "".join(c.text for c in chunks).count("x") >= 10_000


def test_context_must_fit_the_embedder() -> None:
    with pytest.raises(ChunkingError):
        chunk_document(
            _doc((("H",), ["Some text that is long enough."])),
            title="T" * 40_000,
            max_input_tokens=8_192,
        )


@pytest.mark.parametrize("seed", range(5))
def test_sentence_splitting_never_loses_characters(seed: int) -> None:
    rng = random.Random(seed)
    pieces = [
        "Fees are due.",
        "Mr. Naidoo said “Pay early.”",
        "(See page 3.)",
        "e.g. R50.",
        "Is it clear?",
        "Yes!",
    ]
    text = " ".join(rng.choice(pieces) for _ in range(40))
    assert re.sub(r"\s", "", "".join(split_sentences(text))) == re.sub(r"\s", "", text)
