"""Deterministic LibriSpeech subset manifest builder.

A manifest is a JSON list of {"utt", "path", "ref"} — path relative to the
LibriSpeech extraction root, ref taken from the official *.trans.txt files.
Deterministic subset = every N-th (stride) of the lexicographically sorted
flac list, so every engine sees identical audio.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_manifest(librispeech_dir: str | Path, stride: int = 13) -> list[dict]:
    root = Path(librispeech_dir)
    flacs = sorted(str(p) for p in root.rglob("*.flac"))[::stride]

    refs: dict[str, str] = {}
    for tf in root.rglob("*.trans.txt"):
        for line in tf.read_text().splitlines():
            if line.strip():
                utt, text = line.strip().split(" ", 1)
                refs[utt] = text

    manifest = []
    for path in flacs:
        utt = Path(path).stem
        if utt not in refs:
            raise KeyError(f"no reference for {utt}")
        manifest.append({"utt": utt, "path": path, "ref": refs[utt]})
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--librispeech", required=True, help="path to LibriSpeech/test-clean (or test-other)")
    ap.add_argument("--stride", type=int, default=13, help="keep every N-th utterance (default 13 ≈ 200 utts)")
    ap.add_argument("--out", required=True, help="output manifest JSON path")
    a = ap.parse_args()

    man = build_manifest(a.librispeech, a.stride)
    words = sum(len(m["ref"].split()) for m in man)
    Path(a.out).write_text(json.dumps(man, indent=0))
    print(f"{len(man)} utterances, {words} reference words -> {a.out}")


if __name__ == "__main__":
    main()
