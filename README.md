# Histatu Dungeon

A community-maintained **live chest map and route tracker** for Hytale's dungeon, plus **Histatu
Runner** — an in-game overlay that logs the chests you open and times your route runs for the
leaderboards.

- 🌍 **Live map:** https://blakebiz-dungeon-companion.vercel.app
- 📥 **Get the app:** https://blakebiz-dungeon-companion.vercel.app/get-app.html (or the
  [latest release](https://github.com/blakebiz-dev/Histatu-Dungeon-Companion/releases/latest))

This repo is the complete source for all of it — the website, the server, and the app you install —
published so anyone can verify exactly what the tool does before running it.

## What it does — and what it never does

The runner reads the game **only through your own screen**: it OCRs Hytale's built-in F7 debug
panel (your position/orientation and the block you're aiming at). That's the entire interface with
the game.

It **never**:

- injects into or modifies the game, its memory, or its files (Hytale doesn't allow client mods,
  and neither do we);
- reads network traffic or packets;
- captures anything outside the F7 panel region it needs;
- sends anything you didn't ask it to. What it shares when you use it: chest locations you log,
  your chest opens (under your in-game name, for cooldown tracking), measured travel times between
  chests, and route completion times. The **Lite** edition additionally strips the opt-in
  detection-report feature, so it can never upload a capture at all.

Don't take the README's word for it — [`histatu_runner.py`](companion/histatu-capture/histatu_runner.py)
is the whole app, and the release workflow in
[`.github/workflows/release-runner.yml`](.github/workflows/release-runner.yml) builds the exe from
exactly this source.

**First launch — Capture Doctor.** Every PC renders the F7 panel a little differently (resolution,
monitor scaling, GUI size), so on first launch the runner offers a ~1-minute setup wizard: it finds
your game window, measures which OCR zoom reads *your* panel sharpest, health-checks each signal
(position / facing / speed), and measures your walk pace for sharper open-tracking. Everything it
measures
is stored locally in `capture_config.json`, every step is skippable, and you can re-run it anytime
from **⚙ Settings → 🩺 Re-run setup** — do that after changing monitor, resolution, or GUI scale.

## Repo layout

| Path | What it is |
|---|---|
| `index.html` | The live map — a single self-contained page (pins, areas, routes, leaderboards, editor tools) |
| `get-app.html` | Download page for the runner |
| `api/dungeon.js` | The shared-map API (one Vercel function, Upstash Redis storage) |
| `api/debug.js` | Opt-in detection reports (private gists, editor-reviewed, deleted after review) |
| `api/download.js` | Stable download/update endpoint that redirects to the latest GitHub release |
| `api/__tests__/` | Server test suites (run with `node`) |
| `companion/histatu-capture/` | Histatu Runner — the overlay app (Python/Tkinter), its tests, and build scripts |

## Identity — you are who you say you are

Contributing is tied to your **real Hytale account**, so nobody can log chests, post run times, or
rate routes under someone else's name. **Reading the map is always open to everyone.**

- **Sign in** on the website (top-right) — you approve on **Hytale's own site** (`accounts.hytale.com`,
  OAuth2 device flow). This tool never sees your password. The server reads your game profile
  **once** to learn your verified in-game name + account UUID, then **discards every Hytale token** —
  none are ever stored here.
- You get a private key (`hd_…`) the site remembers, and which you paste into the companion app once.
  It's stored only as a **SHA-256 hash** server-side; the app and site send it over HTTPS on each
  write. **Reset it any time** by signing in again (the old key dies instantly).
- **Roles:** the map **owner** (a fixed verified account) can toggle **editor** rights on any
  signed-in player by name — no shared secret to leak or rotate. Editors maintain the pins;
  everyone else contributes their own opens/runs/ratings.
- Every write is checked server-side against the key's bound identity: you **physically cannot**
  write another player's opens, forge a leaderboard time under their name, or self-grant editor.
  Personal aggregates are **merge-only** (add/improve, never wipe someone else's). Rate limits,
  entry caps, and per-IP throttling of bad-key and failed-sign-in attempts bound abuse. Every rule
  lives in `api/dungeon.js` with the reasoning in comments.

> **A note on the Hytale integration:** sign-in uses Hytale's official device-flow endpoints. There
> is no third-party developer program yet, so — like other community tools — this reuses Hypixel's
> own device-flow client id (env-configurable, `HYTALE_OAUTH_CLIENT_ID`). It works and keeps your
> credentials on Hytale's site, but it is an unofficial integration and could change.

## Running from source

**The map** is static — open `index.html` (it runs in local-only mode without the API) or deploy it
(see below).

**The runner** (Windows or Linux): use `run.bat` / `run.sh` in `companion/histatu-capture/` —
they install the few dependencies (Pillow + winsdk on Windows; Pillow, pytesseract and the
`tesseract-ocr` system package on Linux) and start the app. Full details in
[its README](companion/histatu-capture/README.md).

**Build the exe yourself** (what CI does): `build.bat` in the same folder — or read the release
workflow; the published exes are built by GitHub Actions from the tagged source, unmodified.

**Tests:**

```
node api/__tests__/dungeon-validate.test.js     # 168 tests
node api/__tests__/dungeon-handler.test.js      #  98 tests
node api/__tests__/debug.test.js && node api/__tests__/download.test.js
cd companion/histatu-capture && python test_runner.py   # 390 tests
```

## Self-hosting your own map

Deploy this repo to [Vercel](https://vercel.com) (zero config — static root + `api/` functions) and
set the environment variables:

| Variable | Required | Purpose |
|---|---|---|
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | yes | The shared-map store ([Upstash](https://upstash.com) free tier is plenty) |
| `OWNER_IGN` | yes | Your Hytale in-game name — that verified account becomes the map **owner** |
| `DUNGEON_WRITE_KEY` | optional | Break-glass master key (acts as owner). **20+ random chars.** Never shown in any UI; leave unset unless you want a recovery key |
| `HYTALE_OAUTH_CLIENT_ID` / `HYTALE_OAUTH_SCOPE` | optional | Override the device-flow client (defaults to Hytale's `hytale-server`) |
| `GITHUB_GIST_TOKEN` | optional | Enables opt-in detection reports (gist scope only) |
| `GITHUB_TOKEN` | optional | Raises GitHub API rate limits for the download endpoint |
| `DOWNLOAD_REPO` | optional | `owner/repo` to serve releases from (defaults to this repo) |

## License

[MIT](LICENSE). Not affiliated with Hypixel Studios — Hytale is their trademark; this is a fan-made
companion tool that works entirely from what's on your own screen.
