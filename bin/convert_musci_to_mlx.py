#!/usr/bin/env python3
"""Convert Musci-ASR-qwen-1019 (HF/transformers format) to MLX, 8-bit.

Musci is a Qwen3-ASR fine-tune, and mlx-audio ships the same architecture —
its Model.sanitize() maps the HF "thinker.*" weight names directly. Flow:
  load HF safetensors -> sanitize -> load_weights -> quantize(8bit) -> save
Output dir is self-contained (weights + config + tokenizer files) and loads
via mlx_audio.stt.utils.load_model().

Run with the asr venv: ~/.pi/agent/venvs/asr/bin/python bin/convert_musci_to_mlx.py
STATUS: BLOCKED — see docs/MUSCI-MLX-CONVERSION.md (audio-head arch drift:
conv_out vs proj1/proj2; strict load fails 314/314). Kept for reference.
"""
import json
import shutil
from pathlib import Path

import mlx.core as mx
from mlx.utils import tree_flatten
from huggingface_hub import snapshot_download
from mlx_audio.stt.models.qwen3_asr.config import ModelConfig
from mlx_audio.stt.models.qwen3_asr.qwen3_asr import Model

SRC = "Musci-research/Musci-ASR-qwen-1019-hf"
OUT = Path.home() / ".pi" / "agent" / "models" / "musci-qwen-1019-mlx-8bit"
GROUP_SIZE, BITS = 64, 8

src = Path(snapshot_download(SRC))
print("source snapshot:", src)

hf_config = json.loads((src / "config.json").read_text())
cfg = ModelConfig.from_dict(hf_config)

print("loading HF weights…")
weights = mx.load(str(src / "model.safetensors"))
# Musci's transformers-native checkpoint keeps the "model." prefix; the MLX
# parameter tree does not (sanitize() only knows the bare/thinker. layouts).
weights = {k[len("model."):] if k.startswith("model.") else k: v
           for k, v in weights.items()}
print(f"  {len(weights)} tensors, "
      f"{sum(v.nbytes for v in weights.values())/1e9:.2f} GB")

model = Model(cfg)
sanitized = model.sanitize(weights)
model.load_weights(list(sanitized.items()), strict=True)
print("load_weights: OK (strict)")

print("quantizing…")
model.quantize(group_size=GROUP_SIZE, bits=BITS)

OUT.mkdir(parents=True, exist_ok=True)
mx.save_safetensors(str(OUT / "model.safetensors"), dict(tree_flatten(model.parameters())))

out_config = dict(hf_config)
out_config["quantization"] = {"group_size": GROUP_SIZE, "bits": BITS}
(OUT / "config.json").write_text(json.dumps(out_config, indent=2))

for fname in ["tokenizer_config.json", "vocab.json", "merges.txt",
              "generation_config.json", "chat_template.jinja", "chat_template.json",
              "preprocessor_config.json"]:
    f = src / fname
    if f.exists():
        shutil.copy(f, OUT / fname)

size = sum(f.stat().st_size for f in OUT.glob("*")) / 1e9
print(f"saved -> {OUT}  ({size:.2f} GB)")
