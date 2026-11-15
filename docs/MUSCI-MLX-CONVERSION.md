# Why Musci cannot be converted to MLX (yet)

Musci-ASR-qwen-1019 is a GRPO fine-tune of **Qwen3-ASR**, and mlx-audio ships
an MLX implementation of that architecture — so a weight conversion looked
mechanical. It is not: the two audio encoders have **drifted apart**.

## What matches

| Part | Musci (HF) | MLX tree | Fix |
|---|---|---|---|
| Weight names | `model.audio_tower.*`, `model.language_model.*` (transformers-native prefix) | bare names (`audio_tower.*`) | strip `model.` prefix |
| Text stack | `language_model.layers.*` | `model.layers.*` | rename `language_model.` → `model.` |

## What does NOT match

Audio-tower head, structural difference:

```
MLX (official Qwen3-ASR layout):      Musci (older transformers layout):
  conv2d1-3  (480,3,3,1)                conv2d1-3  (480,1,3,3)  ← PyTorch axes
  conv_out   (1024×7680)                conv_out   (1024×7680)  ✓ same
  ln_post                               ln_post                 ✓ same
  proj1      (1024×1024)                — does not exist
  proj2      (2048×1024)                — does not exist
```

`proj1` + `proj2` (~3.1M params) have no counterpart in the Musci
checkpoint. A franken-model with random/identity projections produces
garbage audio features; identity-folding the external projector into
proj2 is blocked by unknown activation placement between the heads
(verified against `mlx_audio/stt/models/qwen3_asr/qwen3_asr.py`).

Also note conv kernels are stored PyTorch-layout `(out,in,kh,kw)` vs MLX
`(out,kh,kw,in)` — needs a transpose when converting anything that *is*
mappable.

## Status

- `bin/convert_musci_to_mlx.py` — kept as reference; fails at
  `load_weights(strict=True)` with 314 missing/314 unexpected keys.
- If a future mlx-audio version adds a Qwen3-ASR converter that handles the
  transformers-native (conv_out) layout, retry — conversion would give
  Musci accuracy at MLX speed (~4–5× current).
- Meanwhile the faster local alternative is `Qwen3-ASR-1.7B-8bit` (MLX):
  1.82% WER (vs Musci 1.63%, statistical tie) and ~2–5× lower latency.
  It was briefly wired into the dictation server as `engine=qwen`, then
  removed (2026-11-15) — Musci is the daily default and the second resident
  model's ~2 GB RAM was not justified. Re-add via the server's git history
  (dual-model version) or the mlx-audio harness in this repo.
