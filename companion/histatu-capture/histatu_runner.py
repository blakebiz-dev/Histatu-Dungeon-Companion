"""Histatu Runner — in-game dungeon overlay for the Dungeon Loot Map.

Everything works by reading YOUR OWN screen (the F7 debug overlay) with OCR —
no game files touched, no injection, no packets. The F7 WORLD panel shows the
player's Position and Orientation plus a Target section for the block being
aimed at; that's all the app needs.

Modes
  Log chests    — press F on a chest (the game's open key) to add it to the
                  shared web map at its exact block coordinates.
  Record route  — every chest you open becomes the next stop; leg times are
                  the gaps between opens. Finish publishes the route for
                  everyone.
  Run route     — pick a published route (drawn on the shared web map) and
                  the app becomes a stopwatch: it counts your opens against
                  the route's stops and records your finish time to the
                  leaderboard. It never points anywhere — navigating the
                  route is the player's own skill.

Between OCR polls (every ~2s) the app coasts the position estimate: WASD
keys move it along the last panel-read heading. Every poll re-syncs to the
true values, so drift never accumulates. This estimate exists purely so an
open still logs correctly when the chest UI covers the F7 panel.

Chest opens are tracked per IGN in the shared store, timestamped, and marked
with the route being run (if any). All chests a player opened relock together
until the next daily reset (8 PM US Eastern, DST-aware) — that powers the
per-player cooldown here and the player view on the web map.

Windows: py -3 -m pip install pillow winsdk       (then run.bat)
Linux:   pip install pillow pytesseract + tesseract-ocr. X11: pynput + xdotool.
         Wayland (wlroots): grim + evdev (in the 'input' group) + hyprctl/swaymsg. (./run.sh)
Test:    py -3 histatu_runner.py --test
"""

import base64, calendar, ctypes, datetime, io, json, math, os, queue, random, re, shutil, subprocess, sys, threading, time, webbrowser
import urllib.request, urllib.error
import tkinter as tk
from tkinter import ttk, messagebox

IS_WIN = sys.platform == "win32"
# Wayland needs a different capture path (grim), window queries (hyprctl/swaymsg) and input
# (evdev) than X11 — none of the X11 tools (ImageGrab/scrot, xdotool, pynput) work under it.
IS_WAYLAND = (not IS_WIN) and bool(
    os.environ.get("WAYLAND_DISPLAY") or
    os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland")

try:
    from PIL import Image, ImageGrab
except ImportError:
    print("Missing dependency: pillow.  Install with:  pip install pillow")
    sys.exit(1)

if IS_WIN:
    try:
        import asyncio
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
        from winsdk.windows.security.cryptography import CryptographicBuffer
    except ImportError:
        print("Missing dependency: winsdk.  Install with:  py -3 -m pip install winsdk")
        sys.exit(1)
    winsound = None
    try:
        import winsound
    except ImportError:
        pass
else:
    winsound = None
    try:
        import pytesseract
    except ImportError:
        print("Missing dependency: pytesseract (plus the tesseract-ocr system package).")
        sys.exit(1)
    try:
        from pynput import keyboard as pynput_keyboard
    except ImportError:
        pynput_keyboard = None          # X11 input; on Wayland evdev is used instead
    try:
        import evdev                     # Wayland input (reads /dev/input; needs the 'input' group)
    except ImportError:
        evdev = None
    if not IS_WAYLAND and pynput_keyboard is None:
        print("Missing dependency: pynput.  Install with:  pip install pynput")
        sys.exit(1)
    if IS_WAYLAND and evdev is None:
        print("Note: for global key detection on Wayland, install python-evdev "
              "(pip install evdev) and add your user to the 'input' group.")

__version__ = "1.0.21"

# When packaged as a standalone .exe (Nuitka onefile / PyInstaller), __file__ points
# inside a temporary unpack dir that's wiped on exit — config written there wouldn't
# survive. Anchor to the directory holding the running executable instead, so
# capture_config.json (and capture_debug.png) live next to the .exe and persist.
FROZEN = getattr(sys, "frozen", False) or "__compiled__" in globals()
if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] or sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "capture_config.json")

# ---- edition ---------------------------------------------------------------
# Two builds ship from one source, chosen at build time:
#   lite   — no detection-report uploader; the button just points to the Full build.
#   full   — the public build (default): OCR + optional detection reports.
# The build writes a tiny _edition.py; from source it defaults to "full" and can be
# overridden with the HISTATU_EDITION env var for local testing.
try:
    from _edition import EDITION            # generated at build time (git-ignored)
except Exception:
    EDITION = os.environ.get("HISTATU_EDITION", "full")
if EDITION not in ("lite", "full"):
    EDITION = "full"
IS_LITE = EDITION == "lite"

DEFAULT_CONFIG = {
    "api_base": "https://blakebiz-dungeon-companion.vercel.app/api/dungeon",
    "window_title": "Hytale",
    "ign": "",
    "hotkey_chest": "F",        # the game's own open/interact key
    "hotkey_log": "",           # optional app-only key: log the chest you're AIMING at without
                                # opening it — for when the chest UI covers the F7 coordinates
    "hotkey_undo": "F10",
    "reset_hour_et": 20,        # daily chest reset: ALL chests you opened relock until this
                                # hour (US Eastern wall clock, DST-aware) comes around again
    "hud_reset_epoch": None,    # next reset as OBSERVED in the game HUD ("reset 19h 26m") —
                                # overrides the configured hour whenever the game tells us better
    "manual_reset_ms": None,    # epoch ms of a MANUAL "all my cooldowns reset" press — for the random
                                # in-game event that relocks nothing / unlocks everything ahead of the
                                # daily reset. Acts as a reset cut: chests opened before it read as up.
    "travel_max_sec": 300,      # gaps longer than this between opens aren't counted as travel
                                # (excludes AFK/combat breaks); shorter is always recorded
    "ocr_poll_sec": 1.8,        # position re-sync cadence in active modes
    "move_speed": 4.5,          # blocks/sec while holding W (used to coast the position estimate
                                # between panel reads for the covered-panel logging fallback)
    "only_when_game_focused": True,
    "write_key": "",            # editor key: unlocks logging/editing/verify (blank = submit-only)
    "collapsed": False,         # overlay starts minimized (title bar + status only)
    "win_x": None,              # remembered overlay position (None = auto-dock top-right)
    "win_y": None,
    "dry_run": False,
    "check_updates": True,      # on launch, check the site for a newer packaged build
    "skip_update": "",          # a release tag the user chose to skip (don't nag again for it)
    "setup_done": 0,            # Capture Doctor version last completed/dismissed (0 = never →
                                # the one-time setup wizard is offered on launch)
    "ocr_scale_hint": None,     # measured best OCR upscale for THIS device's panel strip — tried
                                # first on every read; the normal scale sweep remains the fallback
    "setup_health": None,       # baseline snapshot from the last Capture Doctor run (per-signal
                                # hit-rates + measured pace) — used to spot capture regressions
}

# ---- daily chest reset (8 PM US Eastern by default) --------------------------
# The game relocks every chest a player opened, all at once, at a fixed Eastern
# wall-clock hour — NOT a rolling per-chest cooldown. DST is handled with the
# post-2007 US rule directly (second Sunday of March -> first Sunday of November)
# so the frozen .exe needs no timezone database.

def _us_dst_on(y, m, d):
    """Is US daylight saving active at EVENING hours on this date? (The reset hour
    is far from the 2 AM switch, so date-level granularity is exact for it.)"""
    if m < 3 or m > 11:
        return False
    if 3 < m < 11:
        return True
    sundays = [w[calendar.SUNDAY] for w in calendar.monthcalendar(y, m) if w[calendar.SUNDAY]]
    if m == 3:
        return d >= sundays[1]   # DST begins the second Sunday of March
    return d < sundays[0]        # DST ends the first Sunday of November


def _reset_at(y, m, d, hour_et):
    """Epoch seconds of the reset on the given US-Eastern calendar date."""
    off = -4 if _us_dst_on(y, m, d) else -5          # ET = UTC + off
    return calendar.timegm((y, m, d, hour_et, 0, 0, 0, 0, 0)) - off * 3600


def last_daily_reset(now=None, hour_et=20):
    """Epoch seconds of the most recent daily chest reset. A chest open BEFORE this
    instant is unlocked again; an open at/after it stays locked until the next reset."""
    now = time.time() if now is None else now
    for back in range(3):  # scan a few days back: UTC/ET date skew + DST are all covered
        d = datetime.datetime.fromtimestamp(now, datetime.timezone.utc) - datetime.timedelta(days=back)
        t = _reset_at(d.year, d.month, d.day, hour_et)
        if t <= now:
            return t
    return now - 86400  # unreachable


def next_daily_reset(now=None, hour_et=20):
    """Epoch seconds of the upcoming daily chest reset (for countdown displays)."""
    now = time.time() if now is None else now
    for fwd in range(3):
        d = datetime.datetime.fromtimestamp(now, datetime.timezone.utc) + datetime.timedelta(days=fwd)
        t = _reset_at(d.year, d.month, d.day, hour_et)
        if t > now:
            return t
    return now + 86400  # unreachable


def point_in_poly(x, y, pts):
    """Ray-cast point-in-polygon over [[x,y]..] fracs — TOGGLES on each crossing (an even
    count means outside; the JS twin had a set-instead-of-toggle bug once, hence the tests)."""
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i][0], pts[i][1]
        xj, yj = pts[j][0], pts[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def reset_hour_from_epoch(ov):
    """The ET wall-clock hour of an observed reset instant, or None when it isn't near a whole
    hour (HUD readings carry ~minute granularity, so a 3-min tolerance). Lets one observation
    pin the DST-aware model to whatever hour the server actually uses — adjacent resets then
    come out 23/25h apart on transition days instead of a naive ±24h."""
    d = datetime.datetime.fromtimestamp(ov, datetime.timezone.utc)
    off = -4 if _us_dst_on(d.year, d.month, d.day) else -5
    local = (ov + off * 3600) % 86400
    hour = int(round(local / 3600.0)) % 24
    return hour if abs(local - round(local / 3600.0) * 3600) <= 180 else None


# ---- update check (packaged builds) -----------------------------------------
# Releases are served through the site's own endpoint (api/download.js redirects to
# the latest GitHub release of the public repo) so the app has ONE stable URL that
# never changes with repo/hosting details. ?meta=1 returns {tag, ...}; the bare
# endpoint 302s straight to the newest exe.
SITE_BASE = "https://blakebiz-dungeon-companion.vercel.app"
UPDATE_META_URL = SITE_BASE + "/api/download?meta=1"
DOWNLOAD_URL = SITE_BASE + "/api/download"
DEBUG_UPLOAD_URL = SITE_BASE + "/api/debug"
APP_PAGE_URL = SITE_BASE + "/tools/dungeon-loot-map/get-app.html"
DEBUG_WINDOW_SEC = 180      # a detection-report capture runs for this long, then auto-sends
DEBUG_MAX_FRAMES = 12       # cap frames per report so uploads stay small
DEBUG_MAX_BYTES = 900000    # cap total pre-base64 image bytes (~well under the endpoint's limit)


def self_update_swap(exe_path, new_path):
    """File mechanics of the in-place update. Windows can't overwrite a RUNNING exe but it CAN
    rename one — so the live binary is renamed aside and the downloaded one takes its name.
    Restores the original on any failure (never leaves no exe behind). Returns the backup path,
    which the NEXT launch deletes."""
    old_path = exe_path + ".old"
    if os.path.exists(old_path):
        os.remove(old_path)
    os.rename(exe_path, old_path)
    try:
        os.rename(new_path, exe_path)
    except Exception:
        os.rename(old_path, exe_path)
        raise
    return old_path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An opener handler that does NOT auto-follow 3xx — so the download endpoint's 302 to the
    signed asset URL is read from the Location header and fetched as a separate request."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_version(s):
    """'v1.2.3' / '1.2' -> (1,2,3) for comparison; junk -> (0,)."""
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def latest_release(timeout=6):
    """(tag, exe_download_url) for the newest release via the site, or None on any error."""
    req = urllib.request.Request(UPDATE_META_URL,
                                 headers={"User-Agent": "HistatuRunner/" + __version__})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag")
    # self-update to the SAME edition the user is running (lite→lite, full→full)
    return (tag, DOWNLOAD_URL + "?edition=" + EDITION) if tag else None

VK = {"LMB": 0x01, "RMB": 0x02, "MMB": 0x04, "MOUSE4": 0x05, "MOUSE5": 0x06,
      "DELETE": 0x2E, "DEL": 0x2E, "INSERT": 0x2D, "END": 0x23, "HOME": 0x24,
      "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
      "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B}


def vk_of(name):
    name = str(name).strip().upper()
    if name in VK:
        return VK[name]
    if len(name) == 1 and name.isalnum():
        return ord(name)
    return None


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            for k in DEFAULT_CONFIG:
                if k in stored:
                    cfg[k] = stored[k]
    except FileNotFoundError:
        save_config(cfg)
    except (json.JSONDecodeError, OSError):
        pass
    return cfg


_CONFIG_LOCK = threading.Lock()


def save_config(cfg):
    # atomic + thread-safe: several threads persist config (IGN edits, calibration)
    try:
        with _CONFIG_LOCK:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: cfg[k] for k in DEFAULT_CONFIG}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


# ====================================================================== #
#  platform: window finding / focus / screen grab                        #
# ====================================================================== #

def make_dpi_aware():
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def exclude_from_capture(tk_window):
    """Hide the overlay from screen captures (Windows 10 2004+) so it never
    appears in the OCR grabs — it can then sit right over the F7 panel and the
    tool still reads the game underneath. No-op elsewhere."""
    if not IS_WIN or os.environ.get("HISTATU_NO_EXCLUDE"):
        return False  # env escape: lets the overlay be screenshotted (debugging / design work)
    try:
        tk_window.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(tk_window.winfo_id(), 2)  # GA_ROOT
        # WDA_EXCLUDEFROMCAPTURE = 0x11
        return bool(user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), 0x11))
    except Exception:
        return False


if IS_WIN:
    def _window_title(hwnd):
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()

    def find_game_window(title):
        user32 = ctypes.windll.user32
        result = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_cb(hwnd, _):
            if user32.IsWindowVisible(hwnd) and _window_title(hwnd) == title:
                result.append(hwnd)
                return False
            return True

        user32.EnumWindows(enum_cb, None)
        if not result:
            return None
        hwnd = result[0]

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                        ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        rc = RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
            return None
        pt = POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        w, h = rc.r - rc.l, rc.b - rc.t
        if w < 200 or h < 200:
            return None
        return (pt.x, pt.y, pt.x + w, pt.y + h)

    def game_focused(title):
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return bool(hwnd) and _window_title(hwnd) == title
else:
    def _run(cmd, timeout=2):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None

    def _title_match(name, title):
        name = str(name or "")
        return name == title or (title and title.lower() in name.lower())

    def _wl_window(title):
        """Game window bbox on Wayland via the compositor's IPC (Hyprland / Sway / wlroots)."""
        r = _run(["hyprctl", "clients", "-j"])          # Hyprland
        if r and r.returncode == 0:
            try:
                for c in json.loads(r.stdout):
                    if _title_match(c.get("title"), title) or _title_match(c.get("class"), title):
                        (x, y), (w, h) = c["at"], c["size"]
                        if w >= 200 and h >= 200:
                            return (int(x), int(y), int(x + w), int(y + h))
            except Exception:
                pass
        r = _run(["swaymsg", "-t", "get_tree"])         # Sway / other i3-compatible
        if r and r.returncode == 0:
            hit = [None]
            def walk(n):
                if _title_match(n.get("name"), title) and n.get("rect") and n.get("pid"):
                    rc = n["rect"]
                    if rc["width"] >= 200 and rc["height"] >= 200:
                        hit[0] = (rc["x"], rc["y"], rc["x"] + rc["width"], rc["y"] + rc["height"])
                for k in ("nodes", "floating_nodes"):
                    for ch in n.get(k, []):
                        walk(ch)
            try:
                walk(json.loads(r.stdout))
                return hit[0]
            except Exception:
                pass
        return None

    def _x11_window(title):
        wid = _run(["xdotool", "search", "--name", "^" + title + "$"])
        if not wid or not wid.stdout.split():
            return None
        out = _run(["xdotool", "getwindowgeometry", "--shell", wid.stdout.split()[0]])
        if not out:
            return None
        try:
            g = dict(l.split("=") for l in out.stdout.strip().splitlines() if "=" in l)
            x, y, w, h = int(g["X"]), int(g["Y"]), int(g["WIDTH"]), int(g["HEIGHT"])
            return (x, y, x + w, y + h) if w >= 200 and h >= 200 else None
        except Exception:
            return None

    def find_game_window(title):
        return _wl_window(title) if IS_WAYLAND else _x11_window(title)

    def game_focused(title):
        if IS_WAYLAND:
            r = _run(["hyprctl", "activewindow", "-j"])
            if r and r.returncode == 0:
                try:
                    aw = json.loads(r.stdout)
                    return _title_match(aw.get("title"), title) or _title_match(aw.get("class"), title)
                except Exception:
                    pass
            r = _run(["swaymsg", "-t", "get_tree"])
            if r and r.returncode == 0:
                try:
                    def focused(n):
                        if n.get("focused"):
                            return n
                        for k in ("nodes", "floating_nodes"):
                            for ch in n.get(k, []):
                                f = focused(ch)
                                if f:
                                    return f
                        return None
                    f = focused(json.loads(r.stdout))
                    return _title_match(f.get("name"), title) if f else True
                except Exception:
                    pass
            return True  # couldn't tell — don't block the user
        r = _run(["xdotool", "getactivewindow", "getwindowname"])
        return (r.stdout.strip() == title) if r else True  # exact (unchanged X11 behaviour)


def _grim_grab(bbox):
    """Screen capture on Wayland via grim (wlroots). Returns a PIL Image or None."""
    geo = ("%d,%d %dx%d" % (bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])) if bbox else None
    cmd = ["grim"] + (["-g", geo] if geo else []) + ["-t", "png", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=4)
        if r.returncode == 0 and r.stdout:
            return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except Exception:
        pass
    return None


def grab_game(cfg):
    bbox = find_game_window(cfg["window_title"])
    if not IS_WIN and IS_WAYLAND:
        img = _grim_grab(bbox)
        if img is not None:
            return img
        # grim missing/failed — surface a clear, actionable error (the poller shows it)
        raise RuntimeError("Wayland screen capture failed — install 'grim' (sudo pacman -S grim)")
    if bbox:
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    return ImageGrab.grab()


# ====================================================================== #
#  platform: key state + hotkey edges + raw mouse                        #
# ====================================================================== #

class InputWatcher:
    """Tracks WASD held-state and fires hotkey edge events (cross-platform)."""

    def __init__(self, hotkeys):
        self.hotkeys = hotkeys          # name -> callback tag
        self.events = queue.Queue()     # tags on key-down edges
        self.held = set()               # {'w','a','s','d'}
        self._pressed = set()
        if IS_WIN:
            self._state = {}
        elif IS_WAYLAND and evdev is not None:
            self._start_evdev()          # Wayland: read /dev/input (pynput can't see global keys)
        elif pynput_keyboard is not None:
            listener = pynput_keyboard.Listener(on_press=self._on_press,
                                                on_release=self._on_release)
            listener.daemon = True
            listener.start()
        # else: no input backend (Wayland without evdev) — warned at startup

    # -- Wayland: evdev (/dev/input) --
    def _ev_code(self, name):
        """Our key name -> evdev event code (mirrors vk_of, tolerating a few aliases)."""
        ec = evdev.ecodes
        name = {"DEL": "DELETE", "INS": "INSERT"}.get(str(name).strip().upper(), str(name).strip().upper())
        special = {"DELETE": "KEY_DELETE", "END": "KEY_END", "HOME": "KEY_HOME", "INSERT": "KEY_INSERT",
                   "LMB": "BTN_LEFT", "RMB": "BTN_RIGHT", "MMB": "BTN_MIDDLE",
                   "MOUSE4": "BTN_SIDE", "MOUSE5": "BTN_EXTRA"}
        if name in special:
            return getattr(ec, special[name], None)
        if len(name) == 1 and name.isalnum():
            return getattr(ec, "KEY_" + name, None)
        if re.fullmatch(r"F([1-9]|1[0-2])", name):
            return getattr(ec, "KEY_" + name, None)
        return None

    def _start_evdev(self):
        ec = evdev.ecodes
        self._ev_wasd = {getattr(ec, "KEY_" + c.upper(), -1): c for c in "wasd"}
        started = 0
        try:
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                except Exception:
                    continue
                if ec.EV_KEY in dev.capabilities():   # keyboards + mice
                    threading.Thread(target=self._evdev_loop, args=(dev,), daemon=True).start()
                    started += 1
        except Exception:
            pass
        if not started:
            print("Note: no readable input devices — add your user to the 'input' group "
                  "(sudo usermod -aG input $USER), then log out and back in.")

    def _evdev_loop(self, dev):
        try:
            for e in dev.read_loop():
                if e.type != evdev.ecodes.EV_KEY:
                    continue
                code, val = e.code, e.value       # val: 1 down, 0 up, 2 repeat
                if code in self._ev_wasd:
                    if val == 1:
                        self.held.add(self._ev_wasd[code])
                    elif val == 0:
                        self.held.discard(self._ev_wasd[code])
                    continue
                if val == 1:                      # key-down edge only
                    for name, tag in list(self.hotkeys.items()):
                        if self._ev_code(name) == code:
                            self.events.put(tag)
                            break
        except Exception:
            pass  # device unplugged / read error — its thread just ends

    # -- Windows: polled from the UI loop --
    def poll(self):
        if not IS_WIN:
            return
        gaks = ctypes.windll.user32.GetAsyncKeyState
        for ch in "WASD":
            (self.held.add if gaks(ord(ch)) & 0x8000 else self.held.discard)(ch.lower())
        for name, tag in self.hotkeys.items():
            vk = vk_of(name)
            if not vk:
                continue
            down = bool(gaks(vk) & 0x8000)
            if down and not self._state.get(vk):
                self.events.put(tag)
            self._state[vk] = down

    # -- Linux: pynput callbacks --
    def _keyname(self, key):
        try:
            if hasattr(key, "char") and key.char:
                return key.char.upper()
            return key.name.upper().replace("_", "")  # f10 -> F10
        except Exception:
            return ""

    def _on_press(self, key):
        name = self._keyname(key)
        if name.lower() in ("w", "a", "s", "d"):
            self.held.add(name.lower())
        if name not in self._pressed:
            self._pressed.add(name)
            for hk, tag in self.hotkeys.items():
                if str(hk).strip().upper() == name:
                    self.events.put(tag)

    def _on_release(self, key):
        name = self._keyname(key)
        self.held.discard(name.lower())
        self._pressed.discard(name)


# ====================================================================== #
#  OCR + parsing                                                         #
# ====================================================================== #

OCR_LOCK = threading.Lock()

if IS_WIN:
    def _to_software_bitmap(img):
        rgba = img.convert("RGBA")
        buf = CryptographicBuffer.create_from_byte_array(rgba.tobytes())
        return SoftwareBitmap.create_copy_from_buffer(
            buf, BitmapPixelFormat.RGBA8, rgba.width, rgba.height)

    async def _recognize(img):
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("Windows OCR unavailable (no OCR language installed)")
        result = await engine.recognize_async(_to_software_bitmap(img))
        lines = []
        for l in result.lines:
            ys = [w.bounding_rect.y for w in l.words]
            lines.append((l.text, min(ys) if ys else 0))
        return lines

    def ocr_lines(img, scale):
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        with OCR_LOCK:
            res = asyncio.run(_recognize(img))
        return [(t, y / scale) for t, y in res]
else:
    def ocr_lines(img, scale):
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        with OCR_LOCK:
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        rows = {}
        for i, txt in enumerate(data["text"]):
            if not txt.strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            rows.setdefault(key, {"words": [], "y": data["top"][i]})
            rows[key]["words"].append(txt)
            rows[key]["y"] = min(rows[key]["y"], data["top"][i])
        return [(" ".join(r["words"]), r["y"] / scale) for r in rows.values()]


def ocr_text(img, scale):
    return " ".join(t for t, _ in ocr_lines(img, scale))


