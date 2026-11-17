#!/usr/bin/env python3
"""Global dictation hotkey — Musci local ASR, everywhere on macOS.

⌘⇧D (Cmd+Shift+D) toggles recording from ANY app. On stop, the transcript is
pasted at the cursor of the frontmost application (via clipboard swap + ⌘V).

Runs as a menu-bar app (LaunchAgent com.roman.dictate-menubar, KeepAlive).

Permissions (first run):
  - Microphone    -> for `rec` (sox); macOS prompts on first press, allow it
  - Accessibility -> for the global hotkey tap and the synthetic ⌘V paste
                     (grant to "Python" in System Settings → Privacy & Security)
  - Input Monitoring -> for the global hotkey listener, same place

Engine: resident musci-server on 127.0.0.1:31046 (LaunchAgent
com.roman.musci-server). Audio never leaves this machine.
"""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

import rumps
from pynput import keyboard

try:
    from Foundation import NSMakeRect
    from AppKit import (NSWindow, NSColor, NSTextField, NSFont,
                        NSScreen, NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorFullScreenAuxiliary,
                        NSCenterTextAlignment)
    HAS_APPKIT = True
except Exception as _e:
    print(f"AppKit overlay unavailable: {_e}", flush=True)
    HAS_APPKIT = False

MUSCI_URL = "http://127.0.0.1:31046/transcribe"
HOTKEY = "<cmd>+<shift>+d"
REC_BIN = shutil.which("rec") or "/opt/homebrew/bin/rec"  # launchd PATH has no /opt/homebrew/bin
REC_ERR_LOG = os.path.expanduser("~/.pi/agent/logs/dictate-menubar-rec.err")
WAV_PATH = os.path.expanduser("~/.pi/agent/logs/dictate-last.wav")

IDLE, REC, BUSY = "idle", "recording", "busy"

# live physical-modifier state (updated by the hotkey listener) — used to
# wait for ⌘⇧ release before typing, else every letter comes out SHIFTED
MODS = {"cmd": False, "shift": False}


def _update_mods(key, down: bool):
    name = getattr(key, "name", "") or ""
    if name.startswith("cmd"):
        MODS["cmd"] = down
    if name.startswith("shift"):
        MODS["shift"] = down


def _wait_mods_released(timeout: float = 10.0) -> None:
    """Block until the user physically releases ⌘/⇧ (or timeout)."""
    t0 = time.time()
    while any(MODS.values()) and time.time() - t0 < timeout:
        time.sleep(0.05)
    print(f"mods at delivery: {dict(MODS)} (waited {time.time()-t0:.2f}s)", flush=True)


def notify(title, text=""):
    print(f"notify: {title} | {text}", flush=True)
    # osascript channel — the one proven to display banners on this machine
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(str(text)[:200])} '
             f'with title "Dictation" subtitle {json.dumps(str(title)[:80])}'],
            capture_output=True, timeout=5)
    except Exception as e:
        print(f"notify failed: {type(e).__name__}: {e}", flush=True)


# Chromium-family apps that swallow session-level synthetic ⌘V
STUBBORN = ("helium", "chrom", "brave", "vivaldi", "edge", "arc", "orbit")


def _sanitize_for_typing(text: str) -> str:
    """newlines/tabs would hit Enter/Tab — flatten to spaces."""
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _type_text_quartz(text: str) -> bool:
    """Type via CGEvent unicode injection: each event carries its own text
    with flags=0 — immune to caps-lock latches, held modifiers, and keyboard
    layout (the cause of the ALL-CAPS bug). Chunks of 20 chars per event."""
    try:
        from Quartz import (CGEventCreateKeyboardEvent,
                            CGEventKeyboardSetUnicodeString,
                            CGEventPost, kCGHIDEventTap)
        for i in range(0, len(text), 20):
            chunk = text[i:i + 20]
            for down in (True, False):
                ev = CGEventCreateKeyboardEvent(None, 0, down)
                CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
                CGEventPost(kCGHIDEventTap, ev)
            time.sleep(0.02)
        print("deliver method: typing (quartz unicode)", flush=True)
        return True
    except Exception as e:
        print(f"quartz typing failed: {type(e).__name__}: {e}", flush=True)
        return False


def _type_text(text: str) -> bool:
    if _type_text_quartz(_sanitize_for_typing(text)):
        return True
    # fallback: System Events keystroke (layout+modifier sensitive)
    t = _sanitize_for_typing(text).replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to keystroke "{t}"'],
        capture_output=True, timeout=15)
    if r.returncode == 0:
        print("deliver method: typing (System Events)", flush=True)
        return True
    print(f"typing failed: {r.stderr.decode()[:120]}", flush=True)
    return False


