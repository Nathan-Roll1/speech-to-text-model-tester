"""Engine registry: load-once / transcribe-many for each supported ear.

Every transcribe function takes an ABSOLUTE audio path and returns plain text.
Model ids can be overridden via run_eval.py --model.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_MODELS = {
    # MLX (mlx-audio) ------------------------------------------------------
    "qwen3asr-mlx": "mlx-community/Qwen3-ASR-1.7B-8bit",
    "voxtral-mlx": "mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit",
    # MLX (parakeet-mlx) ---------------------------------------------------
    "parakeet-mlx": "mlx-community/parakeet-tdt-0.6b-v2",
    # PyTorch MPS (transformers) ------------------------------------------
    "musci-mps": "Musci-research/Musci-ASR-qwen-1019-hf",
}


def _mlx_audio(model_id: str):
    """Shared loader for mlx-audio STT models (Qwen3-ASR, Voxtral, …)."""
    from mlx_audio.stt.utils import load_model
    from mlx_audio.stt.generate import generate_transcription

    model = load_model(model_id)

    def transcribe(path: str) -> str:
        out_base = Path("/tmp") / f"stt-mtx-out-{abs(hash(path))}"
        r = generate_transcription(model=model, audio=path,
                                   output_path=str(out_base), format="txt")
        return getattr(r, "text", str(r)).strip()

    return transcribe


def _parakeet(model_id: str):
    from parakeet_mlx import from_pretrained

    model = from_pretrained(model_id)

    def transcribe(path: str) -> str:
        r = model.transcribe(path)
        return (r.text if hasattr(r, "text") else str(r)).strip()

    return transcribe


def _musci_mps(model_id: str):
    import librosa
    import torch
    from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration

    proc = AutoProcessor.from_pretrained(model_id)
    model = Qwen3ASRForConditionalGeneration.from_pretrained(
        model_id, dtype=torch.float16, attn_implementation="sdpa"
    ).to("mps").eval()

    def transcribe(path: str) -> str:
        wav, sr = librosa.load(path, sr=16000, mono=True)
        inputs = proc.apply_transcription_request(
            audio=[wav], sampling_rate=sr
        ).to("mps", torch.float16)
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=512, do_sample=False)
        gen = out[:, inputs["input_ids"].shape[1]:]
        parsed = proc.decode(gen, return_format="parsed")[0]
        text = parsed["transcription"] if isinstance(parsed, dict) else str(parsed)
        return str(text).strip()

    return transcribe


_LOADERS = {
    "qwen3asr-mlx": _mlx_audio,
    "voxtral-mlx": _mlx_audio,
    "parakeet-mlx": _parakeet,
    "musci-mps": _musci_mps,
}


def get_transcriber(engine: str, model_override: str | None = None):
    if engine not in _LOADERS:
        raise SystemExit(f"unknown engine '{engine}'; known: {sorted(_LOADERS)}")
    model_id = model_override or DEFAULT_MODELS[engine]
    return _LOADERS[engine](model_id)
