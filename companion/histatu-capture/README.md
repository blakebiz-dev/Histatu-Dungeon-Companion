# Histatu Runner

In-game dungeon overlay for the [Dungeon Loot Map](https://blakebiz-dungeon-companion.vercel.app):
log chests, record and publish loot routes, and race them against the shared leaderboard.

It never touches the game — no mods, no injection, no packets (Hytale doesn't allow client
mods). Everything is read off **your own screen** with OCR: the F7 debug overlay shows your
Position and Orientation plus the block you're aiming at, and that's all the app needs.

## Install

### Windows — download the app (easiest, no Python)

1. Grab **`HistatuRunner.exe`** from the
   [latest release](https://github.com/blakebiz-dev/Histatu-Dungeon-Companion/releases/latest).
2. Put it in its own folder (it writes `capture_config.json` next to itself) and
   double-click it.
3. **First launch shows "Windows protected your PC."** That's Windows SmartScreen
   warning about an app it hasn't seen many times yet — the app is unsigned (a
   code-signing certificate costs money), not malicious. Click **More info** →
   **Run anyway**. You only do this once per download.

> Your antivirus may also scan it on first run — that's normal for a brand-new
> unsigned `.exe` and clears once it's seen by enough people. The source is right
> here in this repo if you'd rather build it yourself (see below).

### Windows — run from source (for tinkering)

Install Python 3.10+ ([python.org](https://www.python.org/downloads/), tick "Add to
PATH"), then double-click `run.bat` (installs `pillow` + `winsdk` on first run).

### Linux — X11

`pip install pillow pytesseract pynput`, install the `tesseract-ocr` system package
(and optionally `xdotool` for window detection), then `./run.sh`.

### Linux — Wayland (Arch / Hyprland / Sway / wlroots)

The X11 tools don't work under Wayland, so the app uses different backends there —
`run.sh` detects Wayland and installs/points at what's needed:

- **Capture:** `grim` — `sudo pacman -S grim`
- **Key detection:** `python-evdev` (`pip install evdev`), reading `/dev/input`, which needs
  you in the **`input`** group: `sudo usermod -aG input "$USER"` then log out and back in.
- **OCR:** `sudo pacman -S tesseract tesseract-data-eng`
- **Docking** to the game window uses `hyprctl` (Hyprland) or `swaymsg` (Sway); optional.

Then `./run.sh`. The overlay itself runs through XWayland. If your compositor won't let the
overlay place itself, drag it into a corner (Hyprland users can add a `windowrulev2` for the
`Histatu Runner` title to float/position it). No prebuilt binary is provided for Linux — run
from source.

## Setup

**Once per device — 🩺 Capture Doctor**: on first launch the app offers a ~1-minute wizard that
tunes the screen reader to *your* PC: it finds the game window, measures the sharpest OCR zoom for
your resolution, health-checks each F7 signal, and measures your walk pace (sharper
open-tracking). All measurements stay local in `capture_config.json`. Changed monitor, resolution, or GUI
scale later? **⚙ Settings → 🩺 Re-run setup**.

**Once per community**: the web map needs 🎯 Calibrate run one time (two known spots +
their in-game X/Z), so world coordinates can be placed on the map.

**In game**: press **F7** until the WORLD debug panel (top-right) is visible. Enter your
**IGN** in the app — that's what chest opens are tracked under.

## Editor key vs. submit-only

If the shared map has a write key configured, the app has two levels:

- **No key (default):** you can race routes, track your own cooldowns
  and leaderboard times, and **submit chest locations for review** — pressing your log key on
  an unmapped chest sends it as a *pending request* rather than editing the map.
- **Editor key** (paste it in the **Editor key** box): unlocks logging chests directly,
  recording/publishing routes, and **🔎 Verify** mode — check pending requests on the web
  map, then open a chest at a proposed spot to confirm it into a real marker. The button
  shows how many requests are waiting.

If no write key is set on the server, everything is unlocked for everyone (open mode).

**Contributor credit:** every chest that makes it onto the map (or a confirmed missing-chest
report) credits the player who found it — direct editor logs credit yourself, and confirming a
pending request credits its **submitter**. The web map's **🏆 Contributors** view shows the
leaderboard, so top scouts get the thanks they deserve.

## Window: collapse & dock

The overlay is meant to sit out of your way. The title bar has:

- **▾ / ▸** — collapse to just the title bar + status line (tiny, for when you're outside a
  dungeon), or expand back to the full controls.
- **⇱ Dock** — snap it into a corner of the game window. On Windows the overlay is hidden
  from screen captures, so it docks **top-right, right over the F7 coordinates** and the tool
  still reads the game underneath it — zero extra screen space. (On Linux it docks
  bottom-right instead, clear of the panel it reads, since capture-exclusion isn't available
  there.)
- **⚙ Settings** — edit your hotkeys (chest / undo), the OCR poll rate, the daily
  reset hour, and the game window title from a dialog; changes apply immediately and are saved.

Drag the title bar to move it anywhere; its position, and whether it's collapsed, are
remembered between launches.

The packaged **.exe checks for a newer release on launch** and updates itself in place —
**⬆ Update now** downloads the new version, swaps it in, and restarts the app (settings
untouched). You can skip a version or turn it off with `check_updates: false`.

**The game HUD is a second pair of eyes.** The app reads the game's own chest panel — the
movable one listing every area (`Solmara 58/327`, `> The Hollow 44/99`) with the `reset 8h 33m`
countdown. It finds the panel wherever you've placed it. When exactly one area's counter ticks
up but the F7-panel read failed, the open is still logged from where you were aiming
(spatially verified) — with the **area** attributed from the row that moved. The daily reset
time is synced from the game itself, and editors' apps quietly publish each area's true chest
total so the web map can show per-area completion and **how many chests are still
undiscovered** (editors: draw the area boundaries once with ▱ Draw area on the map).

## Modes

- **⏺ Log chests** — open chests normally (**F**); each new chest is added to the shared
  map at its exact block coordinates. High beep = logged, double beep = already mapped
  (the open still counts for your cooldown). Doors/NPCs are ignored silently.
  - **Chest screen covering your coordinates?** At some resolutions the open-chest inventory
    hides the F7 panel, so the app can't read the position at the moment you open. Two safety
    nets handle this — both cross-platform:
    - **Automatic** — the app continuously remembers the last chest you were *aiming at*, so if
      the on-open read comes back blank (panel covered) it logs from that last reading instead.
    - **Manual** — set a **Log-by-aim key** in ⚙ Settings (any spare key): *look at* a chest and
      tap it to log **without opening it**, so the panel is never covered. Logs exactly like a
      normal open (counts for cooldown, feeds travel times).
- **🧭 Record route** — you're asked to **name the route** as you start (or leave it blank
  and name it at the end); then every chest you open becomes the next stop, with the time
  between opens saved as leg times. Press **✔ Finish route** and it's published to the web
  map for everyone (author = your IGN).
- **▶ Run** — study a published route on the web map, then race it: the app is a
  **stopwatch**, counting your opens against the route's stops (out-of-order opens count —
  each open marks only the stop you actually opened). It never points anywhere; knowing
  the route is the skill. When the last stop opens you get your total time, and if it
  beats your previous best it's logged as a **🏁 new record** on the shared leaderboard
  (the web map's 🏁 Runs view). It auto-pauses in menus, on lost focus, or when you stand
  still >10 s, so breaks never pad your time.

**Travel-time learning is always on.** In *any* mode — logging, recording, running, or
verifying — the time between each chest you open and the next one is captured and fed
into the shared leg-time table, **regardless of order**. Open chests A → C → B during a run
and it records the real A→C and C→B times; log two chests a few seconds apart and that short
hop is recorded too. Only clear breaks (gaps longer than `travel_max_sec`, default 5 min —
AFK, long fights) are skipped, and the **lowest** time ever seen for a pair wins, so a
one-off slow leg is corrected the next time anyone walks it cleanly. The status line shows the
seconds since your last open, and the stats line shows how many **legs timed** this session.

The app also shows a persistent **chests available: X/Y** counter (computed from your shared
open log, so it survives restarts) plus **session opened** and **legs timed** counts.

Chest opens are stored per-IGN in the shared map (timestamp + which route you were
running), so cooldowns follow you across devices — and on the web map anyone can type an
IGN into the 👤 box to see that player's cooling chests dimmed with a countdown.

## How opens keep logging when the chest screen covers the panel

Polling the screen constantly would be heavy, so the app OCRs your Position periodically
and **coasts the estimate in between**: WASD keys advance the estimated position along the
last panel-read heading. Every fix applies a latency-compensated correction (movement
during the OCR itself isn't wiped out), re-fits the walk speed, and adapts the poll rate to
recent error. The estimate exists for exactly one job: when the open-chest screen covers
the F7 panel at the moment you press F, the app can still attribute the open to the chest
you're **standing at and facing** — never a guessed one.

## Configuration (`capture_config.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `ign` | — | Your in-game name (also editable in the app) |
| `hotkey_chest` | `F` | Chest/open key (letters, digits, `F1`–`F12`, mouse buttons, `Delete`/`End`/…) |
| `hotkey_log` | — | Optional app-only key: log the chest you're **aiming at** without opening it (for when the chest screen covers the coordinates) |
| `hotkey_undo` | `F10` | Remove the last chest you logged |
| `reset_hour_et` | `20` | Daily chest reset hour, US Eastern wall clock (DST-aware) — every chest you opened relocks until this hour comes around; `20` = 8 PM ET |
| `ocr_poll_sec` | `1.8` | Position re-sync cadence in active modes |
| `move_speed` | auto | Walk-speed constant for the position estimate, self-calibrating |
| `only_when_game_focused` | `true` | Ignore keys when Hytale isn't the active window |
| `dry_run` | `false` | Test everything without posting to the shared map |

⚠ Switch to Idle before typing in chat — the app can't tell chat typing from the F key.

## Troubleshooting

- `run.bat --test` (or `python3 histatu_runner.py --test`) captures one frame, saves
  `capture_debug.png`, and prints the parsed position/yaw/target plus the aim angle.
- "Can't read position": keep the F7 WORLD panel visible and uncovered; the app re-finds
  it automatically after a couple of polls.
- The shared map allows ~30 writes/minute per person; a chest open costs one write (two
  when it also logs a new chest).
- **Can't screenshot the overlay?** That's by design — it's hidden from screen capture so it
  can sit over the F7 coordinates without appearing in the OCR grabs. To capture it (e.g. to
  report a bug), launch with the environment variable `HISTATU_NO_EXCLUDE=1` set, which turns
  the capture-hiding off for that run.

## Building the .exe yourself

The published binary is built with [Nuitka](https://nuitka.net/) (chosen over
PyInstaller because it trips far fewer antivirus false positives). To build it
locally on Windows, just double-click **`build.bat`** — it installs Nuitka, refreshes
the icon, and produces `build\HistatuRunner.exe`. First run may download a C compiler
(accepted automatically). Requires Python 3.10+.

The same build runs in CI: [`.github/workflows/release-runner.yml`](../../.github/workflows/release-runner.yml)
builds the exe on every `v*` tag and attaches it to a GitHub Release. Cut a release with:

```
git tag v1.0.0 && git push origin v1.0.0
```

The build is **unsigned** — signing would need a paid certificate, so the exe stays
free and the trade-off is the one-time SmartScreen "Run anyway" prompt above. `icon.ico`
is generated by `make_icon.py` and checked in.