def _paste_keystroke_osascript() -> bool:
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        capture_output=True, timeout=5)
    if r.returncode == 0:
        print("deliver method: osascript ⌘V", flush=True)
        return True
    print(f"osascript paste failed ({r.returncode}): {r.stderr.decode()[:120]}", flush=True)
    return False


def _paste_keystroke_pynput() -> None:
    c = keyboard.Controller()
    c.press(keyboard.Key.cmd)
    c.tap("v")
    c.release(keyboard.Key.cmd)
    print("deliver method: pynput ⌘V", flush=True)


def deliver_text(text: str) -> str:
    """Get the transcript into the focused field of the frontmost app.
    Returns the app name. Chromium forks: type directly (they block
    synthetic paste). Everyone else: clipboard swap + ⌘V."""
    _wait_mods_released()  # ⌘⇧ still physically held would SHIFT every letter
    target, bundle_id = "?", ""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        target = app.localizedName()
        bundle_id = app.bundleIdentifier() or ""
    except Exception as e:
        print(f"frontmost probe failed: {type(e).__name__}: {e}", flush=True)

    if any(s in bundle_id.lower() for s in STUBBORN):
        if _type_text(text):
            return target
        # typing failed: leave transcript on the clipboard for a manual ⌘V
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
        notify("press ⌘V to paste", text[:120])
        return target

    old = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    subprocess.run(["pbcopy"], input=text.encode(), check=False)
    time.sleep(0.05)
    try:
        if not _paste_keystroke_osascript():
            _paste_keystroke_pynput()
        time.sleep(0.6)  # let the target app read the clipboard
    finally:
        subprocess.run(["pbcopy"], input=old.encode(), check=False)
    return target



