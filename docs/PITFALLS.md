# Pitfalls — why the first benchmark was unreliable

This project exists because a first round of "WER" measurements was wrong.
Documenting the failure modes so future runs stay honest.

## 1. Ad-hoc scorer (the big one)

The first scorer applied hand-rolled canonicalization rules (strip
punctuation, merge ticker spellings, drop "number words") — and the rules
changed between passes. Identical audio produced 1.9% … 9% … 15% … 33%
depending on the rule set. **Fix:** only score with a standard normalizer
(`EnglishTextNormalizer` + `jiwer.process_words`). Never hand-roll token
rules; if a normalization is genuinely needed (e.g. spoken vs digit forms),
define it once, commit it, and apply it to references and hypotheses
symmetrically.

Corollary: naive WER **punishes good behavior**. "twelve point four
percent" → "12.4%" counted as two substitutions in the naive pass. The
official normalizer maps number words to digits on *both* sides, which is
why it is the right tool.

## 2. Synthetic single-voice audio

Samples were generated with macOS `say` (Samantha): one voice, zero noise,
perfect mic. Consequences: tiny content-word counts (one word = 10% WER),
and zero coverage of the hard conditions ASR models actually differ on.
**Fix:** LibriSpeech subsets with official references, 4,000+ reference
words per run. `say`-generated samples are still useful as *qualitative*
formatting tests — just never report WER from them.

## 3. Published-vs-measured gaps have known, benign causes

- Subset: 202 of 2,620 utterances → ±0.5 pt at 95% CI.
- Normalizer: Whisper's vs the HF leaderboard's — close but not identical.
- Precision: MLX 8bit/4bit builds vs bf16 checkpoints in papers.

Expected pattern: measured ≈ published + 0.3–0.5 pt on test-clean subsets.
A *large* deviation means a broken harness, not a bad model.

## 4. Engineering landmines

- **Relative paths**: mlx-audio's audio loader raises FileNotFoundError for
  relative paths depending on cwd. Always pass absolute paths.
- **mlx-audio README is wrong**: `generate_transcription(audio_path=…)`
  fails; the real parameter is `audio=…`.
- **parakeet-mlx shells out to ffmpeg** for loading; a broken Homebrew
  ffmpeg (stale `libbluray` dylib) surfaces as "Failed to load audio".
  `brew reinstall ffmpeg` (or the missing dep) fixes it.
- **EnglishTextNormalizer requires a mapping argument**:
  `EnglishTextNormalizer({})`.
- **jiwer versions differ**: `process_words(...).reference_words` exists in
  some releases only; compute ref length from the normalized refs directly.
- **Per-call overhead dominates short utterances**: mlx-audio re-loads audio
  and parakeet-mlx re-spawns ffmpeg per file; wall-time RTF on 7 s utterances
  understates batch throughput. For throughput numbers, batch or amortize.
- **transformers Qwen3-ASR**: processor call is
  `proc.apply_transcription_request(audio=[wav], sampling_rate=sr)`;
  MPS wants fp16 + `attn_implementation="sdpa"`.

## 5. Trust discipline

- Reconstructing result files from memory corrupts data — one hypothesis
  file had to be regenerated because it was re-typed from a scrolling log.
  Always write engine output straight to disk (`json.dump` in the runner),
  never copy-paste.
- Report subset size and reference word count with every WER; a WER without
  its denominator is marketing.