def ocr_scales(width):
    """Upscale factors that normalize the F7 text to a good OCR size regardless of resolution.
    Aim for an effective region width near 3000px (raised from 2200: at 1440p the panel digits
    were small enough that OCR dropped characters MID-NUMBER — '-106.000' read as '-1.000',
    logging chests at 1/100th coordinates — and a sharper upscale is the root-cause fix).
    Clamped to 2..5, two nearby factors for retry robustness. (1080p→[4,5], 1440p→[3,4], 4K→[2,3].)"""
    s = int(3000 // max(1, width)) or 2
    s = max(2, min(5, s))
    other = s + 1 if s < 5 else s - 1
    return sorted({s, other})


NUM = r"(-?\s?\d+(?:\s?\.\s?\d+)?)"  # OCR splits signs ("- 160.50") and pads decimal points
                                     # ("71 . 000", "-111 .194") — _num() strips the spaces
TRIPLE = r"\(\s*" + NUM + r"\s*,\s*" + NUM + r"\s*,\s*" + NUM + r"\s*\)"
# Position components must carry their decimals: the panel always prints "76.000", so a bare
# "7" is the chest popup OCCLUDING the rest of the number (observed at 1440p: only the first
# glyph of the y survives, the visible comma stitches a structurally-valid-but-wrong triple).
# Rejecting it turns partial occlusion into a clean miss — dead reckoning coasts through and
# the covered-panel fallback / HUD counter still catch the open itself.
NUMF = r"(-?\s?\d+\s?\.\s?\d{2,3})"
POS_RE = (r"Position:?\s*\(\s*" + NUMF + r"\s*,\s*" + NUMF + r"\s*,\s*" + NUMF + r"\s*\)")
ORI_RE = r"Orientation:?\s*\(\s*" + NUM + r"[^,]*,\s*" + NUM + r"[^,]*,"
COMPASS = {"north": 0.0, "west": 90.0, "east": -90.0, "south": 180.0}
# Fallback yaw anchor: the yaw is the number between the mid comma and "<roll≈0.00>) <Cardinal>". The
# cardinal and this tail survive OCR far more reliably than the "Orientation:" label + pitch (which get
# split, reordered, or merged into the next line at 4K), so this recovers most yaws the strict pattern
# misses. The yaw is required to have a COMMA before it (with only sign-free junk): the pitch follows
# "(", never a comma, so this structurally prevents latching onto the pitch when OCR drops the yaw
# token or the pitch→yaw comma. The excluded '-' keeps the yaw's own sign out of the junk. parse_yaw
# also cross-checks the recovered angle against the cardinal it captured (group 2). Angles are bounded
# to 3 integer digits: |yaw|<360, and it stops any long-digit backtracking. The cardinal needs a word
# boundary so a chest/zone name like "Northgate" can't masquerade as the compass word.
_ANG = r"(-?\s?\d{1,3}(?:\s?\.\s?\d{1,3})?)"
_CARD = r"(North|South|East|West)(?![A-Za-z])"
ORI_ROLL_RE = r",[^-,()0-9]{0,4}" + _ANG + r"\s*,\s*-?\s?0\s?\.?\s?\d{0,2}\s*\)\s*" + _CARD
# Motion lines for dead-reckoning (all OPTIONAL — absent/mangled OCR falls back to the WASD + learned
# move_speed model, so these only ever ADD accuracy). Velocity is world-axis blocks/sec; Speed is its
# horizontal magnitude (accurate for BOTH walk and sprint, unlike the single learned move_speed);
# Wish Dir is the horizontal input direction in world space.
VEL_RE = r"Velocity:?\s*\(\s*" + NUM + r"\s*,\s*" + NUM + r"\s*,\s*" + NUM + r"\s*\)"
SPEED_RE = r"Speed:?\s*(-?\d{1,3}\s?\.\s?\d{1,3})(?!\s*[,)])"  # lone scalar; reject a (x, y) tuple misread
WISH_RE = r"Wish\s*Dir:?\s*\(\s*" + NUM + r"\s*,\s*" + NUM + r"\s*\)"
# looser triple for the Target line: parens are optional/mangled by OCR, and the separator may
# come through as a comma or a semicolon. (Position/Orientation keep the strict TRIPLE.)
_SEP = r"\s*[,;]\s*"
TRIPLE_LOOSE = r"[\(\[\{|]?\s*" + NUM + _SEP + NUM + _SEP + NUM + r"\s*[\)\]\}|]?"
_BLOCKNAME = r"([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)"  # e.g. Furniture_Village_Chest_Large


def _num(s):
    return float(s.replace(" ", ""))


def parse_target_block(text):
    # Tolerant anchors, tried BOTTOM-UP: the true Target line is the panel's last, but a trailing
    # target-ish token (a second Target row, OCR junk after the name) must not kill the read —
    # each anchor gets a chance, latest first. An early noise token (before Position/Orientation)
    # only wins when nothing later parses, and the echo checks below still guard what it adopts.
    # "\bblock\b" is an equal anchor: at high resolutions OCR regularly loses the word "Target"
    # while "Block (x, y, z)" survives — it's the only panel line that says Block before a triple
    # (\b keeps "Skyblock" out).
    for m in reversed(list(re.finditer(r"tar?ge?t|\bblock\b", text, re.I))):
        rest = text[m.end():]
        tm = re.search(TRIPLE_LOOSE, rest)
        # The triple must sit near ITS anchor ("Target: Block @ (" is ~16 chars; OCR junk can pad
        # it to ~30). An unbounded search would adopt the first triple of some LOWER panel line —
        # an orientation/rotation readout like (0.0, 180.0, 0.0), typically 50+ chars away — and
        # log a chest at pitch/yaw values. No nearby triple = try an earlier anchor, never guess.
        if not tm or tm.start() > 40:
            continue
        # a 4th number right after the triple means a decimal point was read as a separator
        # ("57,000" -> "57","000"), so which three values are the coords is ambiguous
        if re.match(r"\s*[,;]\s*-?\s?\d", rest[tm.end():]):
            continue
        coords = tuple(_num(tm.group(i)) for i in (1, 2, 3))
        # Same-frame echo rejection: a "target" triple that just repeats the panel's Position or
        # Orientation values means this anchor adopted a neighbouring line's numbers. Real
        # interactions can't collide with either — you never stand INSIDE the chest block, and
        # pitch/yaw/roll matching all three coords within 0.5 is no coincidence.
        ppos = parse_position(text)
        if ppos and all(abs(coords[i] - ppos[i]) < 0.5 for i in range(3)):
            continue
        echo = False
        for om in re.finditer(r"orientation", text[:m.start()], re.I):
            ot = re.search(TRIPLE_LOOSE, text[om.end():m.start()])
            if ot and ot.start() <= 24:  # the ori triple sits right after its label
                ori = tuple(_num(ot.group(i)) for i in (1, 2, 3))
                # yaw compares wrap-aware: the panel prints 270 where elsewhere it reads -90
                if abs(coords[0] - ori[0]) < 0.5 and abs(wrap_deg(coords[1] - ori[1])) < 0.5 \
                        and abs(coords[2] - ori[2]) < 0.5:
                    echo = True
                    break
        if echo:
            continue
        # block name: prefer the Word_Word right after the coords, else the first one on the line
        nm = re.search(_BLOCKNAME, rest[tm.end():]) or re.search(_BLOCKNAME, rest)
        name = nm.group(1) if nm else None
        if not name:
            # Some panel layouts don't print the block name at all — the aimed object shows a big
            # ALL-CAPS nameplate instead ("SMALL WIND TEMPLE CHEST"). Accept a singular ...CHEST
            # nameplate from anywhere in the frame text — but NEVER the game HUD's permanent
            # chest counter ("HISTATU DUNGEON WORLD <area> CHESTS > ... 46/321"). A correctly-read
            # plural can't match (\b fails inside CHESTS), and because one dropped/split glyph
            # ("CHEST S >", "CHEST >") is common in OCR, two structural guards back it up:
            # anything '>'-adjacent is counter context, and header vocabulary disqualifies the
            # match outright.
            np = re.search(r"\b(?:[A-Z0-9]{2,}\s+){1,5}CHEST\b(?!\s*\S{0,2}\s*>)", text)
            if np and not re.search(r"HISTATU|DUNGEON|WORLD|RESET", np.group(0)):
                name = np.group(0).strip()
        return {"coords": coords, "block": name}
    return None


def _within_edit1(a, b):
    """True if `a` is within Levenshtein distance 1 of `b` (one sub / insert / delete)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = j = edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            edits += 1
            if edits > 1:
                return False
            if la == lb:
                i += 1; j += 1
            elif la > lb:
                i += 1
            else:
                j += 1
    return edits + (la - i) + (lb - j) <= 1


def looks_like_chest(name):
    """OCR-tolerant test for whether a block name is a chest — so a single misread character in a
    long name (Furniture_Village_Che5t_Large) doesn't cause the chest to be silently skipped.
    Kept tight — the 'ch' of chest (tolerating h->n/l) followed by 'est' within one edit — so
    unrelated blocks (Oak_Door, Crafting_Table, Wall_Torch, Mountain_Crest) never match."""
    s = str(name or "").lower()
    if "chest" in s:
        return True
    for m in re.finditer(r"c[hnl]", s):
        tail = s[m.end():m.end() + 4]
        for w in (2, 3, 4):
            if _within_edit1(tail[:w], "est"):
                return True
    return False


# covered-panel fallback guards: a cached target is trusted only if it's recent AND the player is
# dead-reckoned to be near it (within FALLBACK_MAX_DIST blocks and, if heading is known, facing
# within FALLBACK_MAX_ANGLE°) — so we never log a chest you merely glanced at and walked past.
# Chests open from up to ~8 blocks away, so the radius is that range plus dead-reckoning drift.
FALLBACK_MAX_AGE = 4.0
FALLBACK_MAX_DIST = 10.0
FALLBACK_MAX_ANGLE = 45.0
# a targeted chest is within the ~8-block interact range of the player, but the dead-reckoned
# position can lag ~16 blocks at sprint speeds (~6.5 b/s × 2.5s polls) — so the gate carries
# generous drift headroom. It's defense-in-depth now: the parser-level echo rejection catches
# misreads first, and truly wrong coords land hundreds of blocks away, far beyond this.
TARGET_MAX_DIST = 30.0
TARGET_MAX_Y = 20.0  # vertical is tighter; stale frames (no same-frame position) get 2.5x this
# a position fix jumping farther than this from the estimate needs a second consistent read before
# it's accepted (real teleports confirm a poll later; a one-frame OCR misread never lands).
JUMP_CONFIRM_DIST = 24.0
# legacy locally-generated route ids — never shareable, never recorded to the leaderboard
# (guards in record_run/_on_chest_open keep an old config or hand-edited route id harmless)
LOCAL_ROUTE_IDS = ("autoroute", "scoutroute", "auditroute")
# directed leg times (A->B can differ from B->A: one-way drops, cliffs, water). Stored ALONGSIDE the
# symmetric `pairs`, never replacing it, as legs["fromKey>toKey"] = {t, n, at}: min directed seconds,
# sample count (confidence), last-measured epoch (recency). The runner only COLLECTS this data —
# the web map's planner and coverage heatmap consume it.
LEG_CAP_N = 250            # cap the stored sample count so confidence stays a small bounded int


def chest_coords(target):
    """Integer (x, y, z) if the HUD target block is a chest, else None. `target` is a
    parse_target_block() result (or None)."""
    if not target or not target.get("block"):
        return None
    if not looks_like_chest(target["block"]):
        return None
    try:
        return tuple(int(round(c)) for c in target["coords"])
    except (TypeError, ValueError):
        return None


# one HUD area row: "Solmara 58/327" / "> The Hollow 44/99", with tolerance for background
# text bleeding through the semi-transparent panel on either side of the row
_HUD_ROW = re.compile(
    r"([A-Za-z][A-Za-z']*(?: [A-Za-z'][A-Za-z']*){0,2})\s+(\d{1,4})\s*/\s*(\d{1,4})(?!\d)\D{0,12}$")
_HUD_TITLE = re.compile(r"^\s*[-–—]\s*([A-Za-z][A-Za-z' ]{1,24}?)\s*[-–—]\s*$")


def parse_hud_panel(lines):
    """Structured read of the game's movable chest HUD:

        HISTATU DUNGEON WORLD / - THE HOLLOW - / CHESTS  reset 8h 33m
        Solmara 58/327 · Thornvale 0/61 · ... · > The Hollow 44/99

    One row per area; '>' (and the title between dashes) marks the current one. `lines` is
    ocr_lines() output [(text, y)]. Returns {"areas": {name: (opened, total)}, "current":
    name-or-None, "reset": secs-or-None, "y0": px, "y1": px}, or None when these lines don't
    look like the panel. The panel is semi-transparent over other UI, so unrelated OCR text
    interleaves freely — rows are matched by their name+N/M shape and the rest is ignored."""
    areas, ys, current, title, reset_secs = {}, [], None, None, None
    for t, y in lines:
        if reset_secs is None:
            rs = parse_hud_reset(t)
            if rs:
                reset_secs = rs
                ys.append(y)
        m = _HUD_TITLE.match(t)
        if m:
            title = m.group(1).strip()
            ys.append(y)
            continue
        m = _HUD_ROW.search(t)
        if m:
            name = m.group(1).strip()
            opened, total = int(m.group(2)), int(m.group(3))
            if 0 <= opened <= total and 5 <= total <= 9999:
                areas[name] = (opened, total)
                ys.append(y)
                if ">" in t[max(0, m.start(1) - 4):m.start(1)]:
                    current = name  # the highlighted "> Current Area" row
    # require a confident panel: several area rows, or one row plus the reset countdown
    if not (len(areas) >= 2 or (areas and reset_secs is not None)):
        return None
    if current is None and title:
        for n in areas:
            if n.lower() == title.lower():
                current = n
                break
    return {"areas": areas, "current": current, "reset": reset_secs,
            "y0": min(ys), "y1": max(ys)}


def parse_hud_reset(text):
    """'reset 19h 26m' (or '26m' / '45s' forms) from the game HUD -> seconds until the daily
    chest reset, or None. Lets the app sync the reset instant from the game itself instead of
    trusting the configured wall-clock hour."""
    # anchored to the CHESTS line like the counter — a stray "reset ..." elsewhere never counts
    m = re.search(r"chests?\b[^/]{0,60}?\breset\s*:?\s*(?:(\d{1,2})\s*h)?\s*(?:(\d{1,2})\s*m)?"
                  r"\s*(?:(\d{1,2})\s*s)?", text, re.I)
    if not m or not any(m.groups()):
        return None
    h, mn, s = (int(g or 0) for g in m.groups())
    secs = h * 3600 + mn * 60 + s
    return secs if 0 < secs <= 90000 else None


def parse_position(text):
    m = re.search(POS_RE, text, re.I)
    return tuple(_num(m.group(i)) for i in (1, 2, 3)) if m else None


def _yaw_head(text):
    """The panel text ABOVE the aimed-block section. The player's Orientation always prints above
    Target, whose block prints its own "Rotation: (0.0°, 0.0°, 0.0°) North" — a tail structurally
    identical to an Orientation tail but ALWAYS North/zero regardless of facing. Cutting the block
    section off keeps the roll+cardinal anchor (and the label-free compass) from ever latching onto
    it, which would otherwise read 0°/North while you aim East/South."""
    return re.split(r"\bTarget\b|Block\s*@|\bRotation\b", text, maxsplit=1, flags=re.I)[0]


def parse_yaw(text):
    """Orientation: (pitch, yaw, roll) — yaw: 0=North(-Z), +90=West(-X). The roll+cardinal-anchored
    match is tried FIRST: it pins the yaw to the "<yaw>, 0.00) <Cardinal>" tail and is SELF-CHECKED
    against the cardinal it captured (a real yaw is within 45° of its cardinal, so a >50° disagreement
    means OCR dropped the yaw token and we latched onto the pitch — reject). This both recovers yaws
    the strict label pattern misses AND avoids ORI_RE's failure of stitching a far-away number (the
    game-clock hour) as yaw on reordered panels. The strict ORI_RE is the fallback for frames where
    the roll/cardinal tail itself got mangled but the label + pitch + yaw survived. The search is
    confined to the pre-block text so the aimed block's fixed North/zero Rotation line can't win."""
    head = _yaw_head(text)
    m = re.search(ORI_ROLL_RE, head, re.I)
    if m:
        y = wrap_deg(_num(m.group(1)))
        if abs(wrap_deg(y - COMPASS[m.group(2).lower()])) <= 50:
            return y
    m = re.search(ORI_RE, head, re.I)
    if m:
        y = wrap_deg(_num(m.group(2)))
        # The strict pattern's [^,]* gaps can absorb a space-for-comma and stitch a far number (the
        # roll 0.00, or the game clock) as "yaw". Hold it to the SAME cardinal cross-check as the roll
        # anchor: a real yaw is within ~45° of its compass word. (An in-bucket sign flip near a
        # cardinal, e.g. -42→+42 both "North", is below this window and can't be caught by the compass
        # alone — a known residual the velocity-heading repair is meant to cover.)
        cw = parse_yaw_compass(text)
        if cw is None or abs(wrap_deg(y - cw)) <= 50:
            return y
    return None


def parse_yaw_compass(text):
    """Coarse fallback: the cardinal word after the Orientation tuple. Tries the labelled form first,
    then a label-free roll+cardinal anchor — so it still returns a heading when OCR mangled the
    'Orientation:' label (exactly when the roll-anchored yaw above needs a coherence partner). Scoped
    to the pre-block text so it can't read the block Rotation line's fixed North as the player's."""
    head = _yaw_head(text)
    m = re.search(r"Orientation[^)]{0,40}\)\s*" + _CARD, head, re.I)
    if not m:
        m = re.search(r"-?\s?0\s?\.?\s?\d{0,2}\s*\)\s*" + _CARD, head, re.I)
    return COMPASS[m.group(1).lower()] if m else None


def parse_speed(text):
    """Horizontal ground speed (blocks/sec) from the panel's Speed field, or None if unreadable."""
    m = re.search(SPEED_RE, text, re.I)
    if not m:
        return None
    v = abs(_num(m.group(1)))
    return v if v <= 30 else None  # reject a garbage misread; real speeds are only a few b/s


def parse_velocity(text):
    """World-axis velocity (vx, vy, vz) blocks/sec, or None. Scored by the Capture Doctor's
    panel health check."""
    m = re.search(VEL_RE, text, re.I)
    return tuple(_num(m.group(i)) for i in (1, 2, 3)) if m else None


def parse_wishdir(text):
    """Horizontal input direction (x, z) in world space, or None. Scored by the Capture Doctor's
    panel health check."""
    m = re.search(WISH_RE, text, re.I)
    return (_num(m.group(1)), _num(m.group(2))) if m else None


def marker_chest_count(m):
    """How many chests a marker stands for: a group pin counts as its pack (clamped to a sane
    1..999 — a garbage count never zeroes or explodes the stats), anything else as 1."""
    if m.get("kind") == "group":
        try:
            return max(1, min(999, int(m.get("count") or 1)))
        except (TypeError, ValueError):
            return 1
    return 1


def wrap_deg(a):
    a = math.fmod(a, 360.0)
    if a > 180.0:
        a -= 360.0
    if a <= -180.0:
        a += 360.0
    return a


def bearing_to(px, pz, tx, tz):
    """Yaw the player would face to look at the target (same convention)."""
    return math.degrees(math.atan2(-(tx - px), -(tz - pz)))


# ====================================================================== #
#  travel-time keys (shared-store data collection)                       #
# ====================================================================== #

def coord_key(wx, wy, wz):
    return "%d,%d,%d" % (wx, wy, wz)


def pair_key(k1, k2):
    return (k1 + "|" + k2) if k1 <= k2 else (k2 + "|" + k1)


def leg_key(k1, k2):
    return k1 + ">" + k2  # DIRECTED (order preserved) — unlike pair_key, which sorts to stay symmetric


