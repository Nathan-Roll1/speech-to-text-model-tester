/**
 * dictate-webview — voice dictation for Pi Agent Studio's webview UI.
 *
 * Toggle with Meta+D (wired in the webview) or /dictate.
 * Engines:
 *   musci    (default) local Musci-ASR via resident server on 127.0.0.1:31046
 *            (server: ~/.pi/agent/bin/musci-server.py, spawned on demand,
 *             auto-exits after 15 idle minutes)
 *   deepgram Deepgram Nova-3 streaming (DEEPGRAM_API_KEY from env,
 *            ~/.pi/agent/deepgram.key, or ~/.zshrc)
 * Switch with /dictate-engine <musci|deepgram>; state persists in
 * ~/.pi/agent/dictate-engine.json.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { spawn, type ChildProcessByStdio } from "node:child_process";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Readable } from "node:stream";

type Engine = "musci" | "deepgram";
type State = "idle" | "recording" | "stopping";

const DG_URL =
  "wss://api.deepgram.com/v1/listen" +
  "?model=nova-3" +
  "&encoding=linear16" +
  "&sample_rate=16000" +
  "&channels=1" +
  "&interim_results=false" +
  "&smart_format=true" +
  "&punctuate=true" +
  "&endpointing=300";

const MUSCI_SERVER = "http://127.0.0.1:31046";
const MUSCI_PYTHON = join(homedir(), ".pi", "agent", "venvs", "asr", "bin", "python");
const MUSCI_SERVER_SCRIPT = join(homedir(), ".pi", "agent", "bin", "musci-server.py");
const ENGINE_CFG = join(homedir(), ".pi", "agent", "dictate-engine.json");

function loadEngine(): Engine {
  try {
    if (existsSync(ENGINE_CFG)) {
      const j = JSON.parse(readFileSync(ENGINE_CFG, "utf8"));
      if (j.engine === "musci" || j.engine === "deepgram") return j.engine;
    }
  } catch {}
  return "musci";
}

function saveEngine(e: Engine): void {
  try {
    writeFileSync(ENGINE_CFG, JSON.stringify({ engine: e }) + "\n");
  } catch {}
}

function loadDeepgramKey(): string | undefined {
  const fromEnv = process.env.DEEPGRAM_API_KEY?.trim();
  if (fromEnv) return fromEnv;

  const keyFile = join(homedir(), ".pi", "agent", "deepgram.key");
  if (existsSync(keyFile)) {
    try {
      const v = readFileSync(keyFile, "utf8").trim().split(/\s+/)[0];
      if (v) return v;
    } catch {}
  }

  for (const rc of [".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile"]) {
    const path = join(homedir(), rc);
    if (!existsSync(path)) continue;
    try {
      const txt = readFileSync(path, "utf8");
      const m = txt.match(/^\s*export\s+DEEPGRAM_API_KEY=(.*)$/m);
      if (!m) continue;
      let v = m[1].trim();
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      if (v) return v;
    } catch {}
  }
  return undefined;
}

export default function (pi: ExtensionAPI) {
  let engine: Engine = loadEngine();
  let state: State = "idle";
  let rec: ChildProcessByStdio<null, Readable, Readable> | null = null;
  let ws: WebSocket | null = null;
  let finals: string[] = [];
  let activeCtx: ExtensionContext | null = null;
  let flushed = false;
  let cancelled = false;
  let stopTimeout: NodeJS.Timeout | null = null;
  let generation = 0;
  let lastCtx: ExtensionContext | null = null;
  let wavPath: string | null = null;

  const flush = () => {
    if (flushed || !activeCtx) return;
    flushed = true;
    if (cancelled) return;
    const text = finals.join(" ").replace(/\s+/g, " ").trim();
    if (!text) {
      activeCtx.ui.notify("No speech detected", "warning");
      return;
    }
    const current = activeCtx.ui.getEditorText() ?? "";
    const sep = current && !/\s$/.test(current) ? " " : "";
    activeCtx.ui.setEditorText(current + sep + text);
  };

  const cleanup = () => {
    generation++;
    flush();
    if (stopTimeout) {
      clearTimeout(stopTimeout);
      stopTimeout = null;
    }
    if (rec) {
      try {
        rec.kill("SIGTERM");
      } catch {}
      rec = null;
    }
    if (ws) {
      try {
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      } catch {}
      ws = null;
    }
    finals = [];
    wavPath = null;
    state = "idle";
    if (activeCtx) activeCtx.ui.setStatus("dictate", undefined);
    activeCtx = null;
    flushed = false;
    cancelled = false;
  };

  // ---- local Musci engine -------------------------------------------------

  const waitForServer = async (ctx: ExtensionContext, timeoutS: number): Promise<boolean> => {
    const deadline = Date.now() + timeoutS * 1000;
    while (Date.now() < deadline) {
      try {
        const r = await fetch(`${MUSCI_SERVER}/health`, { signal: AbortSignal.timeout(800) });
        if (r.ok) return true;
      } catch {}
      await new Promise((res) => setTimeout(res, 700));
    }
    ctx.ui.notify("Local ASR server did not come up", "error");
    return false;
  };

  const ensureMusciServer = async (ctx: ExtensionContext): Promise<boolean> => {
    try {
      const r = await fetch(`${MUSCI_SERVER}/health`, { signal: AbortSignal.timeout(800) });
      if (r.ok) return true;
    } catch {}
    if (!existsSync(MUSCI_PYTHON) || !existsSync(MUSCI_SERVER_SCRIPT)) {
      ctx.ui.notify(
        `Musci server missing (${MUSCI_SERVER_SCRIPT} / ${MUSCI_PYTHON}) — /dictate-engine deepgram as fallback`,
        "error",
      );
      return false;
    }
    const child = spawn(MUSCI_PYTHON, [MUSCI_SERVER_SCRIPT], {
      detached: true,
      stdio: "ignore",
    });
    child.unref();
    ctx.ui.setStatus("dictate", "starting local ASR…");
    return waitForServer(ctx, 60);
  };

  const startMusci = async (ctx: ExtensionContext, myGeneration: number) => {
    if (!(await ensureMusciServer(ctx))) {
      cleanup();
      return;
    }
    if (myGeneration !== generation) return;

    activeCtx = ctx;
    finals = [];
    flushed = false;
    cancelled = false;
    state = "recording";
    wavPath = `/tmp/dictate-${Date.now()}.wav`;
    ctx.ui.setStatus("dictate", "● listening (local Musci)…");
    ctx.ui.notify("● listening (local)…  (Meta+D to stop)", "info");

    let proc: ChildProcessByStdio<null, Readable, Readable>;
    try {
      proc = spawn("rec", ["-q", "-r", "16000", "-c", "1", wavPath!], {
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch {
      ctx.ui.notify("Failed to spawn 'rec'. Install sox: brew install sox", "error");
      cleanup();
      return;
    }
    rec = proc;

    proc.on("error", (err) => {
      if (myGeneration !== generation) return;
      ctx.ui.notify(`rec error: ${err.message} (install sox: brew install sox)`, "error");
      cleanup();
    });
  };

  const finishMusci = async (myGeneration: number) => {
    if (myGeneration !== generation || !activeCtx) return;
    activeCtx.ui.setStatus("dictate", "transcribing (local)…");

    // give sox a moment to finalize the wav header after SIGTERM
    await new Promise((res) => setTimeout(res, 350));

    if (!wavPath || !existsSync(wavPath)) {
      activeCtx.ui.notify("Recording file missing", "warning");
      cleanup();
      return;
    }

    try {
      const audio = readFileSync(wavPath);
      const res = await fetch(`${MUSCI_SERVER}/transcribe`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: audio,
        signal: AbortSignal.timeout(120_000),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        activeCtx.ui.notify(`Local ASR failed (HTTP ${res.status}) ${detail.slice(0, 120)}`, "error");
        cleanup();
        return;
      }
      const data = (await res.json()) as { text?: string };
      if (data.text) finals.push(data.text);
    } catch (e: any) {
      activeCtx.ui.notify(`Local ASR error: ${e?.message || e}`, "error");
      cleanup();
      return;
    }

    try {
      if (wavPath) unlinkSync(wavPath);
    } catch {}
    cleanup();
  };

  // ---- deepgram engine ----------------------------------------------------

  const startDeepgram = (ctx: ExtensionContext, myGeneration: number) => {
    const apiKey = loadDeepgramKey();
    if (!apiKey) {
      ctx.ui.notify("DEEPGRAM_API_KEY not set (env, ~/.pi/agent/deepgram.key, or shell rc)", "error");
      return;
    }

    activeCtx = ctx;
    finals = [];
    flushed = false;
    cancelled = false;
    state = "recording";
    ctx.ui.setStatus("dictate", "● listening (Deepgram)…");
    ctx.ui.notify("● listening (Deepgram)…  (Meta+D to stop)", "info");

    let proc: ChildProcessByStdio<null, Readable, Readable>;
    try {
      proc = spawn(
        "rec",
        [
          "-q",
          "--buffer",
          "512",
          "-r",
          "16000",
          "-c",
          "1",
          "-b",
          "16",
          "-e",
          "signed-integer",
          "-t",
          "raw",
          "-",
        ],
        { stdio: ["ignore", "pipe", "pipe"] },
      );
    } catch {
      ctx.ui.notify("Failed to spawn 'rec'. Install sox: brew install sox", "error");
      cleanup();
      return;
    }
    rec = proc;

    proc.on("error", (err) => {
      if (myGeneration !== generation) return;
      ctx.ui.notify(`rec error: ${err.message} (install sox: brew install sox)`, "error");
      cleanup();
    });

    proc.on("exit", (code) => {
      if (myGeneration !== generation) return;
      if (state === "recording" && code !== null && code !== 0) {
        if (activeCtx) activeCtx.ui.notify(`rec exited unexpectedly (code ${code})`, "warning");
        cleanup();
      }
    });

    try {
      ws = new WebSocket(DG_URL, ["token", apiKey]);
    } catch (e: any) {
      ctx.ui.notify(`Deepgram WS failed: ${e.message}`, "error");
      cleanup();
      return;
    }

    ws.addEventListener("open", () => {
      if (myGeneration !== generation) return;
      if (!rec || !ws) return;
      rec.stdout.on("data", (chunk: Buffer) => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(chunk);
      });
    });

    ws.addEventListener("message", (ev) => {
      if (myGeneration !== generation) return;
      try {
        const msg = JSON.parse(ev.data as string);
        if (msg.type === "Results" && msg.is_final) {
          const t = msg.channel?.alternatives?.[0]?.transcript;
          if (t) finals.push(t);
        }
      } catch {}
    });

    ws.addEventListener("error", () => {
      if (myGeneration !== generation) return;
      if (activeCtx) activeCtx.ui.notify("Deepgram WebSocket error", "error");
      cleanup();
    });

    ws.addEventListener("close", () => {
      if (myGeneration !== generation) return;
      if (state === "recording" || state === "stopping") cleanup();
    });
  };

  // ---- shared controls ----------------------------------------------------

  const startDictation = (ctx: ExtensionContext) => {
    if (process.platform !== "darwin") {
      ctx.ui.notify(
        "This host has no microphone — in the pi-web page press Option+M (browser-side dictation)",
        "warning",
      );
      return;
    }
    const myGeneration = ++generation;
    if (engine === "musci") {
      void startMusci(ctx, myGeneration);
    } else {
      startDeepgram(ctx, myGeneration);
    }
  };

  const stopDictation = () => {
    if (state !== "recording") return;
    state = "stopping";
    if (activeCtx) activeCtx.ui.setStatus("dictate", "finalizing…");

    if (engine === "musci") {
      const myGeneration = generation;
      if (rec) {
        try {
          rec.kill("SIGTERM");
        } catch {}
      }
      void finishMusci(myGeneration);
      return;
    }

    if (rec) {
      try {
        rec.kill("SIGTERM");
      } catch {}
    }

    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: "CloseStream" }));
      } catch {
        cleanup();
        return;
      }
      stopTimeout = setTimeout(() => {
        if (state === "stopping") cleanup();
      }, 3000);
    } else {
      cleanup();
    }
  };

  const cancelDictation = () => {
    if (state !== "recording" && state !== "stopping") return;
    cancelled = true;
    finals = [];
    cleanup();
  };

  const toggleDictation = (ctx: ExtensionContext) => {
    lastCtx = ctx;
    if (state === "idle") startDictation(ctx);
    else if (state === "recording") stopDictation();
  };

  pi.on("session_start", (_event, ctx) => {
    lastCtx = ctx;
    engine = loadEngine();
  });

  pi.registerCommand("dictate", {
    description: `Toggle voice dictation (${engine}, Meta+D in the webview)`,
    handler: async (_args, ctx) => {
      toggleDictation(ctx);
    },
  });

  pi.registerCommand("dictate-engine", {
    description: "Show/set dictation engine: musci (local, default) | deepgram",
    handler: async (args: string | undefined, ctx) => {
      const parts = (args || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
      const cmd = parts[0] || "";
      if (cmd === "musci" || cmd === "deepgram") {
        engine = cmd as Engine;
        saveEngine(engine);
        ctx.ui.notify(`dictate engine: ${engine}`, "info");
        return;
      }
      ctx.ui.notify(
        `dictate engine: ${engine}  (usage: /dictate-engine musci|deepgram)`,
        "info",
      );
    },
  });

  pi.registerCommand("dictate-cancel", {
    description: "Cancel in-flight voice dictation",
    handler: async () => {
      cancelDictation();
    },
  });

  pi.on("session_shutdown", () => {
    if (state !== "idle") cleanup();
  });
}
