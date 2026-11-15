"""Corpus-level WER scoring: jiwer + Whisper's official EnglishTextNormalizer.

This is the scoring stack that produced the validated numbers in
results/RESULTS.md. Do NOT replace it with ad-hoc token canonicalization —
that is exactly what made the first round of measurements unreliable.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jiwer import process_words
from transformers.models.whisper.english_normalizer import EnglishTextNormalizer


def score(manifest_path: str, hyp_paths: list[str]) -> None:
    N = EnglishTextNormalizer({})
    manifest = json.loads(Path(manifest_path).read_text())

    header = f"{'engine':36} {'utt-err':>7} {'WER%':>7} {'subs':>6} {'del':>5} {'ins':>5}"
    print(header)
    print("-" * len(header))

    for hp in hyp_paths:
        hyps = json.loads(Path(hp).read_text())
        refs, hyp_list, errors = [], [], 0
        for m in manifest:
            hyp = hyps.get(m["utt"], "")
            if hyp.startswith("__ERROR__") or hyp == "":
                errors += 1
                hyp = ""
            refs.append(N(m["ref"]))
            hyp_list.append(N(hyp))

        res = process_words(refs, hyp_list)
        n_ref = sum(len(r.split()) for r in refs)
        S, D, I = res.substitutions, res.deletions, res.insertions
        wer_pct = 100 * (S + D + I) / max(n_ref, 1)
        name = Path(hp).stem.replace("hyp_", "")
        print(f"{name:36} {errors:>7} {wer_pct:>6.2f} {S:>6} {D:>5} {I:>5}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--hyps", nargs="+", required=True, help="hyp_*.json files")
    a = ap.parse_args()
    score(a.manifest, a.hyps)


if __name__ == "__main__":
    main()