class HudReader:
    """OCRs the F7 panel strip; remembers where the WORLD text sits to keep
    steady-state polls small and cheap."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.band = None      # (top, bottom) in strip coords
        self.misses = 0
        self.acquired = False # have we ever read a position? (until then, hunt the panel hard)

    def _strip(self, frame):
        w, h = frame.size
        return frame.crop((int(w * 0.62), 0, w, int(h * 0.62)))

    def read(self, frame, want_target=False, thorough=False):
        out = {"position": None, "yaw": None, "yaw_exact": True, "target": None,
               "speed": None, "velocity": None, "wishdir": None}
        strip = self._strip(frame)
        # candidate regions, cheapest first: the remembered band, then the whole top-right
        # strip, and — only after repeated misses — the ENTIRE frame, so the panel is still
        # found if a resolution / UI-scale / ultrawide layout puts it outside the usual strip.
        regions = []
        if self.band:
            top = max(0, int(self.band[0]) - 30)
            # when we also want the Target line extend the crop downward — proportional to the
            # line spacing so it reaches it at any resolution. Real panels run ~15 rows between
            # Orientation and the Target/Block line (State, Velocity, Collision…), so the growth
            # is generous; OCR cost stays modest because it's still a narrow strip.
            grow = (int((self.band[1] - self.band[0]) * 8) + 120) if want_target else 40
            bottom = min(strip.height, int(self.band[1]) + grow)
            if bottom - top > 40:
                regions.append((strip.crop((0, top, strip.width, bottom)), top, True))
        regions.append((strip, 0, True))
        # deep rescan of the WHOLE frame: after repeated misses, OR as soon as the FIRST strip
        # read fails before we've ever acquired a position — so at startup an off-strip panel
        # (moved panel, ultrawide, odd UI scale) is found on the SECOND poll instead of only
        # after 3 misses. Gated on misses>=1 so a normal top-right panel, which reads position
        # from the strip on poll 1, never adds this expensive region at all.
        if (self.misses >= 3 or (not self.acquired and self.misses >= 1)) and not self.band:
            regions.append((frame, 0, False))

        def have_pos():
            return out["position"] is not None and out["yaw"] is not None
        def have_all():
            # on the actual open (thorough) a nameless target isn't "done" — keep hunting the
            # nameplate in larger regions; a named one (or any target when just polling) is
            return have_pos() and (not want_target or
                                   (out["target"] is not None and
                                    (out["target"].get("block") is not None or not thorough)))

        texts = []
        strip_texts = []  # OCR text from panel-strip regions ONLY (never the full-frame rescan)
        last = len(regions) - 1
        for idx, (region, oy, is_strip) in enumerate(regions):
            band_lines = None
            region_label = False
            scales = ocr_scales(region.width)
            # Capture Doctor's measured-best upscale for the panel strip is tried FIRST (band
            # crops share the strip's width, so it applies to them too); the computed sweep
            # stays as the fallback, so a stale hint costs one extra attempt, never a miss.
            # The hint must also make sense AT THIS WIDTH: after a resolution change it could
            # otherwise drive the effective OCR width back under ~2400px — the regime where
            # digits drop MID-NUMBER (see ocr_scales) — or into absurd ~70MP upscales. Out of
            # band → silently unused until the wizard re-measures.
            hint = self.cfg.get("ocr_scale_hint")
            if (is_strip and isinstance(hint, int) and 2 <= hint <= 6
                    and 2400 <= region.width * hint <= 7500):
                scales = [hint] + [s for s in scales if s != hint]
            for scale in scales:
                lines = ocr_lines(region, scale)
                text = " ".join(t for t, _ in lines)
                texts.append(text)
                if is_strip:
                    strip_texts.append(text)
                if out["position"] is None:
                    out["position"] = parse_position(text)
                if out["yaw"] is None:
                    out["yaw"] = parse_yaw(text)
                if out["speed"] is None:
                    out["speed"] = parse_speed(text)
                if out["velocity"] is None:
                    out["velocity"] = parse_velocity(text)
                if out["wishdir"] is None:
                    out["wishdir"] = parse_wishdir(text)
                if want_target and (out["target"] is None or not out["target"].get("block")):
                    nt = parse_target_block(text)
                    # a NAMED parse upgrades a nameless one (4K panels print no in-panel name;
                    # the nameplate may only appear in a larger region's text)
                    if nt and (out["target"] is None or nt.get("block")):
                        out["target"] = nt
                if re.search(r"tar?ge?t|\bblock\b", text, re.I):
                    region_label = True
                if is_strip and band_lines is None:  # band is tracked in strip coordinates only
                    ys = [y + oy for t, y in lines if re.search(r"Position|Orientation", t, re.I)]
                    if ys:
                        band_lines = (min(ys), max(ys) + 20)
                if have_all():
                    break
                # position found and no "Target" label at all → not aiming at a block; stop scales
                if have_pos() and want_target and out["target"] is None and not region_label:
                    break
            if band_lines:
                self.band = band_lines
            if have_all():
                break
            if have_pos() and not want_target:
                break
            if have_pos() and want_target and (out["target"] is None or
                                               (thorough and not out["target"].get("block"))):
                # target still missing (or parsed without a name — the nameplate may sit outside
                # this crop). Escalate to the next, larger region when it may help: on the actual
                # open (thorough) always try the full strip; otherwise only when the Target label
                # wasn't even in this region.
                if idx < last and (thorough or not region_label):
                    continue
                break
            # position not yet found → keep escalating to the next region
        if out["yaw"] is None:  # digits mangled — the compass word is a coarse fallback
            for text in texts:
                cy = parse_yaw_compass(text)
                if cy is not None:
                    out["yaw"], out["yaw_exact"] = cy, False
                    break
        elif out["yaw_exact"]:
            # coherence check: a scrambled Orientation line can yield a wrong-but-parseable yaw
            # (e.g. roll read as yaw). The compass word covers ±45° around each cardinal, so a
            # >60° disagreement means the digits are junk — trust the compass, marked coarse
            # (never trains mouse sensitivity).
            for text in texts:
                cy = parse_yaw_compass(text)
                if cy is not None:
                    if abs(wrap_deg(out["yaw"] - cy)) > 60:
                        out["yaw"], out["yaw_exact"] = cy, False
                    break
        if out["position"] is None:
            self.misses += 1
            if self.misses >= 2:
                self.band = None  # panel moved — go back to scanning the full strip / frame
        else:
            self.misses = 0
            self.acquired = True  # first fix landed — steady-state polls can stay cheap
        out["texts"] = texts  # raw OCR attempts — for the failed-read diagnostics log
        out["strip_texts"] = strip_texts  # panel-region text only — safe for detection reports
        return out


# ====================================================================== #
#  Capture Doctor — one-time per-device setup probes                     #
# ====================================================================== #
# The wizard (App._open_setup_wizard) drives these to convert hardcoded capture
# assumptions into MEASURED per-device constants: which window is the game, which
# OCR upscale reads THIS resolution's panel best, whether each F7 signal parses,
# and how fast this player actually travels. Everything here is UI-free and takes
# injectable grab/clock hooks so it's testable without a screen.

SETUP_VERSION = 1                 # bump when the probes change enough to warrant a re-run offer
SETUP_SCALES = (2, 3, 4, 5, 6)    # swept wider than ocr_scales() so an odd DPI/GUI-scale combo
                                  # still finds its best factor


def list_game_windows():
    """Visible top-level windows big enough to be a game, biggest first (the game is almost
    always the largest surface). [(title, w, h)]; empty off-Windows (the wizard then falls back
    to title-only entry)."""
    if not IS_WIN:
        return []
    user32 = ctypes.windll.user32
    raw = []

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_cb(hwnd, _):
        try:
            if user32.IsWindowVisible(hwnd):
                t = _window_title(hwnd)
                if t:
                    rc = RECT()
                    if user32.GetClientRect(hwnd, ctypes.byref(rc)):
                        w, h = rc.r - rc.l, rc.b - rc.t
                        if w >= 600 and h >= 400:
                            raw.append((t, w, h))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(enum_cb, None)
    except Exception:
        return []
    raw.sort(key=lambda x: -(x[1] * x[2]))
    seen, out = set(), []
    for t, w, h in raw:
        if t not in seen:
            seen.add(t)
            out.append((t, w, h))
    return out


def score_strip_scales(strip, scales=SETUP_SCALES):
    """OCR the panel strip at each upscale and score how much of the F7 panel parses. Position
    DOMINATES the score (weight 6 > the 5 all other fields sum to): a scale that reads Position
    must always outrank one that only reads the looser motion lines, because everything depends
    on Position and its strict 2-3-decimal pattern fails first on a blurry upscale. Returns
    [(scale, score, fields)] best-first (ties → smaller scale = cheaper OCR)."""
    ranked = []
    w = getattr(strip, "width", 0)
    for sc in scales:
        if w and not (2400 <= w * sc <= 7500):
            # keep the wizard's candidate set identical to the runtime hint gate: above 7500
            # effective px is a ~70MP resize (never sharper, hugely slower); below 2400 is the
            # regime where digits drop MID-NUMBER — such a scale can "parse" a WRONG position,
            # win the score, and lock a hint the runtime gate would then permanently refuse.
            continue
        try:
            text = ocr_text(strip, sc)
        except Exception:
            text = ""
        f = {"position": parse_position(text) is not None,
             "yaw": parse_yaw(text) is not None,
             "speed": parse_speed(text) is not None,
             "velocity": parse_velocity(text) is not None,
             "wishdir": parse_wishdir(text) is not None}
        ranked.append((sc, 6 * f["position"] + 2 * f["yaw"] + f["speed"] + f["velocity"] + f["wishdir"], f))
    ranked.sort(key=lambda r: (-r[1], r[0]))
    return ranked


def probe_panel(cfg, samples=3, delay=0.35, grab=None, sleep=time.sleep):
    """Capture a few frames and rank OCR scales on the panel strip across them. A field counts
    as working in a frame if ANY scale parsed it (field health is about the capture, not one
    zoom). Returns {"frames", "size", "window", "best", "scores": {scale: total},
    "fields": {name: 0..1 hit-rate}}."""
    grab = grab or grab_game
    hud = HudReader(cfg)
    totals, pos_by = {}, {}
    hits = {k: 0 for k in ("position", "yaw", "speed", "velocity", "wishdir")}
    n_ok, size, win = 0, None, False
    for i in range(max(1, int(samples))):
        if i:
            sleep(delay)
        try:
            win = win or bool(find_game_window(cfg["window_title"]))
            frame = grab(cfg)
        except Exception:
            continue
        size = frame.size
        n_ok += 1
        ranked = score_strip_scales(hud._strip(frame))
        for sc, score, f in ranked:
            totals[sc] = totals.get(sc, 0) + score
            pos_by[sc] = pos_by.get(sc, 0) + (1 if f.get("position") else 0)
        for k in hits:
            if any(f.get(k) for _, _, f in ranked):
                hits[k] += 1
    # only a scale that actually read Position may become the hint — locking one that merely
    # parsed the looser motion lines would put a position-blind zoom first on every read.
    # Position RELIABILITY (frames hit) outranks the total score: a scale reading Position in
    # every frame beats one that read it once but padded its total with motion lines.
    cand = {sc: t for sc, t in totals.items() if pos_by.get(sc)}
    best = None
    if cand:
        best = sorted(cand.items(), key=lambda kv: (-pos_by[kv[0]], -kv[1], kv[0]))[0][0]
    return {"frames": n_ok, "size": size, "window": win, "best": best, "scores": totals,
            "fields": {k: (hits[k] / n_ok if n_ok else 0.0) for k in hits}}


def probe_movement(cfg, seconds=4.0, interval=0.7, grab=None, reader=None,
                   sleep=time.sleep, now=time.time):
    """While the player runs straight forward, sample OCR fixes and measure their pace.
    Robust by construction: the pace is the MEDIAN of per-segment speeds between consecutive
    fixes — so the stationary lead-in while the player alt-tabs into the game, a stop
    mid-test, a curved path, and a single corrupted coordinate (the classic mid-number OCR
    digit drop reads as ~100 blocks/s) all fall out of the estimate instead of skewing the
    endpoint chord. Fix timestamps are taken at FRAME-GRAB time, not after read() returns —
    the first cold read costs far more OCR time than later band reads, which would compress
    the measured span and inflate the pace. Yaw health is judged per moving segment, only
    against EXACT yaws that agree at both ends (a mid-test turn, or the coarse compass
    fallback quantized to 90°, must never fail a healthy device); the majority verdict wins.
    Returns {"reads", "hits", "speed_bps", "game_speed", "yaw_ok"}."""
    grab = grab or grab_game
    reader = reader or HudReader(cfg)
    t0 = now()
    fixes, speeds, reads = [], [], 0
    while now() - t0 < seconds:
        try:
            frame = grab(cfg)
        except Exception:
            sleep(interval)
            continue
        ts = now()          # the frame shows the world as of NOW; read() below takes a while
        reads += 1
        out = reader.read(frame)
        if out["position"] is not None:
            x, _, z = out["position"]
            fixes.append((ts, x, z, out["yaw"] if out.get("yaw_exact", True) else None))
        if out["speed"] is not None and 0.5 < out["speed"] <= 15:
            speeds.append(out["speed"])  # >0.5: stationary lead-in zeros must not drag the median
        sleep(interval)
    segs = []  # (blocks/s, travel heading, yaw@start, yaw@end) per usable segment
    for a, b in zip(fixes, fixes[1:]):
        dt = b[0] - a[0]
        if dt <= 0.2:
            continue
        dx, dz = b[1] - a[1], b[2] - a[2]
        sp = math.hypot(dx, dz) / dt
        if 0.5 < sp <= 20:  # moving, and plausible for a player on foot
            # travel heading in the same convention tick() uses (forward = (-sin yaw, -cos yaw))
            segs.append((sp, math.degrees(math.atan2(-dx, -dz)), a[3], b[3]))
    ss = sorted(s[0] for s in segs)
    speed_bps = ss[len(ss) // 2] if ss else None
    votes = []
    for sp, head, ya, yb in segs:
        if ya is None or yb is None or abs(wrap_deg(ya - yb)) > 20:
            continue        # turning mid-segment, or a coarse/missing yaw — not a fair test
        votes.append(abs(wrap_deg(head - yb)) < 30)
    yaw_ok = (votes.count(True) >= votes.count(False)) if votes else None
    game_speed = sorted(speeds)[len(speeds) // 2] if speeds else None
    return {"reads": reads, "hits": len(fixes), "speed_bps": speed_bps,
            "game_speed": game_speed, "yaw_ok": yaw_ok}


def apply_setup_results(cfg, panel=None, movement=None, completed=True):
    """Fold wizard probe results into the config (caller saves). Returns human-readable notes
    of what was tuned. Partial results are fine — each block applies independently; a skipped
    or closed-early wizard still marks setup_done so first-launch doesn't nag again (the
    Settings button re-runs it anytime)."""
    notes = []
    # merge INTO the stored baseline: a partial re-run (say, only re-timing the pace) must not
    # erase the panel block — the capture-regression nag keys off it, and ocr_scale_hint from
    # the earlier run stays in force either way
    old = cfg.get("setup_health")
    health = dict(old) if isinstance(old, dict) else {}
    health["at"] = int(time.time())
    health["skipped"] = not completed
    if panel and panel.get("frames"):
        health["panel"] = {"best": panel.get("best"), "fields": panel.get("fields"),
                           "size": list(panel["size"]) if panel.get("size") else None}
        if panel.get("best"):
            cfg["ocr_scale_hint"] = int(panel["best"])
            notes.append("OCR zoom locked to ×%d for your resolution" % panel["best"])
    if movement and movement.get("reads"):
        health["movement"] = {k: movement.get(k)
                              for k in ("reads", "hits", "speed_bps", "game_speed", "yaw_ok")}
        sp = movement.get("speed_bps")
        if sp and 2.0 <= sp < 7.0:
            cfg["move_speed"] = round(sp, 2)  # clearly a walking gait → seed the position tracker
            notes.append("walk speed measured: %.1f blocks/s" % sp)
    cfg["setup_done"] = SETUP_VERSION
    cfg["setup_health"] = health
    return notes


# ====================================================================== #
#  dead reckoning between OCR polls                                      #
# ====================================================================== #

class DeadReckoner:
    """Slim position tracker for the covered-panel logging fallback: WASD held keys coast the
    position estimate between OCR fixes along the last panel-read heading; every fix applies a
    latency-compensated correction and re-fits the walk speed. The heading is only ever what the
    F7 panel itself last reported — it is never steered or predicted."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.pos = None           # [x, y, z]
        self.yaw = None           # last panel-read facing (coasting direction only)
        self.speed = float(cfg["move_speed"])      # least-squares-learned fallback move speed
        self._ocr_speed = None    # ground speed the game just reported (walk vs sprint accurate)
        self._ocr_speed_t = 0.0   # when that speed was read (it goes stale between polls)
        self.pos_err = 0.0        # EMA of |prediction − OCR| per fix (blocks)
        self.last_fix = 0.0       # wall time of the last accepted OCR fix
        self._u = [0.0, 0.0]      # unit-motion integral since last fix (blocks @ speed 1)
        self._last_ocr = None     # previous OCR position (true displacement source)
        self._last_sync_t = 0
        self._jump_pending = None  # far-off fix awaiting confirmation by a second consistent read

    def snapshot(self):
        """Estimate at frame-capture time — lets sync() ignore the movement
        that happens while OCR is still chewing on the frame."""
        return {"pos": list(self.pos) if self.pos else None, "yaw": self.yaw}

    def move_speed(self):
        """The speed to advance the estimate at right now: prefer the ground speed the game itself
        reported at the last poll (correct for BOTH walk and sprint), else the least-squares-learned
        move_speed. The reported speed goes stale between polls, so it's only used briefly and only
        when it clearly indicates motion — a stale/zero reading falls back to the learned constant."""
        if (self._ocr_speed is not None and self._ocr_speed > 0.5
                and (time.time() - self._ocr_speed_t) < 2.5):
            return self._ocr_speed
        return self.speed

    def tick(self, dt, held):
        """Advance the estimate: WASD moves along the last panel-read heading."""
        if self.pos is not None and self.yaw is not None:
            f = (1 if "w" in held else 0) - (1 if "s" in held else 0)
            s = (1 if "d" in held else 0) - (1 if "a" in held else 0)
            if f or s:
                r = math.radians(self.yaw)
                fx, fz = -math.sin(r), -math.cos(r)   # forward
                rx, rz = math.cos(r), -math.sin(r)    # right (strafe)
                vx, vz = f * fx + s * rx, f * fz + s * rz
                n = math.hypot(vx, vz)
                if n > 0:
                    ux, uz = vx / n, vz / n
                    self._u[0] += ux * dt              # direction integral (speed-free)
                    self._u[1] += uz * dt
                    spd = self.move_speed()
                    self.pos[0] += ux * spd * dt
                    self.pos[2] += uz * spd * dt

    def sync(self, pos, yaw, at=None, yaw_exact=True, speed=None):
        """Correct with an OCR fix. `at` is the snapshot() taken when the frame
        was captured; the correction is computed against it and applied to the
        CURRENT estimate, so movement during OCR isn't wiped out. `yaw` is
        stored as-read (a coasting direction, never steered or predicted).
        `speed` is the game-reported ground speed this frame (used for the next
        few ticks so sprinting doesn't outrun the estimate); None leaves the
        learned move_speed in charge."""
        now = time.time()
        gap = now - self._last_sync_t
        changed = False
        if speed is not None and 0 <= speed <= 15:  # plausible ground speed; garbage is ignored
            self._ocr_speed = speed
            self._ocr_speed_t = now
        if yaw is not None:
            self.yaw = yaw
        if pos is not None:
            p = list(pos)
            # A fix that JUMPS far from the current estimate is suspect: a correlated OCR digit
            # misread shifts the Position and Target lines together, which would both poison the
            # estimate and defeat the chest sanity gate. Hold ONE far fix back; if the NEXT fix
            # is also far from the estimate, the ESTIMATE is wrong (teleport, drift), not the
            # OCR — adopt the newest fix. A one-frame misread never lands, and tracking always
            # recovers within two polls, even while the player keeps moving (fix-to-fix distance
            # while running is several blocks, so requiring the two far fixes to agree with each
            # other would deadlock — position frozen, every chest read rejected as 'too far').
            if self.pos is not None and math.hypot(p[0] - self.pos[0], p[1] - self.pos[1],
                                                   p[2] - self.pos[2]) > JUMP_CONFIRM_DIST:
                first_far = self._jump_pending is None
                self._jump_pending = p
                if first_far:
                    return changed  # held back — a real jump confirms on the very next poll
                self.pos = p        # second consecutive far fix: trust the OCR over the estimate
                self._jump_pending = None
                self._last_ocr = None  # displacement across a jump must not train speed
                self._last_sync_t = now
                self.last_fix = now
                return changed
            self._jump_pending = None
            ref = at["pos"] if at and at.get("pos") else self.pos
            if ref is not None and self.pos is not None:
                cx, cz = p[0] - ref[0], p[2] - ref[2]
                self.pos_err = 0.7 * self.pos_err + 0.3 * math.hypot(cx, cz)
                self.pos = [self.pos[0] + cx, p[1], self.pos[2] + cz]
            else:
                self.pos = p
            # speed learning: least-squares fit of true displacement onto the
            # direction integral — works for any WASD mix, not just pure W
            un2 = self._u[0] ** 2 + self._u[1] ** 2
            if self._last_ocr is not None and 0.5 < gap < 6 and un2 > 0.6:
                dxp = p[0] - self._last_ocr[0]
                dzp = p[2] - self._last_ocr[2]
                est = (dxp * self._u[0] + dzp * self._u[1]) / un2
                if 1.0 < est < 15.0:
                    self.speed = 0.7 * self.speed + 0.3 * est
                    self.cfg["move_speed"] = round(self.speed, 2)
                    changed = True
            self._last_ocr = p
        self._u = [0.0, 0.0]
        self._last_sync_t = now
        self.last_fix = now
        return changed

    def poll_interval(self, base):
        """Error-adaptive cadence: poll harder while predictions are off,
        relax when tracking is tight."""
        if self.pos_err > 4.0:
            return max(0.9, base * 0.6)
        if self.pos_err < 1.5:
            return min(3.0, base * 1.4)
        return base

    def stale(self, base):
        """No usable fix for a while — the estimate shouldn't be trusted."""
        return self.last_fix == 0 or (time.time() - self.last_fix) > max(6.0, base * 3)


# ====================================================================== #
#  shared-map client                                                     #
# ====================================================================== #

def ign_slug(ign):
    s = re.sub(r"[^\w-]+", "-", ign.strip().lower()).strip("-")
    return (s or "player")[:24]


class MapClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = cfg["api_base"]
        self.dry = bool(cfg["dry_run"]) or bool(cfg.get("_dry_cli"))
        self.calibration = None
        self.entries = {}
        self.auth_required = False   # legacy flag (identity model: always effectively true)
        self.key_valid = None        # is our key accepted? (from GET; None = server didn't say)
        self.me = None               # {ign, role, uuid} the server bound to our key, or None
        self.reset_override = cfg.get("hud_reset_epoch") or None  # HUD-observed next reset (epoch)
        self.current_area = None     # area name per the game HUD (best effort; advisory stamp)
        self.opens = {}          # this IGN's merged open log: "x,y,z" -> {t, r?}
        self._travel_pending = {}   # pair_key -> observed seconds (pre-flush minima)
        self._leg_pending = {}      # leg_key (DIRECTED) -> [min_secs, count] observed this flush cycle
        self._travel_last_flush = 0
        self._pending_deletes = {}  # id -> soft-deleted marker awaiting a retried POST
        self._pending_runs = []     # finished runs awaiting a retried leaderboard POST
        self.lock = threading.Lock()

    def has_key(self):
        return bool(str(self.cfg.get("write_key", "")).strip())

    def signed_in(self):
        """True once the server has confirmed our key belongs to a verified account (`me`)."""
        return bool(self.me)

    def can_edit(self):
        """True if this client may edit MAP STRUCTURE — i.e. the signed-in account is an editor or
        the owner. (Contributing opens/runs only needs to be signed in, not an editor.)"""
        role = (self.me or {}).get("role")
        return role in ("editor", "owner")

    def _req(self, method, url, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        key = str(self.cfg.get("write_key", "")).strip()
        if key:
            # personal keys (from the website sign-in) go in x-player-key; a legacy/master editor
            # key goes in x-write-key. Sending under the right header lets the server resolve identity.
            headers["x-player-key" if key.startswith("hd_") else "x-write-key"] = key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = (json.loads(e.read().decode() or "{}") or {}).get("error", "")
            except Exception:
                pass
            if e.code == 403:
                raise RuntimeError("editor key rejected by the server — check it in " + chr(0x2699) + " Settings")
            raise RuntimeError(msg or ("server error %d" % e.code))

    def refresh(self):
        data = self._req("GET", self.api)
        entries = data.get("entries") or {}
        self.auth_required = bool(data.get("authRequired"))
        self.key_valid = data.get("keyValid") if "keyValid" in data else None
        # the server tells us who our key belongs to (verified in-game name + role). Adopt that IGN
        # as authoritative — opens/runs are attributed by the SIGNED-IN identity, so a mismatched
        # local IGN would be rejected; using me.ign keeps the client honest and correct.
        me = data.get("me") if isinstance(data.get("me"), dict) else None
        self.me = me
        if me and me.get("ign") and me["ign"] != self.cfg.get("ign"):
            self.cfg["ign"] = me["ign"]
        with self.lock:
            self.entries = entries
            # keep local soft-deletes that haven't reached the server yet, so a
            # "not here" chest doesn't reappear on refresh
            for mid, dm in self._pending_deletes.items():
                if mid in self.entries:
                    self.entries[mid] = dm
            self.calibration = entries.get("calibration")
            if self.calibration and self.calibration.get("type") != "calibration":
                self.calibration = None
            server = entries.get("opens-" + ign_slug(self.cfg["ign"])) if self.cfg["ign"] else None
            # slugs can collide across different raw IGNs — only merge our own log
            if server and isinstance(server.get("opens"), dict) and \
               str(server.get("ign", "")).strip().lower() == self.cfg["ign"].strip().lower():
                merged = dict(server["opens"])
                for k, v in self.opens.items():  # local wins when newer
                    if k not in merged or v.get("t", 0) > merged[k].get("t", 0):
                        merged[k] = v
                self.opens = merged

    def world_to_map(self, wx, wz):
        c = self.calibration
        if not c:
            return None
        return (c["ax"] * wx + c["bx"], c["az"] * wz + c["bz"])

    def chests(self):
        with self.lock:
            return [e for e in self.entries.values()
                    if isinstance(e, dict) and e.get("type") == "marker"
                    and e.get("kind") == "chest" and not e.get("deleted")
                    and e.get("gx") is not None and e.get("gz") is not None]

    def route_markers(self):
        """Route STOPS: individual chests AND group pins (a group is one dense stop worth its
        whole pack). Deliberately separate from chests() — cooldown counts, area tallies and
        cooldown stats all want individual chests only. A group placed straight onto the map with
        no world coords is skipped (an open can never be matched to it)."""
        with self.lock:
            return [e for e in self.entries.values()
                    if isinstance(e, dict) and e.get("type") == "marker"
                    and e.get("kind") in ("chest", "group") and not e.get("deleted")
                    and e.get("gx") is not None and e.get("gz") is not None]

    def routes(self):
        with self.lock:
            rs = [e for e in self.entries.values()
                  if isinstance(e, dict) and e.get("type") == "route"]
        return sorted(rs, key=lambda r: str(r.get("name", "")).lower())

    def marker_at(self, wx, wy, wz):
        for e in self.chests():
            if round(e["gx"]) == wx and round(e["gz"]) == wz:
                gy = e.get("gy")
                if gy is None or round(gy) == wy:
                    return e
        return None

    # -- pending requests (open submission, editor verification). Two kinds: a location
    #    proposal ("unmapped chest here", the default) and a removal report (kind="remove",
    #    "this mapped chest doesn't exist") --
    def pendings(self):
        with self.lock:
            return [e for e in self.entries.values()
                    if isinstance(e, dict) and e.get("type") == "pending"
                    and e.get("gx") is not None and e.get("gz") is not None]

    def pending_at(self, wx, wy, wz, radius=2):
        """Nearest pending LOCATION proposal at this spot (removal/zone flags excluded)."""
        for e in self.pendings():
            if e.get("kind"):
                continue
            if abs(round(e["gx"]) - wx) <= radius and abs(round(e["gz"]) - wz) <= radius:
                return e
        return None

    def removal_at(self, wx, wy, wz, radius=2):
        """Pending REMOVAL report covering this spot, if any."""
        for e in self.pendings():
            if e.get("kind") != "remove":
                continue
            if abs(round(e["gx"]) - wx) <= radius and abs(round(e["gz"]) - wz) <= radius:
                return e
        return None

    def submit_pending(self, wx, wy, wz):
        """Anyone can propose a chest location; editors confirm it later. Skips if
        a real chest or another pending already covers this spot."""
        if self.marker_at(wx, wy, wz) or self.pending_at(wx, wy, wz):
            return None  # already mapped or already proposed
        pos = self.world_to_map(wx, wz)
        if pos is None:
            raise RuntimeError("map not calibrated — an editor must run 🎯 Calibrate first")
        u, v = pos
        # far outside the calibrated map = a misread coordinate (or an unmappable area): REJECT
        # rather than clamp — a clamped pending pinned to the map edge is wrong data that an
        # editor might confirm. (The tiny 2% overshoot tolerance matches add_chest.)
        if not (-0.02 <= u <= 1.02 and -0.02 <= v <= 1.02):
            raise RuntimeError("coords land outside the map — misread or uncalibrated area; not submitted")
        # id is derived from the block coordinate to match the server (which re-derives it),
        # so repeated reports of the same spot collapse into one pending entry.
        rx, ry, rz = int(round(wx)), int(round(wy)), int(round(wz))
        p = {"id": "pend-{}_{}_{}".format(rx, ry, rz),
             "type": "pending", "gx": wx, "gy": wy, "gz": wz,
             "x": min(1.0, max(0.0, u)), "y": min(1.0, max(0.0, v)),
             "by": self.cfg["ign"].strip(), "note": ""}
        if self.current_area:
            p["area"] = self.current_area
        if not self.dry:
            self._req("POST", self.api, p)
        with self.lock:
            self.entries[p["id"]] = p
        return p

    def submit_gone(self, wx, wy, wz):
        """Anyone can report a mapped chest as missing; the marker STAYS on the map until an
        editor verifies. Returns the request, or None when it's already been reported."""
        if self.removal_at(wx, wy, wz):
            return None  # already reported
        pos = self.world_to_map(wx, wz)
        if pos is None:
            raise RuntimeError("map not calibrated — an editor must run 🎯 Calibrate first")
        u, v = pos
        rx, ry, rz = int(round(wx)), int(round(wy)), int(round(wz))
        p = {"id": "pend-rm-{}_{}_{}".format(rx, ry, rz),
             "type": "pending", "kind": "remove", "gx": wx, "gy": wy, "gz": wz,
             "x": min(1.0, max(0.0, u)), "y": min(1.0, max(0.0, v)),
             "by": self.cfg["ign"].strip(), "note": ""}
        if self.current_area:
            p["area"] = self.current_area
        if not self.dry:
            self._req("POST", self.api, p)
        with self.lock:
            self.entries[p["id"]] = p
        return p

    def zone_flag_at(self, wx, wy, wz, radius=2):
        """Existing zone-review flag covering this spot, if any."""
        for e in self.pendings():
            if e.get("kind") != "zone":
                continue
            if abs(round(e["gx"]) - wx) <= radius and abs(round(e["gz"]) - wz) <= radius:
                return e
        return None

    def submit_zone_flag(self, wx, wy, wz, hud_area, mapped_area):
        """Anyone can flag a chest whose in-game area disagrees with the map's boundary; the
        marker itself is untouched — editors adjust the polygon and resolve the flag."""
        if self.zone_flag_at(wx, wy, wz):
            return None  # already flagged
        pos = self.world_to_map(wx, wz)
        if pos is None:
            return None
        rx, ry, rz = int(round(wx)), int(round(wy)), int(round(wz))
        p = {"id": "pend-zn-{}_{}_{}".format(rx, ry, rz),
             "type": "pending", "kind": "zone", "gx": wx, "gy": wy, "gz": wz,
             "x": min(1.0, max(0.0, pos[0])), "y": min(1.0, max(0.0, pos[1])),
             "by": self.cfg["ign"].strip(), "area": hud_area,
             "note": "opened in %s, but mapped inside %s" % (hud_area, mapped_area)}
        if not self.dry:
            self._req("POST", self.api, p)
        with self.lock:
            self.entries[p["id"]] = p
        return p

    def confirm_pending(self, pending):
        """Editor action: turn a pending request into a real chest marker. The SUBMITTER gets
        the find credit — they located it; the editor only verified."""
        wx = int(round(pending["gx"])); wy = int(round(pending["gy"])); wz = int(round(pending["gz"]))
        marker = self.marker_at(wx, wy, wz) or self.add_chest(wx, wy, wz, found_by=pending.get("by"))
        self.reject_pending(pending)  # remove the (and any duplicate) request(s)
        self.credit(pending.get("by"), found=1)
        return marker

    def reject_pending(self, pending):
        """Editor action: drop a pending request (and any SAME-KIND duplicate at that spot —
        a removal report must not silently swallow a location proposal nearby, or vice versa)."""
        wx, wz = int(round(pending["gx"])), int(round(pending["gz"]))
        kind = pending.get("kind")
        for e in self.pendings():
            if e.get("kind") != kind:
                continue
            if abs(round(e["gx"]) - wx) <= 2 and abs(round(e["gz"]) - wz) <= 2:
                if not self.dry:
                    try:
                        self._req("DELETE", self.api + "?id=" + e["id"])
                    except Exception:
                        pass
                with self.lock:
                    self.entries.pop(e["id"], None)

    def marker_by_id(self, mid):
        with self.lock:
            e = self.entries.get(mid)
        return e if isinstance(e, dict) and e.get("type") == "marker" else None

    # -- learned travel times --
    @staticmethod
    def _marker_key(m):
        if m is None or m.get("gx") is None or m.get("gz") is None:
            return None
        return coord_key(int(round(m["gx"])), int(round(m.get("gy") or 0)), int(round(m["gz"])))

    def observe_travel(self, a, b, secs):
        """Queue a player-run leg time (coords tuples): the symmetric `pairs` keeps the min, and the
        DIRECTED `legs` store keeps the per-direction min plus a sample count (so A->B and B->A can
        diverge, and repeated runs build confidence / refresh recency)."""
        if not (1 <= secs <= 3600) or tuple(a) == tuple(b):
            return
        ka, kb = coord_key(*a), coord_key(*b)
        k = pair_key(ka, kb)
        dk = leg_key(ka, kb)  # DIRECTED — a->b kept apart from b->a
        with self.lock:
            cur = self._travel_pending.get(k)
            if cur is None or secs < cur:
                self._travel_pending[k] = secs
            lp = self._leg_pending.get(dk)
            if lp is None:
                self._leg_pending[dk] = [secs, 1]
            else:
                lp[0] = min(lp[0], secs)
                lp[1] += 1

    def flush_travel(self, force=False):
        """Merge pending observations into the shared traveltimes entry: min-wins on the symmetric
        `pairs`; for the directed `legs` we POST our CUMULATIVE view (the refreshed stored n plus
        this cycle's new samples) and the server merges min-t / MAX-n / max-at — all idempotent, so
        a retried or re-posted snapshot converges instead of re-adding counts. (Truly concurrent
        writers can under-count n by an observation — the safe direction for the trust gate. A lost
        response followed by the re-queue below can over-count one cycle's samples once — bounded,
        rare, and far better than dropping them.)"""
        now = time.time()
        with self.lock:
            if not self._travel_pending and not self._leg_pending:
                return
            if not force and now - self._travel_last_flush < 60:
                return
            pending, self._travel_pending = self._travel_pending, {}
            leg_pending, self._leg_pending = self._leg_pending, {}
            self._travel_last_flush = now
            e = self.entries.get("traveltimes")
            merged = dict(e["pairs"]) if isinstance(e, dict) and isinstance(e.get("pairs"), dict) else {}
            merged_legs = dict(e["legs"]) if isinstance(e, dict) and isinstance(e.get("legs"), dict) else {}
        changed = False
        for k, v in pending.items():
            vv = max(1, int(round(v)))
            if k not in merged or vv < merged[k]:
                merged[k] = vv
                changed = True
        now_i = int(now)
        for dk, (secs, cnt) in leg_pending.items():
            tv = max(1, int(round(secs)))
            rec = merged_legs.get(dk)
            if isinstance(rec, dict):
                merged_legs[dk] = {"t": min(int(rec.get("t") or tv), tv),
                                   "n": min(LEG_CAP_N, int(rec.get("n") or 0) + cnt),
                                   "at": now_i}
            else:
                merged_legs[dk] = {"t": tv, "n": min(LEG_CAP_N, cnt), "at": now_i}
            changed = True  # even a repeat refreshes recency/confidence — worth publishing
        if not changed:
            return
        if len(merged) > 3900:
            # Keep the most SURPRISING legs (learned time vs the distance/speed
            # fallback): a chest pair 2 blocks apart that takes 300s is a wall
            # the generator would badly misprice, while short legs already match
            # the fallback and are the safe ones to drop.
            speed = 10.0  # nominal blocks/sec for the surprise ranking (was cfg travel_speed)

            def surprise(item):
                k, v = item
                a, b = k.split("|")
                ax, ay, az = (int(n) for n in a.split(","))
                bx, by, bz = (int(n) for n in b.split(","))
                est = (math.hypot(ax - bx, az - bz) + abs(ay - by)) / speed
                return v / max(est, 0.1)
            merged = dict(sorted(merged.items(), key=surprise, reverse=True)[:3900])
        if len(merged_legs) > 3900:
            # directed legs are freshness-first: drop the STALEST (oldest `at`) — they've decayed to
            # the symmetric fallback anyway, so losing them costs no accuracy, just re-measurement.
            merged_legs = dict(sorted(merged_legs.items(),
                                      key=lambda kv: kv[1].get("at", 0), reverse=True)[:3900])
        try:
            if not self.dry:
                self._req("POST", self.api, {"id": "traveltimes", "type": "travel",
                                             "pairs": merged, "legs": merged_legs})
        except Exception:
            with self.lock:  # re-queue so a network blip doesn't lose observations
                for k, v in pending.items():
                    cur = self._travel_pending.get(k)
                    if cur is None or v < cur:
                        self._travel_pending[k] = v
                for dk, (secs, cnt) in leg_pending.items():
                    lp = self._leg_pending.get(dk)
                    if lp is None:
                        self._leg_pending[dk] = [secs, cnt]
                    else:
                        lp[0] = min(lp[0], secs)
                        lp[1] += cnt
            raise
        with self.lock:
            self.entries["traveltimes"] = {"id": "traveltimes", "type": "travel",
                                           "pairs": merged, "legs": merged_legs}

    # -- per-player cooldown: chests relock on open and ALL reset together at the daily
    #    reset — the instant OBSERVED in the game HUD when available, else the configured
    #    Eastern wall-clock hour. Not a rolling per-chest timer. --
    def reset_cut_ms(self):
        cut = self._scheduled_cut_ms()
        # a MANUAL reset (the random in-game unlock event) is just a more-recent cut: honour it
        # until the next scheduled reset overtakes it, so everything opened before the press reads up.
        manual = self.cfg.get("manual_reset_ms")
        if isinstance(manual, (int, float)) and manual > cut:
            return float(manual)
        return cut

    def _scheduled_cut_ms(self):
        ov = self.reset_override  # epoch secs of a HUD-observed upcoming reset
        now = time.time()
        if ov:
            h = reset_hour_from_epoch(ov)
            if h is not None:
                # the observation pins the wall-clock hour — DST-correct model from here on
                return last_daily_reset(hour_et=h) * 1000.0
            if now < ov:
                return (ov - 86400) * 1000.0   # off-hour server: ~24h cycle around the instant
            if now < ov + 86400:
                return ov * 1000.0             # that reset has now happened
            # observation too stale (app closed across days) — fall back to the model
        return last_daily_reset(hour_et=int(self.cfg.get("reset_hour_et", 20))) * 1000.0

    def mark_all_reset(self):
        """Record a manual 'all my chest cooldowns are up again' event (the random in-game unlock),
        so every chest opened before now reads as available. Persists via the cfg (shared with the
        app), and is naturally superseded by the next scheduled daily reset."""
        self.cfg["manual_reset_ms"] = int(time.time() * 1000)

    def next_reset_epoch(self):
        ov = self.reset_override
        now = time.time()
        if ov:
            h = reset_hour_from_epoch(ov)
            if h is not None:
                return next_daily_reset(hour_et=h)
            if now < ov:
                return ov
            if now < ov + 86400:
                return ov + 86400
        return next_daily_reset(hour_et=int(self.cfg.get("reset_hour_et", 20)))

    def on_cooldown(self, wx, wy, wz, y_known=True):
        cut = self.reset_cut_ms()  # opened at/after the last reset = locked until the next
        if y_known:
            v = self.opens.get("%d,%d,%d" % (wx, wy, wz))
            return bool(v) and v["t"] >= cut
        best = None  # marker saved without a Y — match opens on x/z, newest wins
        for k, v in list(self.opens.items()):
            p = k.split(",")
            if int(p[0]) == wx and int(p[2]) == wz and (best is None or v["t"] > best):
                best = v["t"]
        return best is not None and best >= cut

    def chest_on_cooldown(self, m):
        """Cooldown for a whole marker by its block coord (chest OR group pin). Matched on the exact
        (x, z): a small proximity radius was tried for groups but marks a pack looted when any
        UNRELATED adjacent chest is opened nearby, which is worse than the reverse. Within a run, a
        looted group is instead removed by node-id (run_done) via the 2-block open near-match."""
        gx, gy, gz = m.get("gx"), m.get("gy"), m.get("gz")
        if gx is None or gz is None:
            return False
        return self.on_cooldown(int(round(gx)), int(round(gy or 0)), int(round(gz)), gy is not None)

    def record_open(self, wx, wy, wz, route_id=None):
        """Returns True when the open was tracked (IGN set), False otherwise."""
        if not self.cfg["ign"].strip():
            return False
        entry = {"t": int(time.time() * 1000)}
        if route_id:
            entry["r"] = route_id
        with self.lock:  # refresh() iterates/rebinds opens from another thread
            self.opens["%d,%d,%d" % (wx, wy, wz)] = entry
            if len(self.opens) > 550:  # keep the shared entry bounded
                for k in sorted(self.opens, key=lambda k: self.opens[k]["t"])[:len(self.opens) - 550]:
                    del self.opens[k]
            snapshot = dict(self.opens)
        if self.dry:
            return True
        slug = ign_slug(self.cfg["ign"])
        self._req("POST", self.api, {"id": "opens-" + slug, "type": "opens",
                                     "ign": self.cfg["ign"].strip(), "opens": snapshot})
        return True

    # -- writes --
    def add_chest(self, wx, wy, wz, found_by=None):
        pos = self.world_to_map(wx, wz)
        if pos is None:
            raise RuntimeError("map not calibrated — open the web map and press 🎯 Calibrate")
        u, v = pos
        if not (-0.02 <= u <= 1.02 and -0.02 <= v <= 1.02):
            raise RuntimeError("coords land outside the map — check calibration")
        marker = {"id": "cap" + format(int(time.time() * 1000), "x") + format(os.getpid() % 999, "03d"),
                  "type": "marker", "kind": "chest",
                  "x": min(1.0, max(0.0, u)), "y": min(1.0, max(0.0, v)),
                  "gx": wx, "gy": wy, "gz": wz, "name": "", "note": "", "diff": 1}
        # keep an editor-set route difficulty across a rediscovery: inherit it from the MOST RECENT
        # prior marker at this block (even a soft-deleted one in the recycle bin), so re-logging
        # never resets it — and when several priors exist, the newest editor value wins.
        with self.lock:
            best_diff, best_t = None, -1.0
            for e in self.entries.values():
                if (isinstance(e, dict) and e.get("type") == "marker" and e.get("kind") == "chest"
                        and e.get("gx") is not None and e.get("diff") is not None
                        and int(round(e["gx"])) == wx and int(round(e.get("gz") or 0)) == wz
                        and (e.get("gy") is None or int(round(e["gy"])) == wy)):
                    t = float(e.get("updatedAt") or 0)
                    if t >= best_t:
                        best_diff, best_t = e["diff"], t
            if best_diff is not None:
                marker["diff"] = best_diff
        # provenance for the contributor leaderboard: who located this chest
        fb = str(found_by if found_by is not None else self.cfg.get("ign", "")).strip()
        if fb:
            marker["foundBy"] = fb
        if self.current_area:  # advisory HUD stamp — the map's polygons are authoritative
            marker["area"] = self.current_area
        if not self.dry:
            self._req("POST", self.api, marker)
        with self.lock:
            self.entries[marker["id"]] = marker
        return marker

    def credit(self, ign, found=0, removed=0):
        """Editor-side contributor tally: +found/+removed for `ign` in the shared 'contrib'
        singleton (the leaderboard of confirmed finds/removals). Crediting happens only at
        confirmation time — an editor action — so it rides the editor key. Best effort: a
        failed tally never blocks the action being credited."""
        ign = str(ign or "").strip()
        slug = ign_slug(ign)
        if not ign or not slug or (not found and not removed):
            return
        try:
            with self.lock:
                e = self.entries.get("contrib") if isinstance(self.entries.get("contrib"), dict) else {}
                by = dict(e.get("by")) if isinstance(e.get("by"), dict) else {}
                cur = by.get(slug) or {}
                by[slug] = {"ign": cur.get("ign") or ign,
                            "found": int(cur.get("found") or 0) + found,
                            "removed": int(cur.get("removed") or 0) + removed}
                entry = {"id": "contrib", "type": "contrib", "by": by}
                self.entries["contrib"] = entry
            if not self.dry:
                self._req("POST", self.api, entry)
        except Exception:
            pass  # the tally is best-effort

    def publish_route(self, name, nodes, leg_times, author):
        route = {"id": "run" + format(int(time.time() * 1000), "x"),
                 "type": "route", "name": name, "nodes": nodes,
                 "legTimes": leg_times, "totalTime": sum(t for t in leg_times if t) or None,
                 "author": author, "note": "recorded in game"}
        if not self.dry:
            self._req("POST", self.api, route)
        with self.lock:
            self.entries[route["id"]] = route
        return route

    def delete(self, marker_id):
        if not self.dry:
            self._req("DELETE", self.api + "?id=" + marker_id)
        with self.lock:
            self.entries.pop(marker_id, None)

    def _apply_run(self, run):
        """Merge one finished run into the runs singleton against the CURRENT
        local state (so a retry re-merges against whatever refresh has pulled
        in, never clobbering a newer server best). Returns (payload, is_pr).
        Caller must hold self.lock."""
        e = self.entries.get("runs") if isinstance(self.entries.get("runs"), dict) else {}
        best = dict(e["best"]) if isinstance(e.get("best"), dict) else {}
        recent = list(e["recent"]) if isinstance(e.get("recent"), list) else []
        rec = {"ign": run["ign"], "t": run["t"], "c": run["c"], "at": run["at"]}
        key = run["route"] + "|" + ign_slug(run["ign"])
        is_pr = key not in best or run["t"] < best[key]["t"]
        if is_pr:
            best[key] = dict(rec)
        entry = dict(rec); entry["r"] = run["route"]
        top = recent[0] if recent else None
        dup = top and top.get("at") == run["at"] and top.get("r") == run["route"] \
            and top.get("ign") == run["ign"] and top.get("t") == run["t"]
        if not dup:  # avoid re-inserting the exact same run when it's retried
            recent.insert(0, entry)
        recent = recent[:300]
        if len(best) > 3900:  # keep the fastest per key; drop the slowest overall
            best = dict(sorted(best.items(), key=lambda kv: kv[1]["t"])[:3900])
        return {"id": "runs", "type": "runs", "best": best, "recent": recent}, is_pr

    def record_run(self, route_id, secs, chests):
        """Log a finished run into the shared leaderboard (best time per route,
        min wins, plus a recent feed). Local state is authoritative and a failed
        POST is retried in the background, so a genuine record is never lost to a
        transient blip. Returns True if it's a new personal record."""
        ign = self.cfg["ign"].strip()
        if not ign or not route_id or route_id in LOCAL_ROUTE_IDS or not (1 <= secs <= 1e7):
            return False
        run = {"route": route_id, "ign": ign, "t": int(round(secs)),
               "c": int(chests), "at": int(time.time() * 1000)}
        with self.lock:
            payload, is_pr = self._apply_run(run)
            self.entries["runs"] = payload  # local-first, so the PR survives a POST failure
        if not self.dry:
            try:
                self._req("POST", self.api, payload)
            except Exception:
                with self.lock:
                    self._pending_runs.append(run)  # retried by retry_runs()
        return is_pr

    def retry_runs(self):
        with self.lock:
            pend = list(self._pending_runs)
            self._pending_runs = []
        for run in pend:
            try:
                with self.lock:
                    payload, _ = self._apply_run(run)  # re-merge against fresh state
                    self.entries["runs"] = payload
                if not self.dry:
                    self._req("POST", self.api, payload)
            except Exception:
                with self.lock:
                    self._pending_runs.append(run)  # try again next cycle

    def soft_delete(self, marker):
        """Mark a marker as not-existing (recoverable from the web recycle bin
        for 7 days). The local mutation is authoritative and the POST is
        retried in the background, so a network blip can't resurrect the chest."""
        m = dict(marker)
        m["deleted"] = int(time.time() * 1000)
        with self.lock:
            self.entries[m["id"]] = m
        if not self.dry:
            try:
                self._req("POST", self.api, m)
            except Exception:
                with self.lock:
                    self._pending_deletes[m["id"]] = m  # retried by retry_deletes()
        return m

    def retry_deletes(self):
        with self.lock:
            pend = dict(self._pending_deletes)
        for mid, m in pend.items():
            try:
                if not self.dry:
                    self._req("POST", self.api, m)
                with self.lock:
                    self._pending_deletes.pop(mid, None)
            except Exception:
                pass  # try again next cycle


# ====================================================================== #
#  beeps                                                                 #
# ====================================================================== #

def beep(kind):
    if not winsound:
        return
    try:
        if kind == "ok":
            winsound.Beep(880, 120)
        elif kind == "dup":
            winsound.Beep(500, 90); winsound.Beep(500, 90)
        elif kind == "next":
            winsound.Beep(700, 80); winsound.Beep(900, 80)
        else:
            winsound.Beep(220, 250)
    except RuntimeError:
        pass


# ====================================================================== #
#  the app                                                               #
# ====================================================================== #

BG, SURFACE, FIELD, RAISED = "#0f1116", "#181b22", "#232834", "#2d3442"
BORDER = "#272c37"
FG, FG2, FG3 = "#eceef4", "#969db0", "#5b6273"
ACCENT, ACCENT_FG = "#46c98b", "#08130d"
GOOD, WARN, BAD = "#5fd39a", "#e6b862", "#ec6d6d"
UIFONT = "Segoe UI"


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = MapClient(cfg)
        self.jobs = queue.Queue()
        self.mode = "idle"            # idle | log | record | run | verify
        self.count = 0
        self.undo_stack = []
        self.record_nodes = []        # [{id, wx, wy, wz, t}]
        self.record_name = ""         # name chosen when the recording was started (optional)
        self.run_route = None
        self.run_done = set()         # node IDs of collected route stops (id-keyed so live re-opt can reorder)
        self.run_started = 0
        self.run_opened = 0           # chests opened during the current run
        self.session_opens = 0        # chests opened since the app started
        self.session_legs = 0         # inter-chest legs whose time we've captured this session
        self.session_start = time.time()  # anchor for session duration + chests/hour
        self.session_runs = 0         # routes finished this session
        self.session_best_run = None  # fastest finished route this session (seconds)
        self.run_paused = False       # run is paused (timer frozen)
        self.pause_reason = None      # None | "manual" | "screen" | "focus" | "still" — why it's paused
        self._last_moved = 0.0        # last time the player was moving (for the stand-still auto-pause)
        self._autopause_t = 0.0       # throttle for the auto-pause check
        self._pause_banked_to = 0.0   # end of the last banked pause span (back-date clamp)
        self._setup_nag = False       # once-per-session "capture degraded — re-run setup" hint
        self._wiz_dlg = None          # the Capture Doctor dialog, when open (single instance)
        self._pause_at = 0.0          # wall time the current pause began
        self.run_paused_total = 0.0   # total seconds spent paused this run (excluded from elapsed)
        self._reset_nudged = False    # fired the "reset soon" alert for the current cycle?
        self._last_open = None        # (coords, wall time) — feeds travel-time learning
        self._last_target = None      # {coords, at} last chest the poller saw — fallback when the
                                      # chest UI covers the F7 panel on the actual open
        self._hud_count = None        # ({area: (opened, total)}, at) — last HUD panel reading
        self._hud_pending_open = None # a +1 awaiting confirmation by a second agreeing reading
        self._hud_reset_cand = None   # a new reset observation awaiting its second reading
        self._hud_scan_t = 0          # last HUD OCR pass
        self._success_log_t = 0       # throttle for logging raw OCR of successfully-read chests
        self._debug = None            # active detection-report capture session (or None)
        self._hud_band = None         # (y0, y1) where the movable HUD panel currently sits
        self._hud_miss = 0            # consecutive band reads without the panel
        self._hud_seek_t = 0          # last full-frame rediscovery attempt
        self._hud_area = None         # current area per the HUD (best effort, advisory)
        self._update_avail = None     # (tag, url) once a newer release is known
        self._areatotals_t = 0        # last areatotals push
        self._areatotals_cand = None  # changed totals awaiting their second agreeing reading
        self.dr = DeadReckoner(cfg)
        self.hud = HudReader(cfg)
        self.watcher = InputWatcher(self._hotkey_map())
        self.poll_flag = threading.Event()
        self._last_tick = time.time()
        self._build_ui()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._refresher, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()
        self.root.after(1500, lambda: threading.Thread(target=self._check_updates, daemon=True).start())
        self.root.after(700, self._platform_check)
        if int(cfg.get("setup_done") or 0) < SETUP_VERSION:
            # first launch (or the probes changed since): offer the one-time Capture Doctor.
            # Closing it marks setup_done, so this fires once — ⚙ Settings re-runs it anytime.
            self.root.after(1200, lambda: self._open_setup_wizard(first=True))
        self.root.after(50, self._ui_tick)

    def _platform_check(self):
        """On Wayland, tell the user up front about any missing pieces (grim / evdev / group)."""
        if not IS_WAYLAND:
            return
        need = []
        if not shutil.which("grim"):
            need.append("grim (screen capture)")
        if evdev is None:
            need.append("python-evdev (key detection)")
        elif not (os.access("/dev/input/event0", os.R_OK) or
                  any(os.access(p, os.R_OK) for p in (evdev.list_devices() or []))):
            need.append("read access to /dev/input — add yourself to the 'input' group")
        if need:
            self.set_status("Wayland setup needed: " + " · ".join(need), WARN)

    def _hotkey_map(self):
        """keyname -> action tag for the InputWatcher. The optional log-by-aim key shares the
        'chest' action, so aiming at a chest and pressing it logs the same as opening it —
        useful when the chest UI covers the F7 coordinates at some resolutions."""
        m = {self.cfg["hotkey_chest"]: "chest", self.cfg["hotkey_undo"]: "undo"}
        if self.cfg.get("hotkey_log"):
            m[self.cfg["hotkey_log"]] = "chest"
        return m

    # ---------------- UI ----------------
    PADX = 12  # consistent horizontal margin for every section

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Histatu Runner")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg=BORDER)  # 1px perimeter border shows through the inset below
        self.collapsed = bool(self.cfg.get("collapsed"))
        self._docked = False
        self._dragged = False
        self._game_bbox = None  # cached by the poller so tick-driven docks don't enumerate windows
        px = self.PADX
        outer = tk.Frame(self.root, bg=BG); outer.pack(fill="both", expand=True, padx=1, pady=1)

        # ---- title bar (also the drag handle) ----
        bar = tk.Frame(outer, bg=BG); bar.pack(fill="x", padx=px, pady=(9, 4))
        self.dot = tk.Label(bar, text="●", fg=FG3, bg=BG, font=(UIFONT, 9))
        self.dot.pack(side="left")
        tk.Label(bar, text="Histatu", fg=FG, bg=BG, font=(UIFONT, 11, "bold")).pack(side="left", padx=(6, 0))
        if self.cfg["dry_run"] or self.cfg.get("_dry_cli"):
            # loud on purpose: in dry-run every "✓ logged" is a no-op, so this must be unmissable
            tk.Label(bar, text=" DRY RUN — NOT SAVING ", fg=BAD, bg=SURFACE,
                     font=(UIFONT, 8, "bold")).pack(side="left", padx=6)
        for txt, cmd, tip in (("✕", self.root.destroy, "Close Histatu Runner"),
                              ("⚙", self._open_settings, "Settings — hotkeys, poll rate, reset hour, "
                                                          "and 🐞 report a detection issue"),
                              ("ⓘ", self._show_help, "How to use the tool"),
                              ("⤢", self.dock, "Dock into the corner of the game window")):
            self._iconbtn(bar, txt, cmd, tip=tip).pack(side="right", padx=(2, 0))
        self.btn_collapse = self._iconbtn(bar, "▾", self.toggle_collapse,
                                          tip="Collapse to just the title bar (logging keeps working)")
        self.btn_collapse.pack(side="right", padx=(2, 0))
        # Discord-style update button: exists unpacked until a newer release is known, then
        # appears green in the title bar; clicking opens the update dialog
        self.btn_update = tk.Button(bar, text="⬇", command=self._update_clicked, fg=ACCENT, bg=BG,
                                    bd=0, padx=5, pady=1, activebackground=RAISED,
                                    activeforeground=ACCENT, font=(UIFONT, 10, "bold"),
                                    cursor="hand2", takefocus=0)
        self.btn_update.bind("<Enter>", lambda e: self.btn_update.config(bg=RAISED))
        self.btn_update.bind("<Leave>", lambda e: self.btn_update.config(bg=BG))
        self._tip(self.btn_update, "An update is ready — click to install")
        for w in (bar, self.dot) + tuple(c for c in bar.winfo_children() if isinstance(c, tk.Label)):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._dragged and self._save_geom())

        # everything below collapses away to shrink the overlay outside a run
        self.controls = tk.Frame(outer, bg=BG)

        # ---- identity: IGN + editor key ----
        idrow = tk.Frame(self.controls, bg=BG); idrow.pack(fill="x", padx=px, pady=(2, 0))
        self.ign_var = tk.StringVar(value=self.cfg["ign"])
        ignwrap, ign = self._field(idrow, "👤", self.ign_var)
        ignwrap.pack(side="left", fill="x", expand=True)
        ign.bind("<FocusOut>", lambda e: self._save_ign())
        ign.bind("<Return>", lambda e: (self._save_ign(), self.root.focus()))
        self.key_lbl = tk.Label(idrow, text="", fg=FG3, bg=BG, font=(UIFONT, 8, "bold"))
        self.key_lbl.pack(side="right", padx=(8, 0))

        keyrow = tk.Frame(self.controls, bg=BG); keyrow.pack(fill="x", padx=px, pady=(6, 0))
        self.key_var = tk.StringVar(value=self.cfg.get("write_key", ""))
        keywrap, keyent = self._field(keyrow, "🔑", self.key_var, show="•")
        keywrap.pack(side="left", fill="x", expand=True)
        keyent.bind("<FocusOut>", lambda e: self._save_key())
        keyent.bind("<Return>", lambda e: (self._save_key(), self.root.focus()))
        # Sign-in happens on the WEBSITE (you approve on Hytale's own site — this app never sees your
        # password); it hands you an hd_ key to paste into the box above. This button just opens that page.
        self.btn_signin = self._btn(keyrow, "Sign in", self._open_signin, small=True)
        self.btn_signin.pack(side="left", padx=(6, 0))
        self._tip(self.btn_signin, "Opens the website to sign in with your Hytale account. Copy the key "
                                   "it gives you and paste it into the 🔑 box — the app remembers it.")

        # ---- mode selector (segmented) ----
        seg = tk.Frame(self.controls, bg=BORDER); seg.pack(fill="x", padx=px, pady=(10, 0))
        self.btn_log = self._seg(seg, "⏺ Log", "log")
        self.btn_rec = self._seg(seg, "🧭 Record", "record")
        self.btn_verify = self._seg(seg, "🔎 Verify", "verify")
        for b in (self.btn_log, self.btn_rec, self.btn_verify):
            b.pack(side="left", fill="x", expand=True, padx=1, pady=1)
        self._tip(self.btn_log, "Log chests: open a chest (F) and it's added to the shared map")
        self._tip(self.btn_rec, "Record a route: the chests you open become its stops, with leg times")
        self._tip(self.btn_verify, "Verify (editors): open a chest at a pending request to confirm it")

        # ---- run a route ----
        runrow = tk.Frame(self.controls, bg=BG); runrow.pack(fill="x", padx=px, pady=(8, 0))
        self.route_var = tk.StringVar()
        self.route_box = ttk.Combobox(runrow, textvariable=self.route_var, state="readonly",
                                      font=(UIFONT, 9), style="Histatu.TCombobox")
        self.route_box.pack(side="left", fill="x", expand=True, ipady=3)
        self.btn_run = self._btn(runrow, "▶ Run", self._toggle_run, primary=True)
        self.btn_run.pack(side="right", padx=(8, 0))
        self._tip(self.route_box, "Pick a published route to run")
        self._tip(self.btn_run, "Race the selected route: a stopwatch counts your opens against its "
                                "stops and records your finish time to the leaderboard")

        # ---- context actions ----
        ctx = tk.Frame(self.controls, bg=BG); ctx.pack(fill="x", padx=px, pady=(8, 0))
        undob = self._btn(ctx, "↩ Undo", lambda: self.jobs.put(("undo",)), small=True)
        undob.pack(side="left")
        self._tip(undob, "Remove the last chest you logged")
        # Pause/Resume — packed only while a run is active (managed by _style_pause)
        self.btn_pause = self._btn(ctx, "⏸ Pause", self._toggle_pause, small=True)
        self._tip(self.btn_pause, "Pause the run: the timer freezes, so a break doesn't ruin your "
                                  "time. Opens still count toward cooldowns, not the run.")

        # ---- cooldowns: manual reset for the random in-game unlock event ----
        cdrow = tk.Frame(self.controls, bg=BG); cdrow.pack(fill="x", padx=px, pady=(6, 0))
        self.btn_resetcd = self._btn(cdrow, "♻ Reset my cooldowns", self._reset_cooldowns_clicked, small=True)
        self.btn_resetcd.pack(side="left")
        self._tip(self.btn_resetcd, "Mark ALL your chest cooldowns as available again — for the random "
                                    "in-game event that unlocks every chest early. Local to you; "
                                    "opening a chest re-locks it as usual.")

        self.stats = tk.Label(self.controls, text="", fg=FG3, bg=BG, font=(UIFONT, 8),
                              anchor="w", justify="left")
        self.stats.pack(fill="x", padx=px, pady=(10, 2))
        self._tip(ign, "Your in-game name. Once you sign in on the website it's set from your verified "
                       "Hytale account, so opens and runs are logged as the real you.")
        self._tip(keyent, "Your key from the website — Sign in with Hytale there, copy the hd_… key, and "
                          "paste it here once. It signs your chest opens, runs and (for editors) map edits.")

        # ---- run stopwatch line (elapsed · opened · pause state; empty outside a run) ----
        self.eta_label = tk.Label(outer, text="", fg=FG3, bg=BG, font=(UIFONT, 9),
                                  wraplength=300, justify="center")
        self.eta_label.pack(fill="x", padx=px)

        # ---- status bar ----
        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", pady=(8, 0))
        self.status = tk.Label(outer, text="Set your IGN, then pick a mode", fg=FG2,
                               bg=BG, font=(UIFONT, 9), wraplength=300, justify="left", anchor="w")
        self.status.pack(fill="x", padx=px, pady=(6, 9))

        self._install_combobox_style()
        self._style_modes_ui()
        self._apply_collapsed()
        # keep the overlay out of the OCR grabs so it can sit over the coordinates
        self._capture_excluded = exclude_from_capture(self.root)
        self._place_window()

    # ---- widget helpers ----
    def _entry(self, parent, var, show="", width=0, justify="left"):
        e = tk.Entry(parent, textvariable=var, bg=FIELD, fg=FG, insertbackground=ACCENT,
                     bd=0, relief="flat", font=(UIFONT, 10), show=show, justify=justify,
                     insertwidth=1, highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT)
        if width:
            e.config(width=width)
        return e

    def _field(self, parent, icon, var, show="", width=0):
        """Icon + styled entry sharing one rounded-looking FIELD strip."""
        wrap = tk.Frame(parent, bg=FIELD, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        tk.Label(wrap, text=icon, fg=FG3, bg=FIELD, font=(UIFONT, 10)).pack(side="left", padx=(7, 0))
        e = tk.Entry(wrap, textvariable=var, bg=FIELD, fg=FG, insertbackground=ACCENT, bd=0,
                     relief="flat", font=(UIFONT, 10), show=show, insertwidth=1,
                     highlightthickness=0)
        if width:
            e.config(width=width)
        e.pack(side="left", fill="x", expand=True, padx=(4, 8), ipady=4)
        # focus ring on the whole strip
        e.bind("<FocusIn>", lambda ev: wrap.config(highlightbackground=ACCENT), add="+")
        e.bind("<FocusOut>", lambda ev: wrap.config(highlightbackground=BORDER), add="+")
        return wrap, e

    def _iconbtn(self, parent, text, cmd, tip=None):
        b = tk.Button(parent, text=text, command=cmd, fg=FG2, bg=BG, bd=0, padx=5, pady=1,
                      activebackground=RAISED, activeforeground=FG, font=(UIFONT, 10),
                      cursor="hand2", takefocus=0)
        b.bind("<Enter>", lambda e: b.config(bg=RAISED, fg=FG))
        b.bind("<Leave>", lambda e: b.config(bg=BG, fg=FG2))
        if tip:
            self._tip(b, tip)
        return b

    def _tip(self, widget, text):
        """Hover tooltip: a small dark popup that appears below `widget` after a short delay.
        Kept out of screen captures like the main overlay so it never pollutes the OCR grabs."""
        state = {"win": None, "after": None}

        def show():
            if state["win"] or not widget.winfo_ismapped():
                return
            tw = tk.Toplevel(self.root)
            tw.overrideredirect(True)
            tw.attributes("-topmost", True)
            tk.Label(tw, text=text, bg=RAISED, fg=FG, font=(UIFONT, 8), justify="left",
                     wraplength=240, padx=8, pady=5, bd=0).pack()
            try:
                exclude_from_capture(tw)
            except Exception:
                pass
            tw.update_idletasks()
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            sw = widget.winfo_screenwidth()
            x = min(x, sw - tw.winfo_width() - 6)
            tw.geometry("+%d+%d" % (max(4, x), y))
            state["win"] = tw

        def enter(_):
            state["after"] = widget.after(450, show)

        def leave(_):
            if state["after"]:
                widget.after_cancel(state["after"]); state["after"] = None
            if state["win"]:
                state["win"].destroy(); state["win"] = None

        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", leave, add="+")
        widget.bind("<ButtonPress>", leave, add="+")

    def _show_help(self):
        """How-to dialog reachable from the ⓘ title-bar button."""
        dlg = self._modal("How to use Histatu Runner")
        tk.Label(dlg, text="Histatu Runner", fg=FG, bg=BG, font=(UIFONT, 12, "bold")).pack(
            padx=16, pady=(14, 0), anchor="w")
        tk.Label(dlg, text="Logs the chests you open to the shared web map — by reading the F7 "
                           "panel off your own screen. No mods, no game files touched.",
                 fg=FG3, bg=BG, font=(UIFONT, 8), justify="left", wraplength=360).pack(
            padx=16, pady=(2, 8), anchor="w")
        steps = [
            ("1 · Set up once", "Enter your IGN. In game, press F7 until the top-right WORLD panel "
                                "(Position / Orientation / Target) is visible."),
            ("2 · Pick a mode",
             "⏺ Log — open chests (F); each new one is added to the map.\n"
             "🧭 Record — turn the chests you open into a shareable route.\n"
             "🔎 Verify (editors) — confirm crowd-sourced pending chests."),
            ("3 · Run routes", "Study a published route on the web map, press ▶ Run, and race it: "
                               "the app counts your opens against the route's stops and records "
                               "your time to the leaderboard when you finish. It never navigates "
                               "for you — knowing the route is the skill."),
            ("While running", "The stopwatch line shows elapsed time, stops done, and opens. "
                              "⏸ Pause freezes the clock so a break doesn't ruin your time — and it "
                              "auto-pauses in menus, on lost focus, or when you stand still."),
            ("Smarter maps", "Every pair of consecutive opens teaches the shared map how long the "
                             "leg between those chests takes — that data powers the web map's "
                             "route planning and coverage heatmap for everyone."),
            ("Fixes", "↩ Undo removes your last logged chest."),
            ("Cooldowns", "Every chest you open relocks until the daily 8 PM Eastern reset "
                          "(read from the game's own HUD). The stats line shows how many are up. "
                          "♻ Reset my cooldowns marks them all available again after the random "
                          "in-game event that unlocks every chest early."),
            ("Trouble reading a chest?",
             ("This is Histatu Runner Lite — it never captures or sends anything. Get the Full "
              "version to send a short diagnostic that helps fix hard-to-read chests." if IS_LITE
              else "⚙ Settings → 🐞 Report detection issue captures a short, panel-region-only "
                   "diagnostic and sends it (privately, deleted after review) for a fix.")),
        ]
        for head, body in steps:
            tk.Label(dlg, text=head, fg=ACCENT, bg=BG, font=(UIFONT, 9, "bold")).pack(
                padx=16, pady=(8, 0), anchor="w")
            tk.Label(dlg, text=body, fg=FG2, bg=BG, font=(UIFONT, 8), justify="left",
                     wraplength=380).pack(padx=16, anchor="w")
        btns = tk.Frame(dlg, bg=BG); btns.pack(fill="x", padx=16, pady=(12, 14))
        self._mkbtn(btns, "Got it", dlg.destroy).pack(side="right")
        self._place_modal(dlg, modal=False)

    def _btn(self, parent, text, cmd, primary=False, small=False):
        base = ACCENT if primary else FIELD
        fg = ACCENT_FG if primary else FG
        hot = "#54d99a" if primary else RAISED
        b = tk.Button(parent, text=text, command=cmd, fg=fg, bg=base, bd=0,
                      padx=(8 if small else 12), pady=(3 if small else 5),
                      activebackground=hot, activeforeground=fg, cursor="hand2", takefocus=0,
                      font=(UIFONT, 8 if small else 9, "bold"))
        b._base, b._hot = base, hot
        b.bind("<Enter>", lambda e: b["state"] != "disabled" and b.config(bg=b._hot))
        b.bind("<Leave>", lambda e: b.config(bg=b._base))
        return b

    def _seg(self, parent, text, mode):
        b = tk.Button(parent, text=text, command=lambda: self.set_mode(mode), bd=0,
                      padx=2, pady=6, cursor="hand2", takefocus=0, font=(UIFONT, 9),
                      bg=SURFACE, fg=FG2, activebackground=RAISED, activeforeground=FG)
        b._mode = mode
        b.bind("<Enter>", lambda e: getattr(b, "_mode", None) != self.mode and b.config(bg=RAISED, fg=FG))
        b.bind("<Leave>", lambda e: self._paint_seg(b))
        return b

    def _paint_seg(self, b):
        active = getattr(b, "_mode", None) == self.mode
        b.config(bg=ACCENT if active else SURFACE, fg=ACCENT_FG if active else FG2,
                 font=(UIFONT, 9, "bold" if active else "normal"))

    def _install_combobox_style(self):
        try:
            style = ttk.Style()
            style.theme_use("clam")  # the native Windows theme ignores field colors; clam honors them
            style.configure("Histatu.TCombobox", fieldbackground=FIELD, background=FIELD,
                            foreground=FG, arrowcolor=FG2, bordercolor=BORDER, lightcolor=FIELD,
                            darkcolor=FIELD, borderwidth=0, relief="flat", padding=6)
            style.map("Histatu.TCombobox",
                      fieldbackground=[("readonly", FIELD)], background=[("readonly", FIELD)],
                      foreground=[("readonly", FG)], bordercolor=[("focus", ACCENT), ("hover", RAISED)],
                      arrowcolor=[("hover", FG)],
                      selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
            self.root.option_add("*TCombobox*Listbox.background", FIELD)
            self.root.option_add("*TCombobox*Listbox.foreground", FG)
            self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
            self.root.option_add("*TCombobox*Listbox.selectForeground", ACCENT_FG)
            self.root.option_add("*TCombobox*Listbox.borderWidth", 0)
            self.root.option_add("*TCombobox*Listbox.font", (UIFONT, 9))
        except Exception:
            pass

    def _mkbtn(self, parent, text, cmd):  # back-compat for the modal dialogs
        return self._btn(parent, text, cmd, primary=True)

    def _modal(self, title):
        """A dark Toplevel that reliably sits ABOVE the borderless, always-on-top
        overlay — tkinter's simpledialog can get lost behind an overrideredirect
        topmost window, so we build our own."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        try:
            dlg.transient(self.root)
        except Exception:
            pass
        return dlg

    def _place_modal(self, dlg, modal=True):
        dlg.update_idletasks()
        try:
            dlg.geometry("+%d+%d" % (self.root.winfo_x() + 24, self.root.winfo_y() + 44))
        except Exception:
            pass
        if modal:
            dlg.grab_set()
            self.root.wait_window(dlg)

    def _prompt(self, title, label, initial="", secret=False):
        """Modal single-line text prompt. Returns the string, or None if cancelled."""
        dlg = self._modal(title)
        tk.Label(dlg, text=label, fg=FG, bg=BG, font=("Segoe UI", 10)).pack(
            padx=14, pady=(12, 4), anchor="w")
        var = tk.StringVar(value=initial)
        ent = tk.Entry(dlg, textvariable=var, width=30, bg=FIELD, fg=FG, insertbackground=ACCENT,
                       bd=0, font=("Segoe UI", 10), show="•" if secret else "")
        ent.pack(padx=14, pady=2, ipady=3)
        out = {"v": None}
        def ok(*_):
            out["v"] = var.get(); dlg.destroy()
        btns = tk.Frame(dlg, bg=BG); btns.pack(fill="x", padx=14, pady=(8, 12))
        self._mkbtn(btns, "OK", ok).pack(side="right")
        tk.Button(btns, text="Cancel", command=dlg.destroy, fg=FG2, bg=FIELD, bd=0,
                  padx=9, pady=3, activebackground=RAISED, activeforeground=FG,
                  font=("Segoe UI", 9)).pack(side="right", padx=6)
        ent.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        ent.focus_force()
        self._place_modal(dlg)
        return out["v"]

    # ---------------- Capture Doctor (one-time setup wizard) ----------------

    def _open_setup_wizard(self, first=False):
        """🩺 Per-device tuning: find the game window, measure the sharpest OCR zoom for this
        resolution, health-check each F7 signal, and time the player's real travel pace. Probes
        run on worker threads (the dialog is NON-modal — the player has to alt-tab into the game
        mid-wizard). Closing early still marks setup_done, so the first-launch offer never nags
        twice; ⚙ Settings re-runs it anytime."""
        try:
            if self._wiz_dlg is not None and self._wiz_dlg.winfo_exists():
                self._wiz_dlg.lift()
                return
        except Exception:
            pass
        dlg = self._modal("🩺 Capture Doctor — device setup")
        # CRITICAL: the wizard is topmost and parks near the overlay — which docks over the F7
        # panel — so without capture exclusion the probes would OCR the wizard's own pixels and
        # diagnose a healthy device as broken. Same treatment the tooltips get.
        wiz_hidden_from_grabs = exclude_from_capture(dlg)
        self._wiz_dlg = dlg
        st = {"panel": None, "panel_err": None, "movement": None, "busy": False, "step": 0}

        head = tk.Label(dlg, text="", fg=FG, bg=BG, font=(UIFONT, 11, "bold"))
        head.pack(padx=14, pady=(12, 2), anchor="w")
        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="both", expand=True, padx=14)
        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=14, pady=(10, 12))

        def clear(w):
            for c in w.winfo_children():
                c.destroy()

        def lab(parent, txt, fg=FG2, size=9, bold=False):
            return tk.Label(parent, text=txt, fg=fg, bg=BG, wraplength=340, justify="left",
                            font=(UIFONT, size, "bold") if bold else (UIFONT, size))

        def finish_close(finished):
            # closable even mid-probe (the thread's done() checks winfo_exists before touching
            # widgets; an in-flight probe's result is simply forfeited).
            # apply partial results; on a later re-run closed with NO new data, leave the stored
            # baseline untouched instead of overwriting it with an empty "skipped" one
            if st["panel"] or st["movement"] or int(self.cfg.get("setup_done") or 0) < SETUP_VERSION:
                notes = apply_setup_results(self.cfg, st["panel"], st["movement"], completed=finished)
                save_config(self.cfg)
                if notes:
                    self.set_status("🩺 Setup saved — " + " · ".join(notes), GOOD)
                elif finished:
                    self.set_status("🩺 Setup finished — capture already reads fine as-is", GOOD)
                else:
                    self.set_status("Setup skipped — re-run it anytime from ⚙ Settings", FG2)
            self._wiz_dlg = None
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", lambda: finish_close(False))

        def nav(back=None, nxt=None, nxt_label="Next ➜", finishing=False):
            clear(btns)
            tk.Button(btns, text="Close", command=lambda: finish_close(False), fg=FG3, bg=FIELD,
                      bd=0, padx=9, pady=3, activebackground=RAISED, activeforeground=FG,
                      font=(UIFONT, 9)).pack(side="left")
            if finishing:
                self._mkbtn(btns, "✔ Finish setup", lambda: finish_close(True)).pack(side="right")
            elif nxt:
                self._mkbtn(btns, nxt_label, nxt).pack(side="right")
            if back:
                tk.Button(btns, text="⟵ Back", command=back, fg=FG2, bg=FIELD, bd=0, padx=9,
                          pady=3, activebackground=RAISED, activeforeground=FG,
                          font=(UIFONT, 9)).pack(side="right", padx=6)

        # ---- step 0: why this exists (first launch only) ----
        def step_welcome():
            st["step"] = 0
            head.config(text="Welcome — quick one-time setup")
            clear(body)
            lab(body, "The tool reads the game's F7 debug panel from screen captures, and every "
                      "PC renders it a little differently (resolution, monitor scaling, GUI size). "
                      "This ~1-minute check tunes the reader to YOUR setup:").pack(anchor="w")
            lab(body, "   1 · find your game window\n"
                      "   2 · measure the sharpest OCR zoom for your panel\n"
                      "   3 · time your real walk pace (sharper open-tracking)",
                FG, 9).pack(anchor="w", pady=(5, 3))
            lab(body, "Have Hytale running with the F7 panel open. Every step is skippable, and "
                      "you can re-run this anytime from ⚙ Settings.", FG3, 8).pack(anchor="w")
            nav(nxt=step_window, nxt_label="Start ➜")

        # ---- step 1: which window is the game ----
        def step_window():
            st["step"] = 1
            head.config(text="Step 1 · Find the game window")
            clear(body)
            found = None
            try:
                found = find_game_window(self.cfg["window_title"])
            except Exception:
                pass
            if found:
                lab(body, "✓ Found “%s” — %d×%d px" % (self.cfg["window_title"],
                    found[2] - found[0], found[3] - found[1]), GOOD, 10, bold=True).pack(anchor="w")
                lab(body, "That window will be captured. If it's the wrong one, edit the title in "
                          "⚙ Settings.", FG3, 8).pack(anchor="w", pady=(3, 0))
                nav(back=step_welcome if first else None, nxt=step_panel)
                return
            lab(body, "No window titled “%s” found. Start the game and press ⟳ Re-check, or pick "
                      "it below:" % self.cfg["window_title"], WARN).pack(anchor="w")
            wins = list_game_windows()
            if wins:
                lb = tk.Listbox(body, height=min(6, max(3, len(wins))), bg=FIELD, fg=FG, bd=0,
                                selectbackground=ACCENT, selectforeground=ACCENT_FG,
                                highlightthickness=0, font=(UIFONT, 9), width=44)
                for t, w, h in wins[:12]:
                    lb.insert("end", " %s   (%d×%d)" % (t, w, h))
                lb.pack(fill="x", pady=(6, 4))

                def use_sel():
                    sel = lb.curselection()
                    if sel:
                        self.cfg["window_title"] = wins[sel[0]][0]
                        save_config(self.cfg)
                        step_window()
                row = tk.Frame(body, bg=BG)
                row.pack(anchor="w", pady=(2, 0))
                self._btn(row, "Use selected", use_sel, primary=True, small=True).pack(side="left")
                self._btn(row, "⟳ Re-check", step_window, small=True).pack(side="left", padx=6)
            else:
                self._btn(body, "⟳ Re-check", step_window, small=True).pack(anchor="w", pady=(6, 0))
            lab(body, "Without a match the tool captures the whole screen — that still works, "
                      "just slower and more fragile.", FG3, 8).pack(anchor="w", pady=(4, 0))
            nav(back=step_welcome if first else None, nxt=step_panel, nxt_label="Skip ➜")

        # ---- step 2: panel scan → best OCR zoom + per-signal health ----
        def render_panel_result(parent):
            p = st["panel"]
            f = p.get("fields", {})

            def mark(name, key):
                v = f.get(key, 0)
                return "%s %s %d%%" % ("✓" if v >= 0.5 else "✗", name, round(v * 100))
            lab(parent, "Frames read: %d · game window %s" % (p.get("frames", 0),
                "✓" if p.get("window") else "✗ (full-screen fallback)"), FG3, 8).pack(anchor="w", pady=(4, 0))
            lab(parent, mark("Position", "position") + "    " + mark("Facing", "yaw"),
                GOOD if f.get("position", 0) >= 0.5 else BAD, 10, bold=True).pack(anchor="w", pady=(3, 0))
            lab(parent, mark("Speed", "speed") + "    " + mark("Velocity", "velocity") +
                "    " + mark("Wish Dir", "wishdir"),
                FG2 if f.get("speed", 0) >= 0.5 else FG3, 9).pack(anchor="w")
            if p.get("best"):
                lab(parent, "Sharpest OCR zoom for your panel: ×%d — it will be tried first on "
                            "every read." % p["best"], GOOD, 9).pack(anchor="w", pady=(3, 0))
            if f.get("position", 0) < 0.5:
                lab(parent, "Position didn't read. Is the F7 panel open, fully visible, and not "
                            "covered by a menu? Fix and Scan again.", WARN, 8).pack(anchor="w", pady=(3, 0))

        def step_panel():
            st["step"] = 2
            head.config(text="Step 2 · Read the F7 panel")
            clear(body)
            lab(body, "In the game: open the F7 debug panel (Position visible) and stand still. "
                      "Then press Scan — every OCR zoom is tried and the sharpest one for your "
                      "resolution is kept.").pack(anchor="w")
            res = tk.Frame(body, bg=BG)

            def scan():
                if st["busy"]:
                    return
                st["busy"] = True
                clear(res)
                lab(res, "Scanning… keep the F7 panel visible (a few seconds)", ACCENT, 9,
                    bold=True).pack(anchor="w", pady=(4, 0))

                def work():
                    try:
                        p = probe_panel(self.cfg)
                        err = None if p.get("frames") else "capture failed — is the game visible?"
                    except Exception as e:
                        p, err = None, str(e)

                    def done():
                        st["busy"] = False
                        if not dlg.winfo_exists():
                            return
                        if p and p.get("frames"):
                            st["panel"] = p
                        st["panel_err"] = err
                        rerender()  # whichever step is showing (the user may have navigated on)
                    self.root.after(0, done)
                threading.Thread(target=work, daemon=True).start()

            row = tk.Frame(body, bg=BG)
            row.pack(anchor="w", pady=(7, 0))
            self._btn(row, "📸 Scan the panel", scan, primary=not st["panel"], small=True).pack(side="left")
            res.pack(fill="x")
            if st["panel_err"]:
                lab(res, "⚠ " + st["panel_err"], WARN, 8).pack(anchor="w", pady=(4, 0))
            if st["panel"]:
                render_panel_result(res)
            nav(back=step_window, nxt=step_move,
                nxt_label="Next ➜" if st["panel"] else "Skip ➜")

        # ---- step 3: movement test → real travel pace + yaw ground-truth ----
        def render_move_result(parent):
            m = st["movement"]
            ok = m.get("hits", 0) >= 2
            lab(parent, "Position fixes: %d/%d reads" % (m.get("hits", 0), m.get("reads", 0)),
                GOOD if ok else BAD, 10, bold=True).pack(anchor="w", pady=(4, 0))
            if m.get("speed_bps"):
                extra = "" if m.get("game_speed") is None else \
                        "  (game reports %.1f)" % m["game_speed"]
                # honesty: apply_setup_results only saves a clear WALKING gait (2.0..<7.0) — the
                # estimate coasts at walk speed; anything else is shown but not applied
                if 2.0 <= m["speed_bps"] < 7.0:
                    lab(parent, "Measured walk pace: %.1f blocks/s%s — open-tracking will use this."
                        % (m["speed_bps"], extra), GOOD, 9).pack(anchor="w")
                else:
                    lab(parent, "Measured %.1f blocks/s%s — not a walking pace, so nothing is "
                                "changed (the game's own Speed line covers sprinting)."
                        % (m["speed_bps"], extra), FG2, 9).pack(anchor="w")
            if m.get("yaw_ok") is True:
                lab(parent, "✓ Facing reads reliably on this device.", FG2, 9).pack(anchor="w", pady=(2, 0))
            elif m.get("yaw_ok") is False:
                lab(parent, "✗ Facing didn't match your travel direction — open-tracking will "
                            "lean on distance checks (normal on some setups).",
                    WARN, 9).pack(anchor="w", pady=(2, 0))
            if not ok:
                lab(parent, "Too few position reads — run it again with the F7 panel open the "
                            "whole time.", WARN, 8).pack(anchor="w", pady=(2, 0))

        def step_move():
            st["step"] = 3
            head.config(text="Step 3 · Time your travel pace")
            clear(body)
            lab(body, "Find a clear straight stretch. Press Start, switch to the game, then HOLD "
                      "W and run straight ahead at your normal dungeon pace (sprint if you sprint) "
                      "until the result appears — about 5 seconds. Keep F7 open.").pack(anchor="w")
            res = tk.Frame(body, bg=BG)

            def start():
                if st["busy"]:
                    return
                st["busy"] = True
                clear(res)
                lab(res, "Measuring… RUN STRAIGHT NOW (about 5 seconds)", ACCENT, 10,
                    bold=True).pack(anchor="w", pady=(4, 0))

                def work():
                    try:
                        m = probe_movement(self.cfg, seconds=4.5)
                    except Exception:
                        m = {"reads": 0, "hits": 0}

                    def done():
                        st["busy"] = False
                        if not dlg.winfo_exists():
                            return
                        if m.get("reads"):
                            st["movement"] = m
                        rerender()  # whichever step is showing (stale summaries must refresh too)
                    self.root.after(0, done)
                threading.Thread(target=work, daemon=True).start()

            row = tk.Frame(body, bg=BG)
            row.pack(anchor="w", pady=(7, 0))
            self._btn(row, "▶ Start the 5-second run", start, primary=not st["movement"],
                      small=True).pack(side="left")
            res.pack(fill="x")
            if st["movement"]:
                render_move_result(res)
            nav(back=step_panel, nxt=step_summary,
                nxt_label="Next ➜" if st["movement"] else "Skip ➜")

        # ---- step 4: what gets saved ----
        def step_summary():
            st["step"] = 4
            head.config(text="Step 4 · Save your device profile")
            clear(body)
            preview = apply_setup_results(dict(self.cfg), st["panel"], st["movement"], completed=True)
            if preview:
                lab(body, "Finishing will save:", FG, 9, bold=True).pack(anchor="w")
                for n in preview:
                    lab(body, "  · " + n, FG2, 9).pack(anchor="w")
            else:
                lab(body, "No probes ran (all steps skipped) — nothing gets tuned, and the tool "
                          "keeps its standard behaviour. That's fine: it adapts as you play.",
                    FG2).pack(anchor="w")
            lab(body, "This profile lives in capture_config.json. Changed monitor, resolution, or "
                      "GUI scale later? Re-run setup from ⚙ Settings.", FG3, 8).pack(anchor="w", pady=(5, 0))
            nav(back=step_move, finishing=True)

        def rerender():
            {0: step_welcome, 1: step_window, 2: step_panel,
             3: step_move, 4: step_summary}.get(st["step"], lambda: None)()

        (step_welcome if first else step_window)()
        self._place_modal(dlg, modal=False)
        if not wiz_hidden_from_grabs:
            # capture exclusion unavailable (old Windows / Linux / env escape): the dialog WILL
            # be baked into grabs, so park it far left, clear of the panel strip (right 38%)
            try:
                dlg.geometry("+30+%d" % max(40, int(dlg.winfo_screenheight() * 0.55)))
            except Exception:
                pass

    def _open_settings(self):
        """Edit hotkeys + capture settings without touching capture_config.json by hand."""
        dlg = self._modal("Settings")
        rows = [
            ("Chest / open key", "hotkey_chest", "key"),
            ("Log-by-aim key (optional)", "hotkey_log", "key_opt"),
            ("Undo-last key", "hotkey_undo", "key"),
            ("OCR poll (seconds)", "ocr_poll_sec", "float"),
            ("Daily reset hour (ET)", "reset_hour_et", "int"),
            ("Game window title", "window_title", "text"),
        ]
        fields = {}
        grid = tk.Frame(dlg, bg=BG); grid.pack(padx=14, pady=(12, 2))
        for i, (label, key, kind) in enumerate(rows):
            tk.Label(grid, text=label, fg=FG, bg=BG, font=("Segoe UI", 9)).grid(
                row=i, column=0, sticky="w", pady=3, padx=(0, 10))
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            fields[key] = (var, kind)
            tk.Entry(grid, textvariable=var, width=16, bg=FIELD, fg=FG, insertbackground=ACCENT,
                     bd=0, font=("Segoe UI", 9)).grid(row=i, column=1, pady=3, ipady=2)
        tk.Label(dlg, text="Keys: letters, digits, F1–F12, DELETE/END/HOME, LMB/RMB/MOUSE4/MOUSE5.\n"
                           "Log-by-aim: a spare key — look at a chest and press it to log it without\n"
                           "opening it (for when the chest screen covers the F7 coordinates).",
                 fg=FG3, bg=BG, font=("Segoe UI", 8), wraplength=300, justify="left").pack(
            padx=14, pady=(4, 0), anchor="w")
        err = tk.Label(dlg, text="", fg="#e0a030", bg=BG, font=("Segoe UI", 8),
                       wraplength=300, justify="left")
        err.pack(padx=14, anchor="w")

        def save(*_):
            new = {}
            for key, (var, kind) in fields.items():
                if kind == "bool":
                    new[key] = bool(var.get()); continue
                s = var.get().strip()
                if kind == "key" or kind == "key_opt":
                    if not s and kind == "key_opt":
                        new[key] = ""; continue          # optional key left blank = disabled
                    up = s.upper()
                    if not up or vk_of(up) is None:
                        err.config(text="Unrecognized key for “%s”: %r" % (key, s)); return
                    new[key] = up
                elif kind == "float":
                    try: new[key] = float(s)
                    except ValueError: err.config(text="“%s” must be a number" % key); return
                elif kind == "int":
                    try: new[key] = int(float(s))
                    except ValueError: err.config(text="“%s” must be a whole number" % key); return
                else:
                    new[key] = s
            if new["ocr_poll_sec"] < 0.3:
                err.config(text="OCR poll must be at least 0.3s"); return
            if not (0 <= new["reset_hour_et"] <= 23):
                err.config(text="Reset hour must be 0–23 (US Eastern; 20 = 8 PM)"); return
            self.cfg.update(new)
            save_config(self.cfg)
            self.watcher.hotkeys = self._hotkey_map()   # apply live (watcher reads this each poll)
            self.set_status("Settings saved", GOOD)
            self._style_modes()
            dlg.destroy()

        # ---- Capture Doctor re-run: the fix-it button for "I changed my monitor/resolution/GUI
        #      scale and now chests don't read" — same wizard as first launch, runnable anytime ----
        tk.Frame(dlg, bg=BORDER, height=1).pack(fill="x", padx=14, pady=(14, 0))
        wrow = tk.Frame(dlg, bg=BG); wrow.pack(fill="x", padx=14, pady=(10, 0))
        self._btn(wrow, "🩺 Re-run setup (Capture Doctor)",
                  lambda: (dlg.destroy(), self._open_setup_wizard())).pack(side="left")
        tk.Label(dlg, text="Re-measures the best OCR zoom, signal health, and your travel pace for "
                           "this device. Run it after changing monitor, resolution, or GUI scale.",
                 fg=FG3, bg=BG, font=("Segoe UI", 8), wraplength=320, justify="left").pack(
            padx=14, pady=(5, 0), anchor="w")

        # ---- detection reporting: a real, visible button — new users on unusual resolutions hit
        #      chest-read issues early, so this needs to be easy to find (not a buried text link) ----
        tk.Frame(dlg, bg=BORDER, height=1).pack(fill="x", padx=14, pady=(14, 0))
        drow = tk.Frame(dlg, bg=BG); drow.pack(fill="x", padx=14, pady=(10, 0))
        self._btn(drow, "🐞 Report a detection issue", lambda: (dlg.destroy(), self._start_debug())).pack(side="left")
        tk.Label(dlg, text="Chests not reading right at your resolution? This captures a short, panel-only "
                           "clip (with your consent) so the OCR can be tuned — deleted after review.",
                 fg=FG3, bg=BG, font=("Segoe UI", 8), wraplength=320, justify="left").pack(
            padx=14, pady=(5, 0), anchor="w")

        btns = tk.Frame(dlg, bg=BG); btns.pack(fill="x", padx=14, pady=(12, 12))
        self._mkbtn(btns, "Save", save).pack(side="right")
        tk.Button(btns, text="Cancel", command=dlg.destroy, fg=FG2, bg=FIELD, bd=0,
                  padx=9, pady=3, activebackground=RAISED, activeforeground=FG,
                  font=("Segoe UI", 9)).pack(side="right", padx=6)
        self._place_modal(dlg)

    # ---------------- update check ----------------
    def _check_updates(self):
        """Background loop: ask the site for the newest release at launch and every few hours.
        A newer build LIGHTS the green ⬇ title-bar button (Discord-style — no modal ambush);
        clicking it opens the update dialog. Only for the frozen .exe — from source you just
        `git pull`. Silently no-ops on any network/parse error."""
        if not self.cfg.get("check_updates", True) or not FROZEN:
            return
        while True:
            info = latest_release()
            if info:
                tag, url = info
                if parse_version(tag) > parse_version(__version__):
                    self._update_avail = (tag, url)
                    quiet = tag == self.cfg.get("skip_update")
                    self.root.after(0, lambda t=tag, q=quiet: self._show_update_button(t, q))
                else:
                    self._update_avail = None
                    self.root.after(0, self._hide_update_button)
            time.sleep(4 * 3600)  # low-frequency re-check keeps long sessions informed

    def _show_update_button(self, tag, quiet):
        """Reveal (and green-light) the title-bar update button. `quiet` skips the status nag
        for a version the user chose to skip — the button stays available but passive."""
        try:
            if not self.btn_update.winfo_ismapped():
                self.btn_update.pack(side="right", padx=(2, 0), before=self.btn_collapse)
            if not quiet:
                self.set_status("⬇ Update %s is ready — click the green arrow in the title bar" % tag, GOOD)
        except Exception:
            pass

    def _hide_update_button(self):
        try:
            self.btn_update.pack_forget()
        except Exception:
            pass

    def _update_clicked(self):
        if self._update_avail:
            self._offer_update(*self._update_avail)

    def _offer_update(self, tag, url):
        dlg = self._modal("Update available")
        tk.Label(dlg, text="A newer Histatu Runner is available.", fg=FG, bg=BG,
                 font=("Segoe UI", 10, "bold")).pack(padx=16, pady=(14, 2), anchor="w")
        tk.Label(dlg, text="You have %s — latest is %s." % (__version__, tag), fg=FG3, bg=BG,
                 font=("Segoe UI", 9)).pack(padx=16, pady=(0, 2), anchor="w")
        tk.Label(dlg, text="Update now downloads the new version and restarts the app\n"
                           "in place (your settings stay in capture_config.json).",
                 fg=FG3, bg=BG, font=("Segoe UI", 8), justify="left").pack(padx=16, anchor="w")

        def download(*_):
            dlg.destroy()
            threading.Thread(target=self._self_update_bg, args=(tag, url), daemon=True).start()

        def skip(*_):
            self.cfg["skip_update"] = tag; save_config(self.cfg); dlg.destroy()
            self._hide_update_button()  # skipped — the button re-appears for the NEXT version

        btns = tk.Frame(dlg, bg=BG); btns.pack(fill="x", padx=16, pady=(10, 14))
        self._mkbtn(btns, "⬆ Update now", download).pack(side="right")
        tk.Button(btns, text="Later", command=dlg.destroy, fg=FG2, bg=FIELD, bd=0,
                  padx=9, pady=3, activebackground=RAISED, activeforeground=FG,
                  font=("Segoe UI", 9)).pack(side="right", padx=6)
        tk.Button(btns, text="Skip %s" % tag, command=skip, fg=FG3, bg=BG, bd=0,
                  activebackground=SURFACE, activeforeground=FG,
                  font=("Segoe UI", 8)).pack(side="left")
        self._place_modal(dlg, modal=False)

    def _resolve_download_url(self, url):
        """Ask our own endpoint for the download and return the URL to actually fetch from.
        The endpoint answers with a 302 to a third-party signed URL; redirects are NOT auto-
        followed, and the returned Location is fetched separately."""
        headers = {"User-Agent": "HistatuRunner/" + __version__}
        req = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=60) as r:
                loc = r.headers.get("Location")
                status = getattr(r, "status", None) or r.getcode()
                if status in (301, 302, 303, 307, 308) and loc:
                    return loc
                return url  # no redirect (shouldn't happen) — fetch the original, keyless
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc:
                return loc
            raise

    def _self_update_bg(self, tag, url):
        """Download the new exe next to this one, swap names (the running binary can be renamed,
        not overwritten), relaunch, and exit. Any failure falls back to the download page."""
        new_path = os.path.join(APP_DIR, "HistatuRunner.new.exe")
        try:
            exe = os.path.abspath(sys.argv[0] or sys.executable)
            self.set_status("⬇ Downloading %s…" % tag)
            # Resolve the site's 302 to GitHub's signed asset URL WITHOUT following it with the
            # editor key attached — the key gates our own endpoint only and must never ride the
            # redirect to GitHub's CDN. So send the key just to our host, then fetch the signed
            # URL bare.
            dl_url = self._resolve_download_url(url)
            req = urllib.request.Request(dl_url, headers={"User-Agent": "HistatuRunner/" + __version__})
            with urllib.request.urlopen(req, timeout=120) as r, open(new_path, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        self.set_status("⬇ Downloading %s… %d%%" % (tag, got * 100 // total))
            if total and got != total:
                raise RuntimeError("incomplete download (%d of %d bytes)" % (got, total))
            if os.path.getsize(new_path) < 5000000:
                raise RuntimeError("download too small to be the app")
            self.set_status("Installing %s and restarting…" % tag, GOOD)
            bak = self_update_swap(exe, new_path)
            try:
                # smoke-test the new binary before trusting it, then hand over
                chk = subprocess.run([exe, "--version"], capture_output=True, timeout=60)
                if chk.returncode != 0:
                    raise RuntimeError("new build failed its startup check")
                subprocess.Popen([exe], cwd=APP_DIR, close_fds=True)
            except Exception:
                # roll back — NEVER leave a broken binary under the app's name
                try:
                    os.remove(exe)
                    os.rename(bak, exe)
                except Exception:
                    pass
                raise
            self.root.after(0, self.root.destroy)
        except Exception as e:
            try:
                os.remove(new_path)
            except Exception:
                pass
            self.set_status("⚠ Self-update failed (%s) — opening the download page instead" % e, WARN)
            try:
                webbrowser.open(APP_PAGE_URL)
            except Exception:
                pass

    def _drag_start(self, e):
        self._dragged = False
        self._off = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        self._dragged = True
        self._docked = False  # user positioned it — stop auto-snapping to the corner
        self.root.geometry("+%d+%d" % (e.x_root - self._off[0], e.y_root - self._off[1]))

    def _save_geom(self):
        # persist only a real manual placement, and only when it actually changed
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            if x == self.cfg.get("win_x") and y == self.cfg.get("win_y"):
                return
            self.cfg["win_x"], self.cfg["win_y"] = x, y
            save_config(self.cfg)
        except Exception:
            pass

    # ---------------- collapse / docking ----------------
    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        self.cfg["collapsed"] = self.collapsed
        save_config(self.cfg)
        self._apply_collapsed()
        if self._docked:  # width changed — keep it pinned to the corner
            self.dock()

    def _apply_collapsed(self):
        if self.collapsed:
            self.controls.pack_forget()
            self.status.config(wraplength=230)
        else:
            self.controls.pack(fill="x", before=self.eta_label)
            self.status.config(wraplength=280)
        self.btn_collapse.config(text="▸" if self.collapsed else "▾")

    def dock(self, cached=False):
        """Snap the overlay to a corner of the game window. When it's excluded
        from capture (Windows) it goes top-right, right over the F7 coordinates;
        otherwise it goes bottom-right, clear of the OCR regions so it can't
        cover the panel the tool reads. `cached` reads the poller-refreshed game
        bbox instead of enumerating windows (used on the render tick)."""
        self.root.update_idletasks()
        if cached:
            bbox = self._game_bbox
        else:
            bbox = find_game_window(self.cfg["window_title"])
            self._game_bbox = bbox
        w = self.root.winfo_width() or 300
        h = self.root.winfo_height() or 120
        top = getattr(self, "_capture_excluded", False)
        vx, vy, vw, vh = self._virtual_bounds()
        if bbox:
            x = bbox[2] - w - 8
            y = (bbox[1] + 8) if top else (bbox[3] - h - 8)
        else:  # no game window — default to a corner of the primary monitor
            x = self.root.winfo_screenwidth() - w - 12
            y = 12 if top else (self.root.winfo_screenheight() - h - 48)
        # keep it on a monitor (clamp to the virtual desktop, which spans all of them)
        x = min(max(x, vx), vx + vw - w)
        y = min(max(y, vy), vy + vh - h)
        self.root.geometry("+%d+%d" % (x, y))
        self._docked = True  # auto-dock is NOT persisted; only manual drags are

    def _virtual_bounds(self):
        """Full multi-monitor desktop rectangle (x, y, w, h)."""
        if IS_WIN:
            try:
                gsm = ctypes.windll.user32.GetSystemMetrics
                return gsm(76), gsm(77), gsm(78), gsm(79)  # SM_*VIRTUALSCREEN
            except Exception:
                pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _place_window(self):
        x, y = self.cfg.get("win_x"), self.cfg.get("win_y")
        self.root.update_idletasks()
        vx, vy, vw, vh = self._virtual_bounds()
        w = self.root.winfo_width() or 300
        # trust a saved position only if the (draggable) title bar still lands on a
        # monitor — a display may have been disconnected since; else re-dock
        if isinstance(x, int) and isinstance(y, int) and \
           vy - 10 <= y <= vy + vh - 40 and vx - w + 80 <= x <= vx + vw - 80:
            self.root.geometry("+%d+%d" % (x, y))
            self._docked = False
        else:
            self.dock()  # first run / off-screen — default to the corner

    def set_status(self, text, color=FG2):
        self.root.after(0, lambda: self.status.config(text=text, fg=color))

    def _save_ign(self):
        self.cfg["ign"] = self.ign_var.get().strip()
        save_config(self.cfg)
        if not self.cfg["ign"] and self.mode in ("run", "verify"):
            self.set_mode("idle")
            self.set_status("Set your IGN first — cooldown tracking needs it", WARN)

    def _open_signin(self):
        """Open the website's sign-in page in the browser. The user approves on Hytale's own site,
        copies the key it issues, and pastes it into the 🔑 box here."""
        try:
            webbrowser.open(SITE_BASE + "/?signin=1")
            self.set_status("Opened the website — sign in with Hytale, then paste your key into 🔑", GOOD)
        except Exception:
            self.set_status("Open %s and sign in, then paste your key into 🔑" % SITE_BASE, WARN)

    def _save_key(self):
        self.cfg["write_key"] = self.key_var.get().strip()
        save_config(self.cfg)
        self.client.key_valid = None  # unknown until the server re-checks it
        if not self.cfg["write_key"] and self.mode in ("record", "verify"):
            self.set_mode("idle")
        self._style_modes()  # unlock/lock editor buttons + refresh the editor badge
        threading.Thread(target=self._revalidate_key, daemon=True).start()

    def _revalidate_key(self):
        """Check the freshly-entered key against the server so the badge is honest and finds get
        routed correctly (a rejected key submits them as pending rather than losing them)."""
        try:
            self.client.refresh()
        except Exception:
            return
        self._style_modes()
        if self.cfg["write_key"].strip():
            me = self.client.me
            if me and me.get("ign"):
                self.set_status("✓ Signed in as %s (%s)" % (me["ign"], me.get("role", "player")), GOOD)
            else:
                self.set_status("Key not recognised — sign in on the website (🔑) to get your key", WARN)

    def _update_stats(self):
        """Available/total chests (from the shared per-player open log, so it
        survives restarts) plus this session's opens."""
        total = avail = 0
        for e in self.client.chests():
            total += 1
            if not self.client.chest_on_cooldown(e):
                avail += 1
        # build the readout as grouped LINES rather than one long dotted string, so it stays
        # legible in the compact overlay
        lines = []
        if not self.cfg["ign"].strip():
            lines.append("chests mapped: %d  (set IGN for cooldowns)" % total)
        else:
            # always show the daily-reset countdown (not just when chests are locked), so you always
            # know how long you have — the ⏰ flags the last 15 minutes
            left = max(0, self.client.next_reset_epoch() - time.time())
            soon = left <= 900
            lines.append("chests: %d/%d up  ·  %sreset %dh%02dm"
                         % (avail, total, "⏰ " if soon else "",
                            int(left // 3600), int(left % 3600 // 60)))
        zs = self._zone_stats()
        if zs:
            zone, up, mapped, und = zs
            parts = []
            if up is not None:
                parts.append("%d up" % up)
            if und is not None:
                parts.append("%d to find" % und)
            if parts:
                lines.append("%s:  %s" % (zone, "  ·  ".join(parts)))
        session = []
        if self.session_opens:
            dur = int(time.time() - self.session_start)
            dtxt = ("%dh%02dm" % (dur // 3600, dur % 3600 // 60)) if dur >= 3600 else ("%dm" % (dur // 60))
            s = "session %s: %d opened" % (dtxt, self.session_opens)
            if dur >= 300:  # a chests/hour rate only means something after a few minutes
                s += " (%d/h)" % int(round(self.session_opens / (dur / 3600.0)))
            session.append(s)
        if self.session_legs:
            session.append("%d legs timed" % self.session_legs)
        if self.session_best_run is not None:  # fastest route this session
            session.append("best %d:%02d" % (self.session_best_run // 60, self.session_best_run % 60))
        np = len([p for p in self.client.pendings() if p.get("kind") != "zone"])
        if np and self.client.can_edit():  # zone flags are handled on the site, not in game
            session.append("%d pending 🔎" % np)
        if session:
            lines.append("  ·  ".join(session))
        txt = "\n".join(lines)
        self.root.after(0, lambda: self.stats.config(text=txt))

    # ---------------- modes ----------------
    def set_mode(self, mode):
        if mode == self.mode:
            mode = "idle"  # buttons toggle off
        if mode in ("record", "verify") and not self.client.can_edit():
            self.set_status("%s needs an editor write key (paste it above)" %
                            ("Recording routes" if mode == "record" else "Verifying"), WARN)
            self._style_modes()
            return
        if self.mode == "record" and mode != "record" and self.record_nodes:
            if not self._finish_record():  # publish failed — stay in record so nothing is lost
                self._style_modes()
                return
        if mode in ("run", "verify") and not self.cfg["ign"].strip():
            self.set_status("Set your IGN first — cooldown tracking needs it", WARN)
            self._style_modes()
            return
        self.mode = mode
        if mode == "record":
            nm = self._prompt("Record route", "Name this route (leave blank to name it at the end):", "")
            if nm is None:                      # cancelled — don't enter record mode
                self.mode = "idle"; self._style_modes(); return
            self.record_nodes = []
            self.record_name = nm.strip()[:60]
            self.set_status(("Recording “%s”" % self.record_name if self.record_name
                             else "Recording a route") +
                            " — open chests in order; press the button again to finish")
        elif mode == "log":
            self.set_status(("Logging chests" if self.client.can_edit() else
                             "Submitting chests for review") + " — open them with %s" % self.cfg["hotkey_chest"])
        elif mode == "verify":
            self.set_status("Verifying — open a chest at a pending request (see the map) to confirm it")
        elif mode == "idle":
            self.set_status("Idle")
        self._style_modes()

    def _style_modes(self):
        # may be reached from worker/poller threads via set_mode — marshal to the UI thread
        self.root.after(0, self._style_modes_ui)

    def _style_modes_ui(self):
        editor = self.client.can_edit()
        self.btn_log.config(text="⏺ Log" if editor else "⏺ Submit")
        self.btn_rec.config(text="✔ Finish" if self.mode == "record" else "🧭 Record")
        n = len(self.client.pendings())
        self.btn_verify.config(text="🔎 Verify" + (" %d" % n if n else ""))
        for b in (self.btn_log, self.btn_rec, self.btn_verify):
            self._paint_seg(b)
        # Run button doubles as Stop while running
        running = self.mode == "run"
        self.btn_run.config(text="■ Stop" if running else "▶ Run",
                            bg=BAD if running else ACCENT, fg="#fff" if running else ACCENT_FG)
        self.btn_run._base = BAD if running else ACCENT
        self.btn_run._hot = "#f28080" if running else "#54d99a"
        self._style_pause()  # show/hide the Pause/Resume control with the run
        if getattr(self, "key_lbl", None):
            c = self.client
            me = c.me
            if me and me.get("ign"):
                role = me.get("role", "player")
                self.key_lbl.config(text="✓ %s" % me["ign"] + ("" if role == "player" else " · " + role),
                                    fg=ACCENT if role in ("editor", "owner") else GOOD)
            elif c.has_key():
                self.key_lbl.config(text="✗ key not recognised", fg=BAD)
            else:
                self.key_lbl.config(text="○ not signed in", fg=FG3)
        # a small colored status dot: accent while active, dim when idle
        self.dot.config(fg={"idle": FG3}.get(self.mode, ACCENT))
        # poll in EVERY active mode: it keeps a fresh position + target cache so opening a chest
        # still logs when its UI covers the panel
        active = self.mode in ("log", "record", "run", "verify")
        (self.poll_flag.set if active else self.poll_flag.clear)()

    def _toggle_run(self):
        if self.mode == "run":
            self.set_mode("idle")
            return
        routes = self.client.routes()
        pick = self.route_var.get()
        route = next((r for r in routes if r.get("name") == pick), None)
        if not route:
            self.set_status("Pick a route from the list first", WARN)
            return
        if not self.cfg["ign"].strip():
            self.set_status("Set your IGN first — cooldown tracking needs it", WARN)
            return
        if self.mode == "record" and self.record_nodes:
            if not self._finish_record():  # publish failed — stay in record mode
                return
        self.run_route = route
        self.run_done = set()
        self.run_opened = 0
        self.run_started = time.time()
        self.run_paused = False; self.run_paused_total = 0.0
        self.pause_reason = None; self._last_moved = self._pause_banked_to = time.time()
        self.mode = "run"
        self._advance_run(opened=None)
        self._style_modes()

    PAUSE_STILL_SEC = 10.0  # stand still this long and the run auto-pauses (and these seconds don't count)

    def _set_paused(self, on, reason, since=None):
        """Single freeze/unfreeze transition for the run clock. `since` back-dates the pause start
        (used by the stand-still auto-pause: the clock stops as of when you STOPPED moving, so the
        idle seconds — including the detection grace — never count). A back-date is clamped to
        _pause_banked_to, the end of the last banked span: without that, a stand-still pause right
        after a short menu/focus pause could reach back INTO the span just banked and subtract the
        overlap twice (an artificially fast — cheatable — leaderboard time). Returns True if state
        changed."""
        now = time.time()
        if since is not None:
            since = min(max(since, self._pause_banked_to), now)
        if on and not self.run_paused:
            self.run_paused = True
            self.pause_reason = reason
            self._pause_at = since if since is not None else now
            return True
        if on and self.run_paused and self.pause_reason != reason:
            self.pause_reason = reason  # e.g. a manual pause overriding an auto one; span keeps accruing
            if since is not None and since < self._pause_at:
                # e.g. focus/screen -> still in one step: extend the freeze back to when they
                # actually stopped moving (the clamp above keeps it out of banked time)
                self._pause_at = since
            return True
        if not on and self.run_paused:
            self.run_paused_total += now - self._pause_at  # bank the frozen span
            self._pause_banked_to = now                    # watermark for the back-date clamp above
            self.run_paused = False
            self.pause_reason = None
            return True
        return False

    def _toggle_pause(self):
        """The ⏸/▶ button: manual pause/resume. A manual pause outranks the auto-pause (it won't be
        auto-resumed), and resuming re-checks the run bookkeeping."""
        if self.mode != "run":
            return
        if self.run_paused:
            self._set_paused(False, None)
            self._last_moved = time.time()   # don't let a stale idle instantly re-pause on resume
            self._advance_run(opened=None)
            self.set_status("▶ Resumed", GOOD)
        else:
            self._set_paused(True, "manual")
            self.set_status("⏸ Paused — the run timer is frozen. Press ▶ Resume to continue.", WARN)
        self._style_pause()

    def _auto_pause_eval(self):
        """Freeze the run clock when the player clearly isn't running: the game window is unfocused,
        the F7 panel is covered (a menu or the open-chest UI), or they've stood still past the grace.
        Auto-resumes when they move / the panel returns / focus comes back. A MANUAL pause is left
        alone (only the button clears it). Cheap; called from the 50 ms UI loop, throttled."""
        if self.mode != "run" or not self.run_route:
            if self.run_paused and self.pause_reason and self.pause_reason != "manual":
                self._set_paused(False, None)  # left run mode while auto-paused
            return
        if self.pause_reason == "manual":
            return  # the button owns a manual pause
        now = time.time()
        want, reason, since = False, None, None
        try:
            focused = (not self.cfg.get("only_when_game_focused")) or game_focused(self.cfg["window_title"])
        except Exception:
            focused = True
        if not focused:
            want, reason = True, "focus"
        elif self.hud.misses >= 2:                      # panel unreadable => a menu / chest UI is up
            want, reason = True, "screen"
        elif now - self._last_moved > self.PAUSE_STILL_SEC:
            want, reason, since = True, "still", self._last_moved  # back-date: idle time doesn't count
        if want:
            if self._set_paused(True, reason, since):
                self._style_pause()
        elif self.run_paused:                            # condition cleared -> resume
            if self._set_paused(False, None):
                self._advance_run(opened=None)
                self._style_pause()

    def _setup_nag_check(self):
        """Capture-health regression hint (from the 50 ms tick): setup measured a working panel,
        but reads are now failing hard — the classic "changed resolution / monitor / GUI scale"
        signature. Once per session; >= because two OCR threads can push misses past 12 between
        ticks; 12 consecutive misses is far past the escalating rescans, so it's a real break."""
        if self._setup_nag or self.hud.misses < 12 or self.mode == "idle":
            return
        self._setup_nag = True
        try:  # setup_health is user-editable JSON — any shape must fail quiet, not break the tick
            base = ((self.cfg.get("setup_health") or {}).get("panel") or {}).get("fields") or {}
            healthy_baseline = isinstance(base, dict) and base.get("position", 0) > 0.5
        except Exception:
            healthy_baseline = False
        if healthy_baseline:
            self.set_status("⚠ The panel isn't reading like it did at setup — changed resolution "
                            "or UI scale? Re-run 🩺 Setup in ⚙ Settings.", WARN)

    def _style_pause(self):
        """Show the Pause/Resume button only during a run, labelled for the current state."""
        try:
            if self.mode == "run":
                self.btn_pause.config(text="▶ Resume" if self.run_paused else "⏸ Pause")
                if not self.btn_pause.winfo_ismapped():
                    self.btn_pause.pack(side="left", padx=(6, 0))
            elif self.btn_pause.winfo_ismapped():
                self.btn_pause.pack_forget()
        except Exception:
            pass

    def _worker(self):
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "undo":
                    self._do_undo()
                elif job[0] == "chest":
                    self._do_chest()
                elif job[0] == "resetcd":
                    self._do_reset_cooldowns()
                elif job[0] == "hudopen":
                    cc, det_t, area = job[1], job[2], job[3]
                    # final dedup on the worker thread: the F-key path may have completed while
                    # the HUD confirmation was pending
                    if self._last_open and abs(self._last_open[1] - det_t) <= 12:
                        pass
                    else:
                        # the row that moved names the area more reliably than the title read
                        self.client.current_area = area or self.client.current_area
                        self._last_target = None  # single-use, like the covered-panel path
                        self._on_chest_open(*cc)
                        self.set_status("✓ HUD counter caught an open the panel missed — "
                                        "logged @ %d %d %d (%s)" % (cc[0], cc[1], cc[2], area), GOOD)
            except Exception as e:
                beep("err")
                self.set_status("⚠ %s" % e, BAD)

    def _do_undo(self):
        if not self.undo_stack:
            self.set_status("Nothing to undo")
            return
        mid, label = self.undo_stack.pop()
        self.client.delete(mid)
        self.count = max(0, self.count - 1)
        self.record_nodes = [n for n in self.record_nodes if n["id"] != mid]
        beep("dup")
        self.set_status("Removed " + label)

    def _do_chest(self):
        if self.mode == "idle":
            return
        at = self.dr.snapshot()  # latency compensation for the fix below
        frame = grab_game(self.cfg)
        hud = self.hud.read(frame, want_target=True, thorough=True)  # try hard to read the target
        synced = bool(hud["position"])
        if self._debug is not None:  # detection-report capture active: grab this press's frame
            self._debug_capture(frame, hud)
        if synced:
            self.dr.sync(hud["position"], hud["yaw"], at=at, yaw_exact=hud["yaw_exact"],
                         speed=hud.get("speed"))
        t = hud["target"]
        c = chest_coords(t)
        # a chest you just opened is a few blocks away; coords far from the player's (dead-
        # reckoned) position are an OCR misread — another panel line's numbers, a decimal read
        # as a comma — so drop them rather than log a chest at the wrong spot on the shared map.
        # Gated on dr.pos (not just a same-frame position read): a frame whose Position line
        # failed to parse must not skip the sanity check. Snapshot pos ONCE — the poller thread
        # may set it mid-function, and the gate and the refusal below must agree on what they saw.
        pos_est = self.dr.pos
        if c and pos_est is not None and not self._target_plausible(c, pos=pos_est, fresh_y=synced):
            beep("err")
            dist = math.hypot(c[0] - pos_est[0], c[2] - pos_est[2])
            self.set_status("⚠ Read a chest at %d %d %d but you're not near it (%d blocks) — "
                            "likely a misread; not logged. Re-aim with the F7 panel visible "
                            "and open it again." % (c[0], c[1], c[2], int(dist)), WARN)
            self._log_read_debug("REJECTED far target %s (pos %s, dist %.1f)" % (c, pos_est, dist), hud)
            return
        if c and pos_est is None:
            # no position estimate at all (fresh launch + unreadable Position line): the read
            # can't be sanity-checked, and an unverifiable coordinate must not reach the shared map
            beep("err")
            self.set_status("⚠ Read a chest at %d %d %d but have no position fix to check it "
                            "against — keep the F7 panel visible and open it again" % c, WARN)
            self._log_read_debug("REFUSED no-position target %s" % (c,), hud)
            return
        if c:
            self._last_target = {"coords": c, "at": time.time()}  # keep the fallback cache warm
            # periodically record the raw OCR of a LOGGED chest too (not just failures), so a
            # "logged at the wrong spot" report comes with the exact text OCR saw — that's how
            # every prior detection fix got nailed. Throttled so it can't spam the log.
            nowl = time.time()
            if nowl - self._success_log_t > 20:
                self._success_log_t = nowl
                self._log_read_debug("LOGGED direct read %s (pos %s)" % (c, pos_est), hud)
            self._on_chest_open(*c)
            return
        if t and t.get("block"):
            return  # a readable, NAMED non-chest block (door/lever) — F is the game's interact
                    # key, so ignore it rather than guessing
        if t and not t.get("block") and t.get("coords") and pos_est is not None:
            # coords parsed but no name anywhere (4K panels print none in-panel and the nameplate
            # can be cropped/misread). NEVER log a NEW chest from a nameless read — but coords
            # matching a KNOWN chest location (a marker, a pending request, or the guided target)
            # are safely an open of that chest.
            try:
                tc = tuple(int(round(v)) for v in t["coords"])
            except (TypeError, ValueError):
                tc = None
            if tc and self._target_plausible(tc, pos=pos_est, fresh_y=synced):
                known = self.client.marker_at(*tc) or self.client.pending_at(*tc)
                if known:
                    self._on_chest_open(*tc)
                    return
            # nameless AND unknown — same footing as a covered panel: fall through to the
            # spatially-gated fallback below rather than silently dropping the open
        # nothing readable — the chest UI probably covered the F7 panel. Fall back to the chest we
        # can be confident you just opened (see _covered_fallback), never a guessed one.
        cc = self._covered_fallback(time.time())
        if cc:
            self._last_target = None   # single-use: a stale sighting can't seed several opens
            self._on_chest_open(*cc)
            self.set_status("Logged from your last reading — the chest screen covered the coordinates")
            return
        self.set_status("Couldn't read the coordinates (chest screen covering them?) — "
                        "aim at the chest first, or set a Log-by-aim key in ⚙")
        self._log_read_debug("UNREADABLE (no target parsed, no usable fallback)", hud)

    def _log_read_debug(self, reason, hud):
        """Append the raw OCR of a failed chest press to read_debug.log next to the app — so an
        'it won't detect the chest' report comes with the exact text the OCR saw, not guesswork."""
        try:
            path = os.path.join(APP_DIR, "read_debug.log")
            if os.path.exists(path) and os.path.getsize(path) > 200000:
                os.replace(path, path + ".1")  # keep at most ~2x200KB of history
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] %s | mode=%s pos=%s target=%s\n" %
                        (time.strftime("%H:%M:%S"), reason, self.mode, self.dr.pos, hud.get("target")))
                for tx in (hud.get("texts") or [])[-3:]:  # the last (largest-region) OCR attempts
                    f.write("    ocr: %s\n" % tx[:400])
        except Exception:
            pass  # diagnostics must never break chest handling

    def _offer_full_upgrade(self):
        """Lite build: the detection reporter isn't included. Explain why and offer the Full
        build, which adds it. Nothing is captured or sent from Lite."""
        if messagebox.askyesno(
                "Detection reporting — Full version",
                "You're running Histatu Runner Lite, which never captures or sends any "
                "screenshots or logs.\n\n"
                "Reporting a detection issue (a short, panel-region-only screenshot capture that "
                "helps fix chests the tool can't read) is part of the Full version.\n\n"
                "Open the download page to get the Full version?"):
            try:
                webbrowser.open(APP_PAGE_URL)
            except Exception:
                pass

    # ---------------- detection-issue report (user-initiated, time-limited) ----------------
    def _start_debug(self):
        """Begin a ~3-minute capture: each chest press records the top-right game panel region
        + the parse result; at the end the bundle uploads for review. Explicit, labeled, and it
        captures only that panel region — not the full screen — and never the editor key.
        The uploaded log is built DURING the session from strip-region OCR only; the on-disk
        read_debug.log (which can hold full-frame rescan text) is never uploaded."""
        if IS_LITE:
            self._offer_full_upgrade()
            return
        if self._debug is not None:
            self._finish_debug(manual=True)
            return
        if not messagebox.askokcancel(
                "Report a detection issue",
                "This helps fix chests the tool can't read. For about 3 minutes it captures, "
                "on each chest press:\n\n"
                "  • a screenshot of ONLY the top-right game panel region (where the F7\n"
                "     coordinates show) — never your full screen, chat, or other windows\n"
                "  • the text the tool read from that panel region, and what it parsed\n"
                "     (the coordinates it saw)\n\n"
                "If the game window can't be found, nothing is captured — the report just\n"
                "says so. When the 3 minutes are up it's sent for review.\n\n"
                "What is NOT included:\n"
                "  • your editor key or any password\n"
                "  • anything outside that panel region\n\n"
                "Where it goes: a PRIVATE GitHub report (not stored on the website), and it's\n"
                "permanently DELETED once it's been reviewed.\n\n"
                "After you click OK, go open the chest that won't detect. Start?"):
            return
        d = {"until": time.time() + DEBUG_WINDOW_SEC, "frames": [], "bytes": 0, "res": "",
             "log": ""}
        self._debug = d
        # the timer finishes only ITS OWN session — a manual finish + restart must not let the
        # stale timer cut the new session short
        self.root.after(DEBUG_WINDOW_SEC * 1000, lambda: self._finish_debug(expected=d))
        self.set_status("🐞 Debug capture ON (~3 min) — open the chest that won't detect. "
                        "It sends automatically when done.", WARN)

    def _debug_log(self, d, line):
        if len(d["log"]) < 30000:
            d["log"] += "[%s] %s\n" % (time.strftime("%H:%M:%S"), line)

    def _debug_capture(self, frame, hud):
        d = self._debug
        if d is None or time.time() > d["until"] or len(d["frames"]) >= DEBUG_MAX_FRAMES \
                or d["bytes"] >= DEBUG_MAX_BYTES:
            return
        try:
            # only ship pixels that verifiably came from the game window: when it can't be
            # located, grab_game() falls back to the full desktop and the "strip" crop would be
            # the top-right of the user's SCREEN — record the fact instead of the pixels.
            bbox = find_game_window(self.cfg["window_title"])
            if not bbox or (frame.width, frame.height) != (bbox[2] - bbox[0], bbox[3] - bbox[1]):
                self._debug_log(d, "game window not located (title=%r) — pixels NOT captured"
                                % self.cfg["window_title"])
                return
            d["res"] = "%dx%d" % (frame.width, frame.height)
            strip = self.hud._strip(frame)
            if strip.width > 900:
                sc = 900.0 / strip.width
                strip = strip.resize((900, max(1, int(strip.height * sc))), Image.LANCZOS)
            buf = io.BytesIO()
            strip.convert("RGB").save(buf, "JPEG", quality=82)
            jpg = buf.getvalue()
            note = "pos=%s target=%s chest=%s" % (hud.get("position"), hud.get("target"),
                                                  chest_coords(hud.get("target")))
            d["frames"].append({"note": note[:300],
                                "jpg": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()})
            d["bytes"] += len(jpg)
            self._debug_log(d, note[:300])
            for tx in (hud.get("strip_texts") or [])[-2:]:  # panel-region OCR text only
                self._debug_log(d, "    ocr: %s" % tx[:400])
        except Exception:
            pass  # capture is best-effort — never disrupt a chest press

    def _finish_debug(self, manual=False, expected=None):
        d = self._debug
        if d is None or (expected is not None and d is not expected):
            return  # the timer's session already ended manually — don't touch a newer one
        self._debug = None
        if not d["frames"] and not d["log"]:
            self.set_status("🐞 Debug capture ended — no chest presses were recorded. Start it "
                            "again, then open the problem chest.", WARN)
            return
        self.set_status("🐞 Sending detection report (%d frame%s)…"
                        % (len(d["frames"]), "" if len(d["frames"]) == 1 else "s"))
        threading.Thread(target=self._upload_debug,
                         args=(d["frames"], d.get("res", ""), d.get("log", "")),
                         daemon=True).start()

    def _upload_debug(self, frames, res, log=""):
        try:
            # a compact, secret-free config summary — the useful knobs, never the write key
            g = self.cfg.get
            note = ("IGN=%s mode=%s | poll=%s reset_hour_et=%s window=%s | keys chest=%s log=%s "
                    "| move_speed=%s dry=%s"
                    % (g("ign", ""), self.mode, g("ocr_poll_sec"), g("reset_hour_et"),
                       g("window_title"), g("hotkey_chest"), g("hotkey_log"),
                       g("move_speed"), g("dry_run")))
            bundle = {"version": __version__,
                      "platform": "%s%s" % (sys.platform, " wayland" if IS_WAYLAND else ""),
                      "resolution": res, "note": note[:500],
                      "log": log, "frames": frames}
            data = json.dumps(bundle).encode()
            req = urllib.request.Request(DEBUG_UPLOAD_URL, data=data, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "HistatuRunner/" + __version__})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode() or "{}")
            self.set_status("🐞 Detection report sent — thank you! (id %s)"
                            % str(res.get("id", "?")), GOOD)
        except Exception as e:
            self.set_status("⚠ Couldn't send the report (%s). read_debug.log next to the app "
                            "still has the details." % e, BAD)

    def _near(self, pos, yaw, coords):
        """Is the player dead-reckoned to be right at `coords` (and, if heading is known, facing
        it)? Guards the covered-panel fallback against logging a chest you've walked past."""
        cx, cy, cz = coords
        if math.hypot(cx - pos[0], cz - pos[2]) > FALLBACK_MAX_DIST:
            return False
        if abs(cy - pos[1]) > FALLBACK_MAX_DIST:  # a chest stacked above/below isn't the one you opened
            return False
        if yaw is not None and abs(wrap_deg(bearing_to(pos[0], pos[2], cx, cz) - yaw)) > FALLBACK_MAX_ANGLE:
            return False
        return True

    def _target_plausible(self, c, pos=None, fresh_y=True):
        """Sanity check for a directly-read target chest: it must be within TARGET_MAX_DIST of the
        player position (a targeted block always is). Catches OCR misreads that produce a far-off
        coordinate. Dead reckoning only advances X/Z between polls — Y changes (a pit drop right
        before the open) are invisible until the next position read — so a frame WITHOUT its own
        fresh position gets a wider Y budget instead of misdiagnosing the drop as a misread."""
        pos = self.dr.pos if pos is None else pos
        if pos is None:
            return True  # no position to compare against — trust the read
        y_max = TARGET_MAX_Y if fresh_y else TARGET_MAX_Y * 2.5
        return math.hypot(c[0] - pos[0], c[2] - pos[2]) <= TARGET_MAX_DIST and abs(c[1] - pos[1]) <= y_max

    def _covered_fallback(self, now):
        """Best guess at the chest just opened when the on-open OCR came back blank: the chest
        you're actually STANDING AT and FACING per the position estimate. In RUN mode the
        candidates are the route's remaining stops; in LOG/VERIFY the recently-seen aim cache.
        Only ever a chest you're dead-reckoned to be right at, so an adjacent/earlier chest is
        never mis-logged and a chest you didn't open is never skipped. Returns coords or None.

        RECORD mode deliberately gets NO cache fallback: a wrong guess there would be baked into a
        PUBLISHED route (shared data). VERIFY mode never falls back to the pending's crowd-
        submitted coordinates (that would rubber-stamp the very data being verified) — but the
        editor's OWN recent aim-cache reading is their reading, so that one is allowed."""
        pos, yaw = self.dr.pos, self.dr.yaw
        if pos is None:
            return None  # no position estimate — can't confirm you're at any chest

        def nearest_here(coords_iter):
            best = None
            for wx, wy, wz in coords_iter:
                if self._near(pos, yaw, (wx, wy, wz)):
                    d = math.hypot(wx - pos[0], wz - pos[2])
                    if best is None or d < best[0]:
                        best = (d, (wx, wy, wz))
            return best[1] if best else None

        if self.mode == "run" and self.run_route:
            # the remaining route stop you're standing at and facing
            return nearest_here((wx, wy, wz) for _n, wx, wy, wz
                                in self._run_stop_coords(remaining_only=True))
        if self.mode in ("log", "verify"):
            lt = self._last_target
            if lt and (now - lt["at"]) <= FALLBACK_MAX_AGE and self._near(pos, yaw, lt["coords"]):
                return lt["coords"]
        return None

    def _learn_leg(self, coords, now):
        """Record the travel leg from the last opened chest to `coords`: min-wins, bounded to
        1..travel_max_sec (drops sub-second noise and long AFK/combat gaps). It keys on the
        coordinate PAIR, so it is order-INDEPENDENT and mode-INDEPENDENT — it fires for every
        consecutive pair of real opens in log / record / run / verify (never idle, where
        no chest is opened). Reads self._last_open as the previous open; the CALLER updates the
        anchor afterward. Returns a short " · Ns from last" note, or "".
        """
        prev = self._last_open
        coords = (int(coords[0]), int(coords[1]), int(coords[2]))
        if not (prev and tuple(prev[0]) != coords):
            return ""
        dt = now - prev[1]
        if not (1 <= dt <= float(self.cfg["travel_max_sec"])):
            return ""
        try:
            self.client.observe_travel(prev[0], coords, dt)
            self.client.flush_travel()
            self.session_legs += 1
            return " · %ds from last" % int(round(dt))
        except Exception:
            return ""  # observations stay queued (flush_travel re-queues on the next success)

    def _on_chest_open(self, wx, wy, wz):
        route_id = self.run_route["id"] if self.mode == "run" and self.run_route else None
        if route_id in LOCAL_ROUTE_IDS:
            route_id = None  # local synthetic route (⚡/🧭) — not a shareable id
        already = self.client.on_cooldown(wx, wy, wz)
        tracked = bool(self.cfg["ign"].strip())
        now = time.time()
        # Opening a chest is unambiguous activity: it clears any *automatic* pause (menu / idle /
        # unfocused) so this open still counts and the run resumes. A manual pause is left alone —
        # only the ▶ button clears that. Must run BEFORE the run_opened guard below.
        self._last_moved = now
        if self.mode == "run" and self.run_paused and self.pause_reason != "manual":
            if self._set_paused(False, None):
                self._style_pause()
        leg_note = ""
        if not already:
            # A chest on cooldown is LOCKED in game (everything relocks until the daily 8 PM ET
            # reset — never a per-chest timer) — pressing F on it opens nothing, so nothing may
            # count: not the session/run counters, not the shared open log, and not the
            # travel-time table. The logging flow below still runs.
            try:
                tracked = self.client.record_open(wx, wy, wz, route_id)
            except Exception:
                # the opens POST hiccuped (blip/429). The open is already merged into the local
                # log and the NEXT successful record_open re-sends the whole snapshot, so it
                # self-heals — never let it abort the marker/pending logging below.
                tracked = bool(self.cfg["ign"].strip())

            # EVERY consecutive pair of real chest opens teaches the travel-time table, in any mode
            # and regardless of order (see _learn_leg — keyed by the coordinate pair, so an
            # out-of-order run leg A→C→B records A→C and C→B).
            leg_note = self._learn_leg((wx, wy, wz), now)
            self._last_open = ((wx, wy, wz), now)
            self.session_opens += 1
            self._zone_check(wx, wy, wz)  # boundary sanity: in-game vs drawn-area mismatches
            if self.mode == "run" and not self.run_paused:
                self.run_opened += 1
        self._update_stats()

        if self.mode == "verify":
            # editor confirming a crowd-sourced request: opening a chest at a
            # pending spot promotes it to a real marker; anything else logs normally
            self._confirm_here(wx, wy, wz, leg_note)
            self._advance_run(opened=(wx, wy, wz))
        elif self.mode in ("log", "record"):
            marker = self.client.marker_at(wx, wy, wz)
            if not marker:
                marker = self._log_chest(wx, wy, wz, leg_note)
                if not marker:
                    return  # submitted as a pending request (or failed) — no marker to route through
            elif self.mode == "log":
                beep("dup")
                self.set_status("Chest already mapped (%d %d %d)%s — %s" % (wx, wy, wz, leg_note,
                                "still on cooldown, not counted" if already else
                                ("open recorded" if tracked else "open NOT tracked (no IGN set)")), WARN)
            if self.mode == "record" and marker:
                if self.record_nodes and self.record_nodes[-1]["id"] == marker["id"]:
                    return  # same chest twice in a row
                self.record_nodes.append({"id": marker["id"], "t": time.time()})
                beep("next")
                self.set_status("Stop %d recorded @ %d %d %d%s" %
                                (len(self.record_nodes), wx, wy, wz, leg_note), GOOD)
        elif self.mode == "run":
            if self.run_paused:
                # paused: the open is still recorded to your cooldown log + travel table above (it
                # really happened in game), but the run neither counts it nor marks the stop done
                return
            if not self.client.marker_at(wx, wy, wz):
                # exploring counts in BOTH modes: unmapped chests found on the way get logged
                # (editor) or, if that can't be saved directly, submitted as a request — never
                # lost. (Run mode used to skip this, silently dropping mid-run finds.)
                self._log_chest(wx, wy, wz, "")
            beep("next")
            self._advance_run(opened=(wx, wy, wz))
            if already:
                self.set_status("Note: that chest was still on cooldown for you", WARN)

    def _log_chest(self, wx, wy, wz, leg_note):
        """Log a newly-found chest without ever losing it. Editors add it to the map directly;
        if that write fails for ANY reason (a rejected editor key, offline, the entry cap, an
        uncalibrated map…) we fall back to submitting it as a PENDING request so the location is
        preserved for an editor to confirm. Returns the marker if added directly, else None."""
        if self.client.can_edit():
            try:
                marker = self.client.add_chest(wx, wy, wz)
                self.count += 1
                self.undo_stack.append((marker["id"], "chest @ %d %d %d" % (wx, wy, wz)))
                self.client.credit(self.cfg["ign"], found=1)  # own find, logged directly
                beep("ok")
                self.set_status("✓ new chest logged @ %d %d %d%s" % (wx, wy, wz, leg_note), GOOD)
                return marker
            except Exception as e:
                # don't drop the find — save it as a pending request instead
                self._submit_here(wx, wy, wz, leg_note, prefix="⚠ %s — saved as pending: " % e)
                return None
        self._submit_here(wx, wy, wz, leg_note)
        return None

    def _submit_here(self, wx, wy, wz, leg_note, prefix=""):
        """Propose this chest for an editor to verify (keyless — always allowed)."""
        try:
            p = self.client.submit_pending(wx, wy, wz)
        except Exception as e:
            # Exception, not just RuntimeError: a connection-level failure (URLError/timeout)
            # must degrade to a status message too, not escape and abort route advancement.
            beep("err"); self.set_status("⚠ %s%s" % (prefix, e), BAD); return None
        if p:
            beep("ok")
            self.set_status("%s✓ chest submitted for verification @ %d %d %d%s" % (prefix, wx, wy, wz, leg_note), GOOD)
        else:
            beep("dup")
            self.set_status("%sAlready mapped or already submitted (%d %d %d)%s" % (prefix, wx, wy, wz, leg_note), WARN)
        return p

    def _confirm_here(self, wx, wy, wz, leg_note):
        """Editor verify path: confirm a pending request at this spot into a marker.
        The chest is logged at the editor's own reading (they're physically here),
        and the request(s) near this spot are cleared."""
        rm = self.client.removal_at(wx, wy, wz)
        if rm and self.client.marker_at(wx, wy, wz):
            # the editor just OPENED the reported-missing chest — it exists; dismiss the report
            self.client.reject_pending(rm)
            beep("ok")
            self.set_status("✓ Chest exists @ %d %d %d — removal report dismissed%s" %
                            (wx, wy, wz, leg_note), GOOD)
            return
        pend = self.client.pending_at(wx, wy, wz)
        if pend:
            try:
                marker = self.client.marker_at(wx, wy, wz) or \
                    self.client.add_chest(wx, wy, wz, found_by=pend.get("by"))
                self.client.reject_pending(pend)
                self.client.credit(pend.get("by"), found=1)  # the submitter located it
                self.count += 1
                self.undo_stack.append((marker["id"], "chest @ %d %d %d" % (wx, wy, wz)))
                beep("ok")
                self.set_status("✓ confirmed pending chest @ %d %d %d%s" % (wx, wy, wz, leg_note), GOOD)
            except RuntimeError as e:
                self.set_status("⚠ %s" % e, WARN)
        elif not self.client.marker_at(wx, wy, wz):
            try:
                m = self.client.add_chest(wx, wy, wz)
                self.count += 1
                self.undo_stack.append((m["id"], "chest @ %d %d %d" % (wx, wy, wz)))
                beep("next")
                self.set_status("✓ logged new chest @ %d %d %d%s" % (wx, wy, wz, leg_note), GOOD)
            except RuntimeError as e:
                self.set_status("⚠ %s" % e, WARN)
        else:
            beep("dup")
            self.set_status("Already mapped (%d %d %d)%s" % (wx, wy, wz, leg_note), WARN)

    # ---------------- game-HUD signals (per-area chest counters + reset countdown) --------
    def _hud_scan(self, frame, now):
        """Track and read the game's MOVABLE chest HUD. Its vertical band is remembered and
        re-OCR'd each scan (full frame width — the horizontal position doesn't matter to OCR);
        when the band goes missing a few times, a slower full-frame rediscovery finds where the
        player moved it. Everything here is opportunistic — it must never break the poll."""
        if now - self._hud_scan_t < 8:
            return
        self._hud_scan_t = now
        try:
            # the HUD text is large at 4K but small enough at 1440p-windowed (2560-wide) to
            # need the 2x upscale — only true 4K frames skip it
            sc = 1 if frame.width >= 3200 else 2
            if self._hud_band:
                y0, y1 = self._hud_band
                top = max(0, y0 - 40)
                crop = frame.crop((0, top, frame.width, min(frame.height, y1 + 60)))
                info = parse_hud_panel([(t, y + top) for t, y in ocr_lines(crop, sc)])
                if info:
                    self._hud_band = (int(info["y0"]), int(info["y1"]))
                    self._hud_miss = 0
                    self._hud_apply(info, now)
                else:
                    self._hud_miss += 1
                    if self._hud_miss >= 3:
                        self._hud_band = None  # panel moved or closed — rediscover
                return
            if now - self._hud_seek_t < 20:
                return  # full-frame discovery is the expensive path — slower cadence
            self._hud_seek_t = now
            info = parse_hud_panel(ocr_lines(frame, sc))
            if info:
                self._hud_band = (int(info["y0"]), int(info["y1"]))
                self._hud_miss = 0
                self._hud_apply(info, now)
        except Exception:
            pass

    def _hud_apply(self, info, now):
        if info.get("reset"):
            self._hud_reset_tick(info["reset"], now)
        self._hud_area = info.get("current")           # best effort — polygons on the map are
        self.client.current_area = self._hud_area      # the real area assignment
        if info.get("areas"):
            self._hud_count_tick(info["areas"], now)
            if len(info["areas"]) >= 2:
                self._push_area_totals(info["areas"], now)

    def _push_area_totals(self, rows, now):
        """Editors publish the HUD's true per-area chest totals (the '/61' side) so the map can
        show mapped-vs-total and 'N undiscovered'. Hardened on purpose:
          * only areas whose boundary POLYGON exists on the map are published — that's all the
            site can display, it keeps OCR bleed-through junk names ('Idle The Hollow') out of
            the shared singleton, and it bounds its growth below the server's key cap;
          * a CHANGED total needs two consecutive readings agreeing before it's pushed, like
            every other HUD signal (a one-frame digit misread never reaches the shared map);
          * stored slugs whose polygon is gone are pruned on the next push (self-healing)."""
        if not self.client.can_edit():
            return
        with self.client.lock:
            known = set()
            for e in self.client.entries.values():
                if isinstance(e, dict) and e.get("type") == "area" and e.get("name"):
                    s = ign_slug(e["name"])
                    if s:
                        known.add(s)
            e = self.client.entries.get("areatotals")
            old = dict(e.get("areas")) if isinstance(e, dict) and isinstance(e.get("areas"), dict) else {}
        fresh = {}
        for name, (_, total) in rows.items():
            slug = ign_slug(name)
            if slug in known:
                fresh[slug] = {"name": name, "total": total}
        changed = {s: v for s, v in fresh.items() if old.get(s, {}).get("total") != v["total"]}
        cand = self._areatotals_cand or {}
        self._areatotals_cand = changed
        confirmed = {s: v for s, v in changed.items() if cand.get(s, {}).get("total") == v["total"]}
        if not confirmed or now - self._areatotals_t < 60:
            return
        self._areatotals_t = now
        merged = {s: v for s, v in old.items() if s in known}  # prune junk / removed areas
        merged.update(confirmed)
        entry = {"id": "areatotals", "type": "areatotals", "areas": merged}
        try:
            if not self.client.dry:
                self.client._req("POST", self.client.api, entry)
            with self.client.lock:
                self.client.entries["areatotals"] = entry
        except Exception:
            pass

    def _hud_count_tick(self, rows, now):
        """The game's own per-area chest counters ('Solmara 58/327', '> The Hollow 44/99') — a
        detection signal independent of the F7 panel. Exactly ONE area moving by exactly +1
        (same total, every other shared row unchanged) means one chest opened IN THAT AREA. A
        candidate is only ACTED on after a SECOND reading confirms it (one misread frame can
        never fire), the location is captured at detection time (fresh aim-cache), and the
        logging runs on the worker queue — never racing the F-key path from the poller thread."""
        prev = self._hud_count
        self._hud_count = (rows, now)
        pend = self._hud_pending_open
        if pend:
            cur = rows.get(pend["area"])
            if cur is None and now - pend["at"] <= 30:
                pass  # the row was missing from THIS frame (OCR flicker) — keep waiting
            else:
                self._hud_pending_open = None
                if cur == pend["pair"] and now - pend["at"] <= 30:
                    # confirmed by a second agreeing reading
                    if self._last_open and abs(self._last_open[1] - pend["at"]) <= 12:
                        return  # the F-key path recorded an open around detection time
                    if pend["cc"]:
                        self.jobs.put(("hudopen", pend["cc"], pend["at"], pend["area"]))
                    else:
                        self.set_status("HUD counter says a chest opened in %s, but there's no "
                                        "confident location for it — counted in game only"
                                        % pend["area"], WARN)
                    return
        if not prev or now - prev[1] > 30:
            return  # first reading, or too stale to attribute
        po = prev[0]
        changed = [n for n in rows if n in po and rows[n] != po[n]]
        if len(changed) != 1:
            return  # nothing moved, or several rows moved (misread) — just resync
        n = changed[0]
        (o0, t0), (o1, t1) = po[n], rows[n]
        if t0 != t1 or o1 != o0 + 1:
            return  # a jump or a total change is a misread, never an open
        if self._last_open and abs(self._last_open[1] - now) <= 12:
            return  # already explained by an F-key open (window covers the scan cadence)
        if self.mode == "idle":
            return
        # stash the candidate WITH its location — the cache is fresh now, not at confirmation
        self._hud_pending_open = {"area": n, "pair": rows[n],
                                  "cc": self._covered_fallback(now), "at": now}

    def _hud_reset_tick(self, secs_left, now):
        """'reset 19h 26m' from the HUD — the game telling us the exact next reset. Adopted only
        after TWO agreeing readings (a one-frame digit drop reads 10h wrong; it must never be
        persisted), with a 90s band absorbing minute-granularity jitter."""
        nxt = now + secs_left
        cur = self.client.reset_override
        if cur is not None and abs(nxt - cur) <= 90:
            self._hud_reset_cand = None
            return  # agrees with what we already trust
        cand = self._hud_reset_cand
        self._hud_reset_cand = (nxt, now)
        if not cand or abs(nxt - cand[0]) > 90 or now - cand[1] > 60:
            return  # a NEW value needs a second agreeing reading before it's believed
        self._hud_reset_cand = None
        self.client.reset_override = nxt
        self.cfg["hud_reset_epoch"] = int(nxt)
        save_config(self.cfg)
        self._update_stats()

    def _zone_check(self, wx, wy, wz):
        """After an open: if the HUD's current area and the polygon containing this chest
        DISAGREE, flag the spot for editors to review the boundary. Strictly noise-proof:
        the HUD area name must exactly match a drawn polygon's name (an OCR-mangled name can
        never flag), the chest must verifiably sit inside a DIFFERENT drawn polygon, and the
        flag is coord-deduped. Best effort — never blocks the open being recorded."""
        try:
            hud = str(self.client.current_area or "").strip()
            if not hud:
                return
            hud_slug = ign_slug(hud)
            with self.client.lock:
                known = {}
                for e in self.client.entries.values():
                    if isinstance(e, dict) and e.get("type") == "area" and e.get("name") \
                            and isinstance(e.get("points"), list) and len(e["points"]) >= 3:
                        s = ign_slug(e["name"])
                        if s:
                            known[s] = e
            if hud_slug not in known:
                return  # the HUD name isn't a drawn area — can't be confident enough to flag
            pos = self.client.world_to_map(wx, wz)
            if pos is None:
                return
            containing = None
            for s, a in known.items():
                if point_in_poly(pos[0], pos[1], a["points"]):
                    containing = s
                    break
            if containing is None or containing == hud_slug:
                return  # unzoned chest, or the boundary agrees — nothing to review
            p = self.client.submit_zone_flag(wx, wy, wz, known[hud_slug]["name"],
                                             known[containing]["name"])
            if p:
                self.set_status("⚠ Zone mismatch flagged: opened in %s but mapped inside %s — "
                                "editors will review the boundary"
                                % (known[hud_slug]["name"], known[containing]["name"]), WARN)
        except Exception:
            pass  # the flag is advisory — an open must never fail because of it

    def _zone_stats(self):
        """(zone, up, mapped, undiscovered) for the HUD's current zone: mapped = chests whose
        map position falls inside the zone's drawn polygon (groups count their size, matching
        the site), up = those off cooldown for this player, undiscovered = the game's true
        total minus mapped. Fields are None when the polygon or total isn't known yet."""
        hud = str(self._hud_area or "").strip()
        if not hud:
            return None
        slug = ign_slug(hud)
        with self.client.lock:
            poly, tot = None, None
            for e in self.client.entries.values():
                if isinstance(e, dict) and e.get("type") == "area" and e.get("name") \
                        and ign_slug(e["name"]) == slug and isinstance(e.get("points"), list) \
                        and len(e["points"]) >= 3:
                    poly = e["points"]
                    break
            at = self.client.entries.get("areatotals")
            if isinstance(at, dict) and isinstance(at.get("areas"), dict):
                v = at["areas"].get(slug)
                if isinstance(v, dict):
                    tot = v.get("total")
            markers = [e for e in self.client.entries.values()
                       if isinstance(e, dict) and e.get("type") == "marker" and not e.get("deleted")
                       and e.get("kind") in ("chest", "group") and e.get("x") is not None
                       and e.get("y") is not None] if poly else []
        if tot is None and self._hud_count and hud in self._hud_count[0]:
            tot = self._hud_count[0][hud][1]  # the HUD itself displays the zone's true total
        if poly is None:
            return (hud, None, None, tot)
        mapped = up = 0
        for e in markers:
            if not point_in_poly(e["x"], e["y"], poly):
                continue
            n = marker_chest_count(e)  # group pin -> its pack; chest -> 1 (tolerates a garbage count)
            mapped += n
            if not self.client.chest_on_cooldown(e):
                up += n
        und = max(0, int(tot) - mapped) if isinstance(tot, (int, float)) else None
        return (hud, up, mapped, und)

    def _reset_cooldowns_clicked(self):
        """Button handler (UI thread): confirm, then queue the reset for the worker so it runs on the
        same thread that owns the client + run state."""
        if messagebox.askyesno(
                "Reset chest cooldowns",
                "Mark ALL your chest cooldowns as available again?\n\n"
                "Use this when the random in-game event unlocks every chest early. It only changes "
                "what YOU see here — opening a chest re-locks it as usual, and the daily reset still "
                "applies normally."):
            self.jobs.put(("resetcd",))

    def _do_reset_cooldowns(self):
        """The random in-game event just unlocked every chest early — mark all your cooldowns up so
        the counter shows them as available again. Local + persisted; the next daily reset takes
        over normally, and any chest you re-open re-locks as usual."""
        self.client.mark_all_reset()
        save_config(self.cfg)
        beep("ok")
        self.set_status("♻ Cooldowns reset — all your chests read as up again", GOOD)
        self._update_stats()

    def _finish_record(self):
        """Returns False only when publishing failed (recording kept for a retry)."""
        nodes = self.record_nodes
        self.record_nodes = []
        if len(nodes) < 2:
            self.record_name = ""
            self.set_status("Route discarded — fewer than 2 stops", WARN)
            return True
        # use the name chosen when recording started; otherwise ask now (pre-filled)
        name = self._prompt("Publish route", "Route name:", self.record_name or "")
        self.record_name = ""
        if not name or not name.strip():
            self.set_status("Route discarded", WARN)
            return True
        legs = [max(1, int(round(nodes[i + 1]["t"] - nodes[i]["t"]))) for i in range(len(nodes) - 1)]
        try:
            route = self.client.publish_route(name.strip()[:60], [n["id"] for n in nodes], legs,
                                              self.cfg["ign"].strip()[:40])
        except Exception as e:
            self.record_nodes = nodes  # keep everything so Finish can be pressed again
            beep("err")
            self.set_status("⚠ Publish failed: %s — still recording, press Finish to retry" % e, BAD)
            return False
        try:
            # recorded legs are player-run truth — feed the travel-time table (best effort)
            for i in range(len(legs)):
                ka = MapClient._marker_key(self.client.marker_by_id(nodes[i]["id"]))
                kb = MapClient._marker_key(self.client.marker_by_id(nodes[i + 1]["id"]))
                if ka and kb and ka != kb:
                    self.client.observe_travel(tuple(map(int, ka.split(","))),
                                               tuple(map(int, kb.split(","))), legs[i])
            self.client.flush_travel(force=True)
        except Exception:
            pass
        total = sum(legs)
        beep("ok")
        self.set_status("✓ published \"%s\" — %d stops, ~%d:%02d" %
                        (route["name"], len(nodes), total // 60, total % 60), GOOD)
        return True

    # ---------------- run bookkeeping (stopwatch + leaderboard; no live guidance) ----------------
    def _run_elapsed(self):
        """Seconds elapsed in the current run, EXCLUDING any paused spans, so a pause truly stops the
        clock (the stopwatch display and the recorded finish time both honour it)."""
        e = time.time() - self.run_started - self.run_paused_total
        if self.run_paused:
            e -= time.time() - self._pause_at   # freeze the display during the current pause
        return max(0.0, e)

    def _run_stop_coords(self, remaining_only=False):
        """[(node, wx, wy, wz)] for the run route's openable stops (chests/groups with world
        coordinates) — optionally only those not yet opened. Feeds open-matching and the
        covered-panel fallback; the runner never points anywhere (the route lives on the map)."""
        out = []
        if not self.run_route:
            return out
        for node in self.run_route.get("nodes") or []:
            if remaining_only and node in self.run_done:
                continue
            m = self.client.entries.get(node)
            if (not isinstance(m, dict) or m.get("deleted") or m.get("kind") not in ("chest", "group")
                    or m.get("gx") is None or m.get("gz") is None):
                continue
            out.append((node, int(round(m["gx"])), int(round(m.get("gy") or 0)), int(round(m["gz"]))))
        return out

    def _advance_run(self, opened):
        """Run BOOKKEEPING only: match an open to the nearest remaining stop within 2 blocks, mark
        it done, and — once every stop has been opened — stop the clock and record the leaderboard
        time. Progress shows in the stopwatch line; where to go next is the player's own business
        (the route is drawn on the shared map)."""
        if self.mode != "run" or not self.run_route:
            return
        stops = self._run_stop_coords()
        if not stops:
            return  # no matchable stops (mob-only / coordless route): never auto-finish a 0:00 —
                    # the ■ Stop button is the only way out of such a run
        if opened:
            near = None
            for node, wx, wy, wz in stops:
                if node in self.run_done:
                    continue
                if abs(wx - opened[0]) <= 2 and abs(wz - opened[2]) <= 2:
                    d = abs(wx - opened[0]) + abs(wz - opened[2])
                    if near is None or d < near[0]:
                        near = (d, node)
            if near is not None:
                self.run_done.add(near[1])
        if any(node not in self.run_done for node, _wx, _wy, _wz in stops):
            return  # stops left — the clock keeps running
        elapsed = int(self._run_elapsed())  # excludes any paused spans
        if self.run_opened > 0:  # session tallies
            self.session_runs += 1
            if self.session_best_run is None or elapsed < self.session_best_run:
                self.session_best_run = elapsed
        rid = self.run_route.get("id") if self.run_route else None
        self.set_mode("idle")
        pr = False
        if rid and rid not in LOCAL_ROUTE_IDS and self.run_opened > 0:
            try:
                pr = self.client.record_run(rid, elapsed, self.run_opened)
            except Exception:
                pass  # leaderboard write is best-effort
        self.set_status("%s route finished — %d chest%s in %d:%02d" %
                        ("🏁 NEW RECORD!" if pr else "✓", self.run_opened,
                         "" if self.run_opened == 1 else "s", elapsed // 60, elapsed % 60),
                        GOOD)
        if pr:
            beep("ok")

    def _reset_nudge(self):
        """Once per cycle, as the daily reset nears with chests still up for you, nudge you to go —
        so a window of open chests doesn't quietly relock while you're doing something else."""
        if not self.cfg["ign"].strip():
            return
        if not self.client.calibration:
            return  # without calibration nothing is mapped — don't fire (or burn the flag); the refresher's
                    # calibration warning is the priority, and the nudge still fires once calibrated
        left = self.client.next_reset_epoch() - time.time()
        if 0 < left <= 900:  # last 15 minutes
            if not self._reset_nudged:
                avail = sum(1 for e in self.client.chests() if not self.client.chest_on_cooldown(e))
                if avail > 0:
                    self._reset_nudged = True
                    self.set_status("⏰ Daily reset in %d min — %d chest%s still up, go grab them!"
                                    % (max(1, int(round(left / 60))), avail,
                                       "" if avail == 1 else "s"), WARN)
        elif left > 900:
            self._reset_nudged = False  # re-arm for the next day's reset

    def _refresher(self):
        while True:
            try:
                self.client.refresh()
                names = [r.get("name", "?") for r in self.client.routes()]
                self.root.after(0, lambda n=names: self.route_box.config(values=n))
                self._style_modes()  # reflect server auth state (open vs key-gated) live
                self._update_stats()
                self._reset_nudge()
                self.client.flush_travel()
                self.client.retry_deletes()
                self.client.retry_runs()
                if not self.client.calibration:
                    self.set_status("⚠ Map not calibrated — open the web map and press 🎯 Calibrate", WARN)
            except Exception as e:
                self.set_status("⚠ Can't reach the shared map: %s" % e, BAD)
            time.sleep(60)

    def _poller(self):
        """Occasional OCR to re-sync position/orientation while guiding.
        Cadence adapts to how wrong recent predictions were."""
        last_save = 0
        while True:
            self.poll_flag.wait()
            t0 = time.time()
            try:
                self._game_bbox = find_game_window(self.cfg["window_title"])  # keep dock cache fresh
                at = self.dr.snapshot()  # estimate at capture time (latency compensation)
                frame = grab_game(self.cfg)
                hud = self.hud.read(frame, want_target=True)
                # remember the last chest we were aiming at — _do_chest falls back to this when
                # the open-chest UI covers the F7 panel and the on-open read comes back empty.
                cc = chest_coords(hud["target"])
                if cc and self._target_plausible(cc):
                    self._last_target = {"coords": cc, "at": time.time()}
                elif hud["target"] and hud["target"].get("block"):
                    self._last_target = None  # non-chest block (or implausible read) — looked away
                self._hud_scan(frame, time.time())
                if hud["position"]:
                    if self.dr.sync(hud["position"], hud["yaw"], at=at,
                                    yaw_exact=hud["yaw_exact"],
                                    speed=hud.get("speed")) and t0 - last_save > 30:
                        save_config(self.cfg)  # persist the refined walk speed, throttled
                        last_save = t0
                elif self.hud.misses == 2 and (self._last_open is None
                        or time.time() - self._last_open[1] > FALLBACK_MAX_AGE):
                    # a covered panel is expected right after an open — don't clobber the
                    # fallback's success message with a scary "can't read position" warning
                    self.set_status("Can't read position — is the F7 WORLD panel visible?", WARN)
            except Exception:
                pass
            interval = self.dr.poll_interval(float(self.cfg["ocr_poll_sec"]))
            time.sleep(max(0.3, interval - (time.time() - t0)))

    def _ui_tick(self):
        try:
            now = time.time()
            dt, self._last_tick = now - self._last_tick, now
            self.watcher.poll()
            held = set(self.watcher.held)
            self.dr.tick(min(dt, 0.3), held)
            # movement bookkeeping for the stand-still auto-pause: a WASD key held, or the game itself
            # reporting a FRESH ground speed, counts as moving. (move_speed() can't be used — it falls
            # back to the learned constant when idle, so it would read as "always moving".)
            ocr_moving = (self.dr._ocr_speed is not None and self.dr._ocr_speed > 0.5
                          and (now - self.dr._ocr_speed_t) < 2.5)
            if held or ocr_moving:
                self._last_moved = now
            if self.mode == "run" and now - self._autopause_t > 0.4:  # throttle the focus/panel checks
                self._autopause_t = now
                self._auto_pause_eval()
            self._setup_nag_check()
            try:
                while True:
                    tag = self.watcher.events.get_nowait()
                    if self.mode != "idle" and (not self.cfg["only_when_game_focused"]
                                                or game_focused(self.cfg["window_title"])):
                        if self.jobs.qsize() == 0:
                            self.jobs.put((tag,))
            except queue.Empty:
                pass
            self._draw_run_eta()
        except Exception:
            pass  # a transient hiccup must never kill the 50 ms loop
        finally:
            try:
                self.root.after(50, self._ui_tick)
            except tk.TclError:
                pass  # window destroyed — shutting down

    @staticmethod
    def _mmss(s):
        s = max(0, int(round(s)))
        return "%d:%02d" % (s // 60, s % 60)

    def _draw_run_eta(self):
        """Run STOPWATCH line (run mode only): elapsed time, opens counted, stops done, and the
        pause state. Pure bookkeeping display — it never estimates, points, or paces."""
        if self.mode != "run":
            self.eta_label.config(text="")
            return
        if self.run_paused:
            why = {"focus": "game unfocused", "screen": "menu / chest open",
                   "still": "standing still"}.get(self.pause_reason)
            head = "⏸ Auto-paused (%s)" % why if why else "⏸ Paused"
            self.eta_label.config(text="%s · %s elapsed (timer frozen)" % (head, self._mmss(self._run_elapsed())),
                                  fg=WARN)
            return
        stops = self._run_stop_coords()
        txt = "⏱ %s" % self._mmss(self._run_elapsed())
        if stops:
            txt += " · %d/%d stops" % (sum(1 for n, *_ in stops if n in self.run_done), len(stops))
        if self.run_opened:
            txt += " · %d opened" % self.run_opened
        self.eta_label.config(text=txt, fg=FG3)

    def run(self):
        try:
            self.client.refresh()
            self.route_box.config(values=[r.get("name", "?") for r in self.client.routes()])
            self._update_stats()
        except Exception as e:
            self.set_status("⚠ Can't reach the shared map: %s" % e, BAD)
        if self.client.dry:  # unmissable: every write is skipped in this mode
            self.set_status("🧪 DRY RUN — chests are NOT saved to the shared map. Turn off "
                            "dry_run in capture_config.json to log for real.", BAD)
        self.root.mainloop()


# ====================================================================== #
#  --test                                                                #
# ====================================================================== #

def test_once(cfg):
    frame = grab_game(cfg)
    dbg = os.path.join(APP_DIR, "capture_debug.png")
    frame.save(dbg)
    print("captured %dx%d -> %s" % (frame.width, frame.height, dbg))
    hud = HudReader(cfg)
    # show exactly what the OCR sees, so a missed chest can be diagnosed from the raw text
    strip = hud._strip(frame)
    for sc in ocr_scales(strip.width):
        print("panel OCR x%d: %r" % (sc, ocr_text(strip, sc)[:400]))
    out = hud.read(frame, want_target=True, thorough=True)
    print("position :", out["position"])
    print("yaw      :", out["yaw"])
    print("target   :", out["target"], "-> chest coords:", chest_coords(out["target"]))
    if out["position"] and out["yaw"] is not None and out["target"]:
        px, _, pz = out["position"]
        t = out["target"]["coords"]
        rel = wrap_deg(bearing_to(px, pz, t[0], t[2]) - out["yaw"])
        print("aim      : %.1f deg %s (should be ~0 when aiming at the target)"
              % (rel, "left" if rel > 0 else "right"))


def main():
    if "--version" in sys.argv or "-V" in sys.argv:
        print("Histatu Runner " + __version__ + ("" if EDITION == "full" else " [" + EDITION + "]"))
        return
    make_dpi_aware()
    if FROZEN:  # tidy up after a self-update: the previous binary was renamed aside
        try:
            old = os.path.abspath(sys.argv[0] or sys.executable) + ".old"
            if os.path.exists(old):
                os.remove(old)
        except Exception:
            pass  # the old instance may still be exiting — next launch gets it
    cfg = load_config()
    if "--dry-run" in sys.argv:
        # session-only override: an underscore key is never written by save_config, so one
        # --dry-run launch can't silently poison the saved config into permanent dry-run
        # (which would make every "✓ logged" a no-op — writes skipped, nothing shared).
        cfg["_dry_cli"] = True
    if "--test" in sys.argv:
        test_once(cfg)
        return
    App(cfg).run()


if __name__ == "__main__":
    main()