class DictateApp(rumps.App):
    def __init__(self):
        super().__init__("🎙", quit_button=None)
        self.state = IDLE
        self.proc = None
        self.wav = None
        self.cancelled = False
        self.title = "🎙"
        self._overlay = None
        self._ovq = queue.Queue()  # overlay ops from any thread -> main thread

        self.menu = [
            rumps.MenuItem("Start / Stop dictation"),
            rumps.MenuItem("Cancel recording"),
            None,
            rumps.MenuItem("Check server"),
            rumps.MenuItem("Quit"),
        ]

        self.listener = keyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release)
        self.listener.start()
        print("global hotkey registered:", HOTKEY, flush=True)

        # main-thread pump that applies overlay changes
        self._timer = rumps.Timer(self._drain_overlay, 0.10)
        self._timer.start()
        print("overlay pump started, AppKit:", HAS_APPKIT, flush=True)

    # ---------------- visual overlay (main thread only) ----------------

    def _drain_overlay(self, _sender=None):
        try:
            while True:
                op, text, rgb = self._ovq.get_nowait()
                if op == "show":
                    self._overlay_show(text, rgb)
                else:
                    self._overlay_hide()
        except queue.Empty:
            pass

    def _overlay_show(self, text, rgb):
        if not HAS_APPKIT:
            return
        try:
            if self._overlay is None:
                win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                    NSMakeRect(0, 0, 260, 64), 0, 2, False)
                win.setLevel_(25)  # NSStatusWindowLevel — above all normal windows
                win.setOpaque_(False)
                win.setHasShadow_(True)
                win.setIgnoresMouseEvents_(True)
                win.setCollectionBehavior_(
                    NSWindowCollectionBehaviorCanJoinAllSpaces |
                    NSWindowCollectionBehaviorFullScreenAuxiliary)
                label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 64))
                label.setEditable_(False)
                label.setBordered_(False)
                label.setDrawsBackground_(False)
                label.setAlignment_(NSCenterTextAlignment)
                label.setFont_(NSFont.boldSystemFontOfSize_(24))
                label.setTextColor_(NSColor.whiteColor())
                win.contentView().addSubview_(label)
                self._overlay = (win, label)
            win, label = self._overlay
            win.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
                rgb[0], rgb[1], rgb[2], 0.94))
            label.setStringValue_(text)
            frame = NSScreen.mainScreen().frame()
            win.setFrameOrigin_((frame.origin.x + (frame.size.width - 260) / 2,
                                 frame.origin.y + frame.size.height - 140))
            win.orderFrontRegardless()
        except Exception as e:
            print(f"overlay show failed: {type(e).__name__}: {e}", flush=True)

    def _overlay_hide(self):
        if not HAS_APPKIT or self._overlay is None:
            return
        try:
            self._overlay[0].orderOut_(None)
        except Exception as e:
            print(f"overlay hide failed: {type(e).__name__}: {e}", flush=True)

    def _ov(self, op, text="", rgb=(1, 1, 1)):
        self._ovq.put((op, text, rgb))

    # ---------------- core toggle ----------------

    def _on_key_press(self, key):
        _update_mods(key, True)
        ch = getattr(key, "char", None)
        if ch in ("d", "D") and MODS["cmd"] and MODS["shift"]:
            self.toggle()

    def _on_key_release(self, key):
        _update_mods(key, False)

    def toggle(self, _=None):
        print(f"hotkey pressed, state={self.state}", flush=True)
        if self.state == IDLE:
            self.start()
        elif self.state == REC:
            self.stop()

    def start(self):
        if self.state != IDLE:
            return
        self.cancelled = False
        self.wav = WAV_PATH
        try:
            os.remove(self.wav)
        except OSError:
            pass
        try:
            self._rec_err = open(REC_ERR_LOG, "ab")
            self.proc = subprocess.Popen(
                [REC_BIN, "-q", "-r", "16000", "-c", "1", self.wav],
                stdout=subprocess.DEVNULL, stderr=self._rec_err,
            )
        except FileNotFoundError:
            notify("sox missing", REC_BIN)
            return
        self.state = REC
        self.title = "●"
        self._ov("show", "● REC", (1.0, 0.22, 0.19))
        print("recording…", flush=True)

    def stop(self):
        if self.state != REC:
            return
        self.state = BUSY
        self.title = "⏳"
        self._ov("show", "⏳ transcribing…", (1.0, 0.60, 0.0))
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            self.proc = None
        threading.Thread(target=self._finish, daemon=True).start()

    # ---------------- menu callbacks ----------------

    @rumps.clicked("Start / Stop dictation")
    def toggle_menu(self, _):
        self.toggle()

    @rumps.clicked("Cancel recording")
    def cancel(self, _=None):
        if self.state == REC:
            self.cancelled = True
            if self.proc:
                self.proc.terminate()
        self.state = IDLE
        self.title = "🎙"
        self._ov("hide")

    @rumps.clicked("Check server")
    def check_server(self, _=None):
        try:
            with urllib.request.urlopen("http://127.0.0.1:31046/health", timeout=2) as r:
                notify("musci-server", r.read().decode()[:80])
        except Exception as e:
            notify("musci-server down", str(e)[:80])

    @rumps.clicked("Quit")
    def quit(self, _=None):
        self.cancel()
        rumps.quit_application()

    # ---------------- transcribe + paste ----------------

    def _finish(self):
        time.sleep(0.35)  # let sox finalize the wav header
        if self.cancelled:
            self._done()
            return
        try:
            audio = open(self.wav, "rb").read()
        except OSError as e:
            rec_err = ""
            try:
                rec_err = open(REC_ERR_LOG).read()[-120:]
            except OSError:
                pass
            notify("recording failed", (f"{e}"[:60] + " | " + rec_err)[-120:])
            self._done()
            return
        print(f"posting {len(audio)} bytes to musci-server…", flush=True)
        try:
            import numpy as np
            import soundfile as sf
            w, sr = sf.read(self.wav, dtype="float32")
            rms = float(np.sqrt((w.astype(np.float64) ** 2).mean()))
            peak = float(np.abs(w).max())
            print(f"audio: sr={sr} dur={len(w)/sr:.1f}s rms={rms:.5f} peak={peak:.4f}", flush=True)
            if rms < 1e-4:
                notify("recording is silent", "check microphone input device/level")
        except Exception as e:
            print(f"audio probe skipped: {type(e).__name__}: {e}", flush=True)
        try:
            req = urllib.request.Request(MUSCI_URL, data=audio,
                headers={"Content-Type": "audio/wav"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                text = json.loads(r.read().decode()).get("text", "").strip()
        except Exception as e:
            notify("ASR failed", f"{type(e).__name__}: {str(e)[:80]}")
            self._done()
            return
        print(f"asr returned {len(text)} chars: {text[:100]!r}", flush=True)

        if text:
            try:
                target = deliver_text(text)
                print(f"delivered -> {target}", flush=True)
                notify(f"✓ pasted to {target}", text[:120])
            except Exception as e:
                notify("delivery failed", f"{type(e).__name__}: {str(e)[:80]}")
        else:
            notify("no speech detected")
        self._done()

    def _done(self):
        self.state = IDLE
        self.title = "🎙"
        self._ov("hide")
        print("cycle complete", flush=True)


if __name__ == "__main__":
    DictateApp().run()
