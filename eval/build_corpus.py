"""Write eval/corpus/<file> for every manifest entry from its Markdown source.

python eval/build_corpus.py
"""

from __future__ import annotations

from corpus_tools import build, load_manifest


def main() -> None:
    for document in load_manifest().documents:
        document.path.write_bytes(build(document))
        print(f"built {document.file} ({document.path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
