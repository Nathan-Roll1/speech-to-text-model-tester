/**
 * Pi Web dictation + speak shortcuts. Loaded into the pi-web page.
 * Client-side dictation: the browser captures the mic and streams 16 kHz
 * PCM16 straight to Deepgram; finals go into the page editor. This is what
 * makes dictation work when pi runs on a headless server (no `rec` there).
 *
 * Dictate: Option+M / Cmd+D / Ctrl+Shift+D / Cmd+Shift+D
 * Speak last reply: Option+S (server-side /speak prompt, unchanged)
 * Key source: /pi-web-dictate-key.txt (deployed next to this file by the
 * overlay; reachable only inside the tailnet, same as pi-web itself).
 */
(() => {
  if (window.__piWebDictate) return;
  window.__piWebDictate = true;

  // ---------------- state ----------------
  let listening = false;
  let stopping = false;
  let speakBusy = false;
  let mediaStream = null;
  let audioCtx = null;
  let workletNode = null;
  let sourceNode = null;
  let ws = null;
  let finals = [];
  let badgeEl = null;
  let stopTimer = null;
  let starting = false;
  let pendingAudio = [];
  let localMode = false;   // true = local Musci on this machine
  let pcmBuffer = [];      // Int16Array chunks captured for local mode
  let localOK = false;     // health probe result, cached per page load
  let localProbed = false;

  const MUSCI_URL = "http://127.0.0.1:31046";

  const DG_WS =
    "wss://api.deepgram.com/v1/listen" +
    "?model=nova-3" +
    "&encoding=linear16" +
    "&sample_rate=16000" +
    "&channels=1" +
    "&interim_results=false" +
    "&smart_format=true" +
    "&punctuate=true" +
    "&endpointing=300";

  // ---------------- ui ----------------
  const showBadge = (text) => {
    if (!badgeEl) {
      badgeEl = document.createElement("div");
      badgeEl.style.cssText =
        "position:fixed;right:16px;bottom:16px;z-index:99999;" +
        "background:#111827;color:#f9fafb;border:1px solid #374151;border-radius:8px;" +
        "padding:6px 12px;font:12px/1.4 system-ui,sans-serif;box-shadow:0 4px 12px rgba(0,0,0,.35)";
      document.body.appendChild(badgeEl);
    }
    badgeEl.textContent = text;
    badgeEl.style.display = "block";
  };
  const hideBadge = () => {
    if (badgeEl) badgeEl.style.display = "none";
  };

  // ---------------- editor insertion ----------------
  const pickEditor = () => {
    const list = Array.from(document.querySelectorAll("textarea"));
    let best = null;
    for (const ta of list) {
      const r = ta.getBoundingClientRect();
      if (r.width < 40 || r.height < 16) continue;
      if (!best || r.height > best.getBoundingClientRect().height) best = ta;
    }
    return best;
  };

  const insertIntoEditor = (text) => {
    const ta = pickEditor();
    if (!ta) {
      console.warn("[pi-web-dictate] no editor textarea found");
      return false;
    }
    const current = ta.value ?? "";
    const sep = current && !/\s$/.test(current) ? " " : "";
    const next = current + sep + text;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    if (setter) setter.call(ta, next);
    else ta.value = next;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    ta.focus();
    try {
      ta.setSelectionRange(next.length, next.length);
    } catch {}
    return true;
  };

  // ---------------- teardown ----------------
  const teardownAudio = () => {
    try {
      workletNode && workletNode.disconnect();
    } catch {}
    try {
      sourceNode && sourceNode.disconnect();
    } catch {}
    try {
      mediaStream && mediaStream.getTracks().forEach((t) => t.stop());
    } catch {}
    try {
      audioCtx && audioCtx.close();
    } catch {}
    workletNode = sourceNode = mediaStream = audioCtx = null;
  };

  const finish = () => {
    clearTimeout(stopTimer);
    stopTimer = null;
    teardownAudio();
    if (ws) {
      try {
        ws.close();
      } catch {}
      ws = null;
    }
    const text = finals.join(" ").replace(/\s+/g, " ").trim();
    listening = false;
    stopping = false;
    if (text) {
      insertIntoEditor(text);
      hideBadge();
    } else {
      showBadge("no speech detected");
      setTimeout(hideBadge, 2000);
    }
    finals = [];
    pendingAudio = [];
  };

  // ---------------- dictation ----------------
  // Startup used to be strictly serial (fetch key -> getUserMedia -> worklet
  // -> open Deepgram socket), so the badge only appeared after the SUM of all
  // round trips — the Deepgram TCP+TLS handshake alone measures 0.5-0.9s.
  // Now mic setup, key load and socket handshake run in parallel and the key
  // is cached, so startup costs roughly max(mic spin-up, handshake) and the
  // key fetch is free after the first press.
  let cachedKey = null;
  const loadKey = async () => {
    if (cachedKey) return cachedKey;
    const res = await fetch("/pi-web-dictate-key.txt", { credentials: "same-origin" });
    if (!res.ok) throw new Error("dictate key missing (/pi-web-dictate-key.txt)");
    const key = (await res.text()).trim();
    if (!key) throw new Error("dictate key missing (/pi-web-dictate-key.txt)");
    cachedKey = key;
    return key;
  };
  // Warm the key (and the browser's DNS/TLS path to api.deepgram.com) right
  // after page load, before the first keypress.
  loadKey().catch(() => {});

  const setupMic = async () => {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      throw new Error("browser has no mic capture (AudioWorklet)");
    }

    let recCtx;
    try {
      recCtx = new AudioContext({ sampleRate: 16000 });
    } catch {
      recCtx = new AudioContext();
    }
    audioCtx = recCtx;
    // The keydown that starts dictation is a user gesture, so a resume()
    // request here is honoured immediately instead of leaving us muted.
    if (recCtx.state === "suspended") {
      try {
        await recCtx.resume();
      } catch {}
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    mediaStream = stream;

    const workletCode = `
      class PCMCapture extends AudioWorkletProcessor {
        constructor() {
          super();
          this.frac = 0;
          this.buf = new Int16Array(2048);
          this.n = 0;
        }
        process(inputs) {
          const ch = inputs[0] && inputs[0][0];
          if (!ch || !ch.length) return true;
          const ratio = sampleRate / 16000;
          for (let i = 0; i < ch.length; i++) {
            this.frac += ratio;
            while (this.frac >= 1) {
              this.frac -= 1;
              const s = Math.max(-1, Math.min(1, ch[i]));
              this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
              if (this.n === this.buf.length) {
                this.port.postMessage(this.buf.slice(0));
                this.n = 0;
              }
            }
          }
          return true;
        }
      }
      registerProcessor("pcm-capture", PCMCapture);
    `;
    const blobUrl = URL.createObjectURL(new Blob([workletCode], { type: "application/javascript" }));
    await recCtx.audioWorklet.addModule(blobUrl);
    URL.revokeObjectURL(blobUrl);

    sourceNode = recCtx.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(recCtx, "pcm-capture");
    workletNode.port.onmessage = (ev) => {
      if (localMode) {
        pcmBuffer.push(new Int16Array(ev.data));
        if (pcmBuffer.length > 4000) pcmBuffer.shift(); // cap ~3.5 min
        return;
      }
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(ev.data);
      } else if (ws) {
        // Socket still handshaking: buffer so the first words survive.
        pendingAudio.push(ev.data);
        if (pendingAudio.length > 150) pendingAudio.shift(); // cap ~5s
      }
    };
    sourceNode.connect(workletNode);
    // Do NOT connect worklet to destination — would echo mic to speakers.
  };

  const openDeepgram = (key) =>
    new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new Error("ws open timeout"));
      }, 8000);
      const sock = new WebSocket(DG_WS, ["token", key]);
      ws = sock; // assign early so stopListening/teardown can reach it
      sock.binaryType = "arraybuffer";

      sock.addEventListener("message", (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "Results" && msg.is_final) {
            const t = msg.channel?.alternatives?.[0]?.transcript;
            if (t) finals.push(t);
          }
        } catch {}
      });
      sock.addEventListener("close", () => {
        if (stopping) finish();
      });
      sock.addEventListener("error", () => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error("Deepgram WebSocket error"));
          return;
        }
        if (!stopping) {
          showBadge("Deepgram WebSocket error");
          setTimeout(hideBadge, 3000);
          finish();
        }
      });
      sock.addEventListener("open", () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        // Flush audio captured while the handshake was in flight.
        for (const chunk of pendingAudio) {
          try {
            sock.send(chunk);
          } catch {}
        }
        pendingAudio = [];
        resolve(sock);
      });
    });

  const probeLocal = async () => {
    if (localProbed) return localOK;
    localProbed = true;
    try {
      const r = await fetch(`${MUSCI_URL}/health`, { signal: AbortSignal.timeout(700) });
      localOK = r.ok;
    } catch {
      localOK = false;
    }
    return localOK;
  };
  // Warm the probe right after page load so the first press has the answer.
  probeLocal().catch(() => {});

  const encodeWav = (chunks, sampleRate) => {
    let samples = 0;
    for (const c of chunks) samples += c.length;
    const buf = new ArrayBuffer(44 + samples * 2);
    const v = new DataView(buf);
    const w = (off, s) => {
      for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
    };
    w(0, "RIFF");
    v.setUint32(4, 36 + samples * 2, true);
    w(8, "WAVE");
    w(12, "fmt ");
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);        // PCM
    v.setUint16(22, 1, true);        // mono
    v.setUint32(24, sampleRate, true);
    v.setUint32(28, sampleRate * 2, true);
    v.setUint16(32, 2, true);
    v.setUint16(34, 16, true);
    w(36, "data");
    v.setUint32(40, samples * 2, true);
    let off = 44;
    for (const c of chunks) {
      for (let i = 0; i < c.length; i++) {
        v.setInt16(off, c[i], true);
        off += 2;
      }
    }
    return new Blob([buf], { type: "audio/wav" });
  };

  const finishLocal = async () => {
    teardownAudio();
    const chunks = pcmBuffer;
    pcmBuffer = [];
    const total = chunks.reduce((n, c) => n + c.length, 0);
    if (!total) {
      showBadge("no speech detected");
      setTimeout(hideBadge, 2000);
      listening = false;
      stopping = false;
      return;
    }
    showBadge("transcribing (local)\u2026");
    try {
      const res = await fetch(`${MUSCI_URL}/transcribe`, {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: encodeWav(chunks, 16000),
        signal: AbortSignal.timeout(60000),
      });
      if (!res.ok) throw new Error(`local ASR HTTP ${res.status}`);
      const data = await res.json();
      const text = (data.text || "").replace(/\s+/g, " ").trim();
      if (text) {
        insertIntoEditor(text);
        hideBadge();
      } else {
        showBadge("no speech detected");
        setTimeout(hideBadge, 2000);
      }
    } catch (e) {
      showBadge(`local ASR failed: ${e && e.message ? e.message : e}`);
      setTimeout(hideBadge, 3500);
    }
    listening = false;
    stopping = false;
  };

  const startListening = async () => {
    starting = true;
    showBadge("starting mic\u2026");
    try {
      localMode = await probeLocal();
      if (localMode) {
        await setupMic();
        listening = true;
        showBadge("\u25CF listening (local Musci)\u2026  (Option+M to stop)");
      } else {
        // Mic and Deepgram socket in parallel; only the socket needs the key.
        await Promise.all([setupMic(), loadKey().then(openDeepgram)]);
        listening = true;
        showBadge("\u25CF listening (Deepgram)\u2026  (Option+M to stop)");
      }
    } finally {
      starting = false;
    }
  };

  const stopListening = () => {
    stopping = true;
    showBadge("finalizing\u2026");
    if (localMode) {
      finishLocal();
      return;
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: "CloseStream" }));
      } catch {}
      stopTimer = setTimeout(finish, 3000);
    } else {
      finish();
    }
  };

  const toggle = async () => {
    // `starting` guards the startup window: without it a second press during
    // startup used to run startListening() again (double mic + double socket).
    if (starting || stopping) return;
    if (listening) {
      stopListening();
      return;
    }
    try {
      await startListening();
    } catch (e) {
      console.warn("[pi-web-dictate]", e);
      showBadge(`dictate failed: ${e && e.message ? e.message : e}`);
      setTimeout(hideBadge, 3000);
      finish();
    }
  };

  // ---------------- speak (server-side, unchanged) ----------------
  const sessionId = () => {
    const q = new URLSearchParams(location.search).get("session");
    if (q) return q;
    const m = location.pathname.match(/session[=/]([0-9A-Za-z-]+)/);
    return m ? m[1] : null;
  };

  const speakNow = async () => {
    if (speakBusy) return;
    const id = sessionId();
    if (!id) {
      console.warn("[pi-web-speak] no session id in the URL");
      return;
    }
    speakBusy = true;
    try {
      await fetch(`/api/agent/${encodeURIComponent(id)}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "prompt", message: "/speak now" }),
      });
    } catch (err) {
      console.warn("[pi-web-speak]", err);
    } finally {
      speakBusy = false;
    }
  };

  // ---------------- keys ----------------
  window.addEventListener(
    "keydown",
    (e) => {
      if (e.repeat) return;
      const k = (e.key || "").toLowerCase();
      const isD = e.code === "KeyD" || k === "d";
      const altM =
        e.altKey && !e.metaKey && !e.ctrlKey && (e.code === "KeyM" || k === "m" || k === "\u00b5");
      const metaD = e.metaKey && e.altKey === false && !e.ctrlKey && !e.shiftKey && isD;
      const ctrlShiftD = (e.ctrlKey || e.metaKey) && e.shiftKey && isD;
      const altS =
        e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey && (e.code === "KeyS" || k === "s" || k === "\u00df");
      if (altS) {
        e.preventDefault();
        e.stopPropagation();
        speakNow();
        return;
      }
      if (!altM && !metaD && !ctrlShiftD) return;
      e.preventDefault();
      e.stopPropagation();
      toggle();
    },
    true,
  );
})();
