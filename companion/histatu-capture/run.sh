#!/bin/sh
# Histatu Runner (Linux) — checks dependencies, then starts the overlay.
# Works on X11 (xdotool + pynput) and Wayland/wlroots — Hyprland, Sway (grim + evdev).
cd "$(dirname "$0")"

# base deps (both display servers)
python3 - <<'EOF' || python3 -m pip install --user pillow pytesseract
import PIL, pytesseract
EOF
command -v tesseract >/dev/null 2>&1 || \
  echo "WARNING: install the tesseract-ocr package (Arch: sudo pacman -S tesseract tesseract-data-eng)"

if [ -n "$WAYLAND_DISPLAY" ] || [ "$XDG_SESSION_TYPE" = "wayland" ]; then
  # Wayland: capture via grim, input via evdev (needs the 'input' group), docking via hyprctl/swaymsg
  python3 -c "import evdev" 2>/dev/null || python3 -m pip install --user evdev
  command -v grim >/dev/null 2>&1 || \
    echo "WARNING: install 'grim' for screen capture (Arch: sudo pacman -S grim)"
  id -nG 2>/dev/null | tr ' ' '\n' | grep -qx input || \
    echo "WARNING: key detection needs read access to /dev/input — run: sudo usermod -aG input \"$USER\"  then log out/in"
else
  # X11: pynput for input, xdotool for the game window
  python3 -c "import pynput" 2>/dev/null || python3 -m pip install --user pynput
  command -v xdotool >/dev/null 2>&1 || echo "note: install xdotool so the overlay can find/dock to the game window"
fi

exec python3 histatu_runner.py "$@"
