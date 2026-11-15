#!/usr/bin/env bash
# One-time environment setup for speech-to-text-model-tester.
# Python 3.13, Apple Silicon. Creates .venv in the repo root.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

./.venv/bin/python - <<'PY'
import torch, transformers, jiwer
print("torch", torch.__version__, "| mps:", torch.backends.mps.is_available())
print("transformers", transformers.__version__)
from transformers import Qwen3ASRForConditionalGeneration  # noqa: F401
from transformers.models.whisper.english_normalizer import EnglishTextNormalizer
EnglishTextNormalizer({})
print("scoring + Qwen3-ASR classes: OK")
PY
echo "venv ready: ./.venv"
