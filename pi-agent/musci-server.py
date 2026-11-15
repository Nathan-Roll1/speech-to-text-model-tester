#!/usr/bin/env python3
"""Resident Musci-ASR transcription server (local, 127.0.0.1 only).

Model stays loaded; dictation latency after stop ≈ 1 s instead of 6–10 s
model-load-per-utterance. Managed by LaunchAgent com.roman.musci-server
(KeepAlive) so it is always warm for browser-side dictation.

  GET  /health      -> {"ok": true, "model": ...}
  POST /transcribe  (raw 16 kHz mono wav bytes) -> {"text": "..."}

CORS is open (bound to 127.0.0.1 only) so the pi-web page can reach it
directly; remote clients (iPhone) fall back to Deepgram instead.

Run with: ~/.pi/agent/venvs/asr/bin/python ~/.pi/agent/bin/musci-server.py
"""
import io
import json
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 31046
MODEL_ID = "Musci-research/Musci-ASR-qwen-1019-hf"
KEEP_WARM_S = 240  # tiny self-transcription keeps weights + kernels resident

import torch  # noqa: E402
import soundfile as sf  # noqa: E402
from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration  # noqa: E402

print("loading model…", flush=True)
t0 = time.time()
_proc = AutoProcessor.from_pretrained(MODEL_ID)
_model = Qwen3ASRForConditionalGeneration.from_pretrained(
    MODEL_ID, dtype=torch.float16, attn_implementation="sdpa"
).to("mps").eval()
print(f"model ready in {time.time()-t0:.1f}s", flush=True)

_lock = threading.Lock()


def _silence_wav() -> bytes:
    """0.5 s of silence as a 16 kHz mono PCM16 wav, in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)
    return buf.getvalue()


def transcribe_wav(b: bytes) -> str:
    # soundfile fast path: dictation wavs are 16 kHz mono already (rec -r 16000,
    # browser worklet resamples). Falls back to librosa for anything else.
    try:
        wav, sr = sf.read(io.BytesIO(b), dtype="float32")
        if wav.ndim > 1:
            wav = wav.mean(axis=1)
        if sr != 16000:
            import librosa
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
        sr = 16000
    except Exception:  # noqa: BLE001 — exotic container, let librosa try
        import librosa
        wav, sr = librosa.load(io.BytesIO(b), sr=16000, mono=True)
    inputs = _proc.apply_transcription_request(
        audio=[wav], sampling_rate=sr
    ).to("mps", torch.float16)
    with torch.no_grad():
        out = _model.generate(**inputs, max_new_tokens=512, do_sample=False)
    gen = out[:, inputs["input_ids"].shape[1]:]
    parsed = _proc.decode(gen, return_format="parsed")[0]
    return parsed["transcription"] if isinstance(parsed, dict) else str(parsed)


SILENCE = _silence_wav()


def keep_warm():
    """Periodic tiny inference: macOS pages out the 4 GB of GPU-resident
    weights after long idle (server RSS is tiny — weights live in MPS memory),
    which made the first dictation after idle take seconds. A 0.2 s silent
    inference every KEEP_WARM_S keeps everything hot."""
    while True:
        time.sleep(KEEP_WARM_S)
        try:
            t0 = time.time()
            with _lock:
                transcribe_wav(SILENCE)
            print(f"keep-warm ok ({time.time()-t0:.2f}s)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"keep-warm failed: {type(e).__name__}: {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence request spam
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "model": MODEL_ID})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/transcribe":
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        try:
            with _lock:
                text = transcribe_wav(body)
            self._send(200, {"text": text})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    # Compile MPS kernels + touch all weight pages NOW, so the first real
    # dictation after a (re)start is as fast as every other one.
    t0 = time.time()
    with _lock:
        transcribe_wav(SILENCE)
    print(f"warmup inference in {time.time()-t0:.2f}s", flush=True)

    threading.Thread(target=keep_warm, daemon=True).start()
    print(f"keep-warm heartbeat: every {KEEP_WARM_S}s", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
