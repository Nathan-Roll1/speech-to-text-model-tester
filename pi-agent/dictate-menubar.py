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
import subprocess
import tempfile
import threading
import time
import urllib.request

import rumps
from pynput import keyboard

MUSCI_URL = "http://127.0.0.1:31046/transcribe"
HOTKEY = "<cmd>+<shift>+d"

IDLE, REC, BUSY = "idle", "recording", "busy"


def notify(title, text=""):
    try:
        rumps.notification("Dictation", title, text)
    except Exception:
        pass


def paste_at_cursor(text: str) -> None:
    """Clipboard swap + synthetic ⌘V. Requires Accessibility for Python."""
    old = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    subprocess.run(["pbcopy"], input=text.encode(), check=False)
    time.sleep(0.05)
    try:
        c = keyboard.Controller()
        c.press(keyboard.Key.cmd)
        c.tap("v")
        c.release(keyboard.Key.cmd)
        time.sleep(0.6)  # let the target app read the clipboard
    finally:
        subprocess.run(["pbcopy"], input=old.encode(), check=False)


class DictateApp(rumps.App):
    def __init__(self):
        super().__init__("🎙", quit_button=None)
        self.state = IDLE
        self.proc = None
        self.wav = None
        self.cancelled = False
        self.title = "🎙"

        self.menu = [
            rumps.MenuItem("Start / Stop dictation"),
            rumps.MenuItem("Cancel recording"),
            None,
            rumps.MenuItem("Check server"),
            rumps.MenuItem("Quit"),
        ]

        self.listener = keyboard.GlobalHotKeys({HOTKEY: self.toggle})
        self.listener.start()
        print("global hotkey registered:", HOTKEY, flush=True)

    # ---------------- core toggle ----------------

    def toggle(self, _=None):
        if self.state == IDLE:
            self.start()
        elif self.state == REC:
            self.stop()

    def start(self):
        if self.state != IDLE:
            return
        self.cancelled = False
        self.wav = tempfile.mktemp(prefix="dictate-global-", suffix=".wav")
        try:
            self.proc = subprocess.Popen(
                ["rec", "-q", "-r", "16000", "-c", "1", self.wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            notify("sox missing", "brew install sox")
            return
        self.state = REC
        self.title = "●"
        print("recording…", flush=True)

    def stop(self):
        if self.state != REC:
            return
        self.state = BUSY
        self.title = "⏳"
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
            notify("recording file missing", str(e)[:80])
            self._done()
            return
        try:
            req = urllib.request.Request(MUSCI_URL, data=audio,
                headers={"Content-Type": "audio/wav"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                text = json.loads(r.read().decode()).get("text", "").strip()
        except Exception as e:
            notify("ASR failed", f"{type(e).__name__}: {str(e)[:80]}")
            self._done()
            return

        if text:
            paste_at_cursor(text)
        else:
            notify("no speech detected")
        self._done()

    def _done(self):
        self.state = IDLE
        self.title = "🎙"
        print("cycle complete", flush=True)


if __name__ == "__main__":
    DictateApp().run()
