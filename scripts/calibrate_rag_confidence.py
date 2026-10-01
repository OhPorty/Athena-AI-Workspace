"""One-off calibration probe for rag.py's search() similarity scores, used to
sanity-check/tune the confidence bands in main.py's _score_confidence_label
(_RAG_HIGH_CONFIDENCE_FLOOR / _RAG_LOW_CONFIDENCE_CEILING).

Runs a handful of genuinely-relevant queries and a handful of nonsense
queries against the live index and prints their top scores side by side, so
a hard cutoff's safety (or lack of it) is visible directly rather than
assumed. Re-run this after any reindex (e.g. after scripts/dedupe_rag_index.py
or a fresh index_codebase pass) since duplicate/stale chunks can distort scores.

Usage: python3 scripts/calibrate_rag_confidence.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rag

GOOD_QUERIES = [
    "tool output rendering dropdown collapse overflow height",
    "read-only bash command allowlist grep sed",
    "embedding model load fastembed onnx",
    "search codebase RAG semantic search",
    "tool call gate exploration threshold delegation",
]

NONSENSE_QUERIES = [
    "how does the quantum flux capacitor rebalance thermal load",
    "stock market prices for cryptocurrency trading bot",
    "medieval castle siege engine blueprint",
    "recipe for sourdough bread starter",
]


def main():
    print("-- known-good queries --")
    for q in GOOD_QUERIES:
        results = rag.search(q, limit=1)
        score = results[0]["score"] if results else None
        print(f"{score!r:>10}  {q}")

    print("\n-- nonsense queries --")
    for q in NONSENSE_QUERIES:
        results = rag.search(q, limit=1)
        score = results[0]["score"] if results else None
        print(f"{score!r:>10}  {q}")


if __name__ == "__main__":
    main()
