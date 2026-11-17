# System-wide dictation (dictate-menubar) — findings & gotchas

`⌘⇧D` in **any** app → record → local Musci ASR → text at cursor.
The whole stack lives in `pi-agent/dictate-menubar.py` (rumps menu-bar app,
LaunchAgent `com.roman.dictate-menubar`, Python = `~/.pi/agent/venvs/asr`).

## Delivery matrix (the hard-won part)

| Frontmost app | Method | Why |
|---|---|---|
| Native apps (TextEdit, FSNotes…) | clipboard swap + synthetic ⌘V (System Events keystroke, pynput fallback) | proven |
| **Chromium forks** (Helium `net.imput.helium`, Chrome, Brave, Arc, Vivaldi, Edge) | **type the transcript directly** (System Events `keystroke`) | they silently swallow *every* synthetic paste vector we tried |

Synthetic paste vectors tried and **dead in Helium** (all work in TextEdit):

1. pynput `Controller` ⌘V (session CGEvent post)
2. System Events `keystroke "v" using command down`
3. `CGEventPostToPid` — event posted directly into the browser PID

`keystroke "some text"` (typing, no clipboard) gets through — that's what
TextExpander has always done in browsers. Chromium forks filter synthetic
*paste* (clipboard access is privacy-sensitive) but not synthetic typing.

## ALL-CAPS bug

Two stacked causes, both fixed:

1. **Phantom caps-lock latch**: the HID event-source state had
   `kCGEventFlagMaskAlphaShift` set even though the user's physical caps
   lock was off — manual typing unaffected, but *synthetic* typing
   (System Events `keystroke`, which maps through the layout honoring
   that state) came out ALL CAPS. Diagnosed via
   `CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState)`;
   cleared by toggling keycode 57 synthetically.
2. **Held modifiers**: users hold the stop-press ⌘**⇧**D through the
   banner change; `keystroke` inherits held modifiers, shifting every
   letter. A synthetic `key up` cannot override a physically held key.

Final delivery method (immune to both): **CGEvent unicode injection** —
`CGEventKeyboardSetUnicodeString` carries the text inside each event with
flags=0, chunks of 20 chars; no layout mapping, no modifier state, no
caps state can touch it. System Events `keystroke` kept only as fallback.
Also: `_wait_mods_released` (pynput tracks live ⌘/⇧) protects the
clipboard-paste path.

## macOS 27 permission saga (exact order matters)

- **Input Monitoring** — global hotkey tap (pynput listener)
- **Accessibility** — synthetic keystroke injection
- **Microphone** — prompts on first ⌘⇧D press; allow once
- All three must be granted to the **real binary**, not a symlink:
  `/opt/homebrew/Cellar/python@3.13/<ver>/Frameworks/Python.framework/Versions/3.13/Resources/Python.app`
  The venv `bin/python` is a symlink — the System Settings picker can't
  resolve it. Use ⌘⇧G in the picker and paste the full path. A
  Homebrew python upgrade changes the Cellar path → grant silently detaches
  (symptom: hotkey dead, log shows "This process is not trusted!").
- A process started **before** the grant keeps a dead event tap — restart
  the agent after granting: `launchctl kickstart -k gui/$(id -u)/com.roman.dictate-menubar`

## Feedback channels (macOS gives bare scripts nothing)

- **Floating banner** (red ● REC / orange ⏳): AppKit NSWindow, level 25,
  `CanJoinAllSpaces | FullScreenAuxiliary`, `ignoresMouseEvents` — above
  every app/space/fullscreen. The only always-visible cue.
- **Toasts**: `osascript display notification` — the one channel that
  displays banners on this machine. rumps notifications were **silently
  invisible** even with "Python" enabled in System Settings → Notifications.

## Debug plumbing (all failures were invisible by default)

- every hotkey press logs `hotkey pressed, state=…`
- `audio: sr=… dur=… rms=… peak=…` before each POST (silence detector:
  room tone ≈ rms 0.009; healthy speech ≈ 0.04–0.15 quiet, 0.2+ loud)
- `asr returned N chars: '…'`, `deliver method: …`, `delivered -> <App>`
- rec stderr → `~/.pi/agent/logs/dictate-menubar-rec.err`
- wav → `~/.pi/agent/logs/dictate-last.wav` (stable path; see TMPDIR gotcha)

## Gotchas

- **launchd PATH** has no `/opt/homebrew/bin` — `rec` must be resolved
  absolutely (`shutil.which` at import, fallback hardcoded).
- `tempfile.mktemp` writes to the **per-user TMPDIR**
  (`/var/folders/…/T/`), not `/tmp` — don't glob `/tmp` for recordings.
- sox `rec -r 16000` warns "can't set sample rate 16000; using 48000"
  (device rate) but the **output file is still 16 kHz** — sox resamples.
- musci-server resamples anything non-16k via librosa fallback anyway.
- Raw sox capture has **no AGC** (browsers apply AGC — that's why pi-web
  dictation felt more reliable). +12 dB software gain tested then removed
  per preference; normal speaking volume works (rms ≈ 0.04).
- Killing the agent leaves a child `rec` orphan recording forever —
  `pkill -f "dictate-global"` / new cycles overwrite a stable path now.
- System Events keystroke with `\n` would hit **Return** (submits search
  boxes) — transcripts are flattened to single-line before typing.

## Ops

```bash
# restart after edits
launchctl kickstart -k gui/$(id -u)/com.roman.dictate-menubar
# logs
tail -f ~/.pi/agent/logs/dictate-menubar.log
# deploy from repo
cd speech-to-text-model-tester && ./bin/deploy.sh
```
