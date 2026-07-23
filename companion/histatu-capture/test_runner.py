"""Unit tests for histatu_runner.py — parsers, run bookkeeping, auth/pending, update + OCR helpers.
Run:  py -3 test_runner.py   (from this folder)"""
import sys, math, time, types, os, random, builtins, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import histatu_runner as hr

fails = []
def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

def mk(i, x, y, z):
    return {"id": "m%d" % i, "gx": x, "gy": y, "gz": z}

# ---------- parsers / geometry regressions ----------
txt = "Position: (60.677, 76.000, -63.178) Orientation: (-6.4, 78.0, 0.0) West Target: Block @ (57.000, 76.000, -65.000) Furniture_Village_Chest_Large"
t("parse position", hr.parse_position(txt) == (60.677, 76.0, -63.178))
t("parse yaw", hr.parse_yaw(txt) == 78.0)
t("parse target", hr.parse_target_block(txt)["block"].startswith("Furniture"))
t("target picks target triple, not position", hr.parse_target_block(txt)["coords"] == (57.0, 76.0, -65.0))
t("split-sign yaw", hr.parse_yaw("Orientation: (-11.80, - 160.50, 0.00) South") == hr.wrap_deg(-160.5))
# yaw hardening: recover the yaw from the roll+cardinal tail when the label/pitch got mangled/reordered
t("yaw roll-anchor: mangled pitch, label intact",
  hr.parse_yaw("Orientation: (42 40, . -63.20, 0.00) East") == hr.wrap_deg(-63.2))
t("yaw roll-anchor: cardinal precedes a broken label",
  hr.parse_yaw("Chunk: (11, 8, 26) , -63.20, 0.00) East Orientation: (42.40 Environment") == hr.wrap_deg(-63.2))
t("yaw roll-anchor: colon read as bullet",
  hr.parse_yaw("Orientation ( 5 80, . -170.00, 0.00) South") == hr.wrap_deg(-170.0))
t("yaw: strict pattern still wins when clean (roll-anchor only a fallback)",
  hr.parse_yaw("Orientation: (-6.4, 78.0, 0.0) West") == 78.0)
t("yaw: roll-anchor never fires without a cardinal (no false yaw from Velocity/Move Force)",
  hr.parse_yaw("Move Force: (0.000, 0.000, 0.000) Velocity: (1.5, 0.0, 2.0)") is None)
# roll-anchor must NEVER return the PITCH as yaw (regression for the two adversarial-review findings):
# the yaw needs a COMMA before it, which the pitch (following "(") never has, so these drop to coarse
# rather than trusting the pitch as an exact heading.
t("yaw roll-anchor: dropped pitch-yaw comma never returns the pitch (55)",
  hr.parse_yaw("Orientation: (55.000 -3.2, 0.00) North") not in (55.0, hr.wrap_deg(55.0)))
t("yaw roll-anchor: dropped yaw token never returns the pitch (55)",
  hr.parse_yaw("Orientation: (55.000, 0.00) North") is None)
# clock-grab (pre-existing ORI_RE bug): a reordered panel must NOT stitch the game-clock hour as yaw —
# the self-validated roll anchor (tried first) pins the real yaw instead
t("yaw: reordered panel doesn't stitch the game clock '11' as yaw",
  hr.parse_yaw("Orientation: (42.40 Env Weather Day 1 , 11 : 00 (Morning) , -38.60, 0.00) North") == hr.wrap_deg(-38.6))
# aimed-block Rotation line is ALWAYS "(0,0,0) North": the yaw search must stay ABOVE the block section
# so it can't read that fixed North as the player's heading (regression for the round-3 review finding)
_ROT = ("Orientation: (-28.6°, -84.4°, 0.0°) East Target: Block @ (-11.000, 67.000, -227.000) "
        "Furniture_Dungeon_Chest_Epic Rotation: (0.0, 0.0, 0.0) North (0, 0, 0)")
t("yaw: aimed-block Rotation '(0,0,0) North' is never read as the player yaw",
  hr.parse_yaw(_ROT) == hr.wrap_deg(-84.4))
t("yaw compass: coherence partner ignores the block Rotation cardinal too",
  hr.parse_yaw_compass(_ROT) == hr.wrap_deg(-90.0))
# the strict ORI_RE fallback is also cardinal-cross-checked (round-4 review): a space-for-comma that
# lets its gap stitch the roll 0.00 as "yaw", or a leading-digit-dropped 3-digit yaw, is rejected to
# coarse rather than trusted as an exact (wrong) heading
t("yaw: space-for-comma never stitches the roll 0.00 as yaw",
  hr.parse_yaw("Orientation: (-29.10 80.40, 0.00) West Environment Day 1 , 11:0") is None)
t("yaw: leading-digit-dropped yaw fails the cardinal cross-check (132.8 read as 32.8 -> rejected)",
  hr.parse_yaw("Orientation: (-14.70, 32.80, 0.00) West Environment Day 1 , 11") is None)
t("yaw roll-anchor: mangled label + dropped sign -> rejected via cardinal cross-check",
  hr.parse_yaw("Chunk (1,2,3) , 100.00, 0.00) East 0ri3nt: (12.40") is None)
t("yaw compass: label-free fallback finds the cardinal when 'Orientation' is mangled",
  hr.parse_yaw_compass("0ri3ntation (x, y, 0.00) West") == 90.0)
# motion parsers: Speed (walk/sprint magnitude), Velocity, Wish Dir
t("parse speed: clean scalar", hr.parse_speed("Velocity: (0.0, 0.0, 0.0) Speed: 3.35 Wish Dir: (0,0)") == 3.35)
t("parse speed: sprint value", hr.parse_speed("Speed: 6.48 Wish") == 6.48)
t("parse speed: a (x, y) tuple misread is NOT taken as a scalar", hr.parse_speed("Speed: (0.000, 0.000) Wish Dir:") is None)
t("parse speed: absent -> None", hr.parse_speed("Position: (1.0, 2.0, 3.0)") is None)
t("parse speed: garbage magnitude rejected", hr.parse_speed("Speed: 480.00") is None)
t("parse velocity: world triple", hr.parse_velocity("Velocity: (-4.24, 0.000, 4.24) Speed: 6.0") == (-4.24, 0.0, 4.24))
t("parse wishdir: horizontal pair", hr.parse_wishdir("Wish Dir: (-1.000, 1.000) State") == (-1.0, 1.0))

# ---------- target-OCR robustness (chests that read imperfectly) ----------
t("target: mangled parens still parse", hr.chest_coords(hr.parse_target_block("Target Block @ l57, 76, -65 J Furniture_Village_Chest_Large")) == (57, 76, -65))
t("target: no parens, semicolons", hr.chest_coords(hr.parse_target_block("Target: Block @ 57; 76; -65 Big_Chest")) == (57, 76, -65))
t("target: dropped Block/@", hr.parse_target_block("Targt (57, 76, -65) Wooden_Chest")["coords"] == (57.0, 76.0, -65.0))
t("target: no target line -> None", hr.parse_target_block("Position: (1,2,3) Orientation: (0,0,0) North") is None)
t("target: decimal-as-comma is ambiguous -> None", hr.parse_target_block("Target: Block @ (57,000, 76.000, -65.000) Big_Chest") is None)
t("target: normal decimals still parse", hr.parse_target_block("Target: Block @ (57.000, 76.000, -65.000) Big_Chest")["coords"] == (57.0, 76.0, -65.0))
# a triple far past the anchor belongs to some other panel line — never adopt it
t("target: distant triple not adopted", hr.parse_target_block(
    "Target: Block @ scuffed unreadable smear here then later (0.0, 180.0, 0.0) Furniture_Chest") is None)
# but moderate OCR junk between the anchor and the coords must not cost the read
t("target: junky-but-close triple still parses", hr.parse_target_block(
    "Target: Block @ sm3ared junk here (57.000, 76.000, -65.000) Big_Chest")["coords"] == (57.0, 76.0, -65.0))
# a trailing target-ish token (second Target row / junk after the name) must not kill the read
t("target: trailing target-ish text ignored", hr.parse_target_block(
    "Target: Block @ (57.000, 76.000, -65.000) Big_Chest Target Entity: none")["coords"] == (57.0, 76.0, -65.0))

# ---------- real-world 4K panel shapes (verbatim from read_debug.log, 2026-07-15) ----------
# the word "Target" lost to OCR, "Block (…)" survives, decimals space-padded, chest name only
# present as an on-screen ALL-CAPS nameplate
_REAL = ("SMALL WIND TEMPLE CHEST 4) (-112.347, 70.000, -49.846) on . (15, 6, 14) in (-4, 2, -2) "
         "at ion: (32.40, 63.20, 0.00) West ntlent. Env Zone2 Savanna r Histatu_S01mara_Light_Duststorm "
         "Day 1, 11:0 Local R: 1 Best: 183 (nevu'): On Ground I Idle Adventure "
         "Block (-114.000, 71 . 000, -51 . 000)")
_real_parse = hr.parse_target_block(_REAL)
t("real 4K: Block-anchored, space-padded decimals parse", _real_parse is not None
  and _real_parse["coords"] == (-114.0, 71.0, -51.0))
t("real 4K: nameplate supplies the chest name", _real_parse is not None
  and hr.chest_coords(_real_parse) == (-114, 71, -51))
# space-padded POSITION decimals ("-111 .194" verbatim from the log) parse too
t("real 4K: space-padded position parses", hr.parse_position(
    "WORLD Position: (-111 .194, 70.000, -49.951) Chunk: (16, 6, 14)") == (-111.194, 70.0, -49.951))
# popup occlusion (observed at 1440p): only the first glyph of a component survives, and the
# visible comma stitches a structurally valid triple — it must be REJECTED, never y=7
t("occlusion: truncated y rejected", hr.parse_position("Position: (57.000, 7, -65.000)") is None)
t("occlusion: one-decimal stub rejected", hr.parse_position("Position: (57.000, 7.0, -65.000)") is None)
t("occlusion: bare-dot stub rejected", hr.parse_position("Position: (57.000, 7., -65.000)") is None)
t("occlusion: clipped tail still fails closed", hr.parse_position("Position: (57.000, 7") is None)
t("two-decimal read still accepted (last glyph lost)", hr.parse_position(
    "Position: (57.000, 76.00, -65.000)") == (57.0, 76.0, -65.0))
# the game HUD's plural "CHESTS >" counter must never make an aimed non-chest a chest —
# including when OCR drops or splits the trailing S (proven attack from the adversarial review)
_DOOR = ("HISTATU DUNGEON WORLD SOL MARA CHESTS > Solmara Thornvale reset 19h 26m 46/321 0/61 "
         "Block (-114.000, 71.000, -51.000)")
t("real 4K: HUD 'CHESTS >' never names a chest", hr.chest_coords(hr.parse_target_block(_DOOR)) is None)
for _hud in ("SOL MARA CHEST S > Solmara reset", "SOL MARA CHEST > Solmara reset",
             "SOL MARA CHEST' > Solmara reset", "HISTATU DUNGEON WORLD SOL MARA CHEST 46/321"):
    t("HUD misread never names a chest: %r" % _hud[:26],
      hr.chest_coords(hr.parse_target_block(_hud + " Block (-114.000, 71.000, -51.000)")) is None)
# "Skyblock 37565" must not anchor as a Block line
t("real 4K: 'Skyblock' is not a Block anchor", hr.parse_target_block(
    "Network Skyblock 37565 Builtln: 22848 Cached : 14717") is None)
# panel text with no target section at all still returns None
t("real 4K: panel without target section -> None", hr.parse_target_block(
    "WORLD Position: (-109.444, 70.000, -49.392) Chunk: (18, 6, 14) in (-4, 2, -2) "
    "Orientation: (14.30, 68.10, 0.00) West Weather: Zone2_Desert_Haze Velocity: (-0.102, 0.000, 0.000)") is None)
# a "target" triple that echoes the Orientation readout is the orientation, not a chest
t("target: orientation echo rejected", hr.parse_target_block(
    "Position: (60.0, 76.0, -63.0) Orientation: (0.00, 180.00, 0.00) South Target: (0, 180, 0) Furniture_Chest") is None)
t("target: legit coords near ori values still parse", hr.parse_target_block(
    "Position: (60.0, 76.0, -63.0) Orientation: (-6.4, 78.0, 0.0) West Target: Block @ (57.000, 76.000, -65.000) Furniture_Chest")["coords"] == (57.0, 76.0, -65.0))
t("target: unreadable ori doesn't break a legit read", hr.parse_target_block(
    "Orientation: l*#$ West Target: Block @ (57.000, 76.000, -65.000) Furniture_Chest")["coords"] == (57.0, 76.0, -65.0))
# a noise token matching the loose anchor must not adopt the Position line's numbers
t("target: position echo rejected", hr.parse_target_block(
    "taget Position: (60.000, 76.000, -63.000) Orientation: (-6.4, 78.0, 0.0) West Old_Chest") is None)
t("target: noise anchor doesn't shadow the real line", hr.parse_target_block(
    "taget smear (1.0, 2.0, 3.0) junk Target: Block @ (57.000, 76.000, -65.000) Old_Chest")["coords"] == (57.0, 76.0, -65.0))
# orientation echo compare is wrap-aware (panel prints 270 where -90 was read)
t("target: orientation echo rejected wrap-aware", hr.parse_target_block(
    "Orientation: (0.00, -90.00, 0.00) East Target: (0, 270, 0) Old_Chest") is None)
t("chest name exact", hr.looks_like_chest("Chest"))
t("chest name Che5t (s->5)", hr.looks_like_chest("Furniture_Village_Che5t_Large"))
t("chest name Ch3st (e->3)", hr.looks_like_chest("Old_Ch3st"))
t("chest name Chesf (t->f)", hr.looks_like_chest("Chesf"))
t("chest name Cnest (h->n)", hr.looks_like_chest("Cnest"))
t("chest name Chst (dropped e)", hr.looks_like_chest("Chst"))
t("not chest: door", not hr.looks_like_chest("Oak_Door"))
t("not chest: crafting table", not hr.looks_like_chest("Crafting_Table"))
t("not chest: villager", not hr.looks_like_chest("Villager_Farmer"))
t("not chest: torch", not hr.looks_like_chest("Wall_Torch"))
t("not chest: crest (near-miss)", not hr.looks_like_chest("Mountain_Crest"))
t("chest_coords rejects a non-chest block", hr.chest_coords({"coords": (1, 2, 3), "block": "Oak_Door"}) is None)
t("compass fallback", hr.parse_yaw_compass("Orientation: (x) West") == 90.0)
t("bearing empirical", abs(hr.wrap_deg(hr.bearing_to(-14.989, -33.211, -14.543, -30.574) + 170.3)) < 20)

# ---------- pause/resume: the clock stops so a break doesn't ruin your time ----------
_pf = types.SimpleNamespace(run_started=time.time() - 100, run_paused=False, run_paused_total=30.0, _pause_at=0.0)
t("run elapsed: excludes banked paused time", abs(hr.App._run_elapsed(_pf) - 70) < 0.6)
_pf.run_paused = True; _pf._pause_at = time.time() - 10
t("run elapsed: freezes during an active pause", abs(hr.App._run_elapsed(_pf) - 60) < 0.6)
_tp = types.SimpleNamespace(mode="run", run_paused=False, pause_reason=None, run_paused_total=0.0, _pause_at=0.0,
                            run_started=time.time() - 50, set_status=lambda *a, **k: None,
                            _advance_run=lambda opened=None: None, _style_pause=lambda: None)
_tp._set_paused = lambda on, reason, since=None: hr.App._set_paused(_tp, on, reason, since)
hr.App._toggle_pause(_tp)
t("pause: toggling a running route pauses it", _tp.run_paused is True and _tp._pause_at > 0)
_tp._pause_at = time.time() - 8   # pretend the pause lasted 8s
hr.App._toggle_pause(_tp)
t("pause: resuming banks the paused span and clears paused",
  _tp.run_paused is False and abs(_tp.run_paused_total - 8) < 0.6)
_tp2 = types.SimpleNamespace(mode="log", run_paused=False)
hr.App._toggle_pause(_tp2)
t("pause: a no-op when not running", _tp2.run_paused is False)

# ---------- run bookkeeping: id-matched stops, out-of-order opens, finish records the time ----------
_rbe = {"a": {"id": "a", "type": "marker", "kind": "chest", "gx": 0, "gy": 64, "gz": 0},
        "b": {"id": "b", "type": "marker", "kind": "chest", "gx": 10, "gy": 64, "gz": 0},
        "g": {"id": "g", "type": "marker", "kind": "group", "count": 4, "gx": 20, "gy": 64, "gz": 0},
        "m": {"id": "m", "type": "marker", "kind": "mob", "gx": 30, "gy": 64, "gz": 0}}
def _mkrun():
    f = types.SimpleNamespace(mode="run", run_route={"id": "r1", "nodes": ["a", "b", "g", "m"]},
                              run_done=set(), run_started=time.time() - 100, run_paused=False,
                              run_paused_total=0.0, _pause_at=0.0, run_opened=3,
                              session_runs=0, session_best_run=None, statuses=[], recorded=[])
    f.set_status = lambda m, c=None: f.statuses.append(m)
    f.set_mode = lambda m: setattr(f, "mode", m)
    f._run_elapsed = lambda: hr.App._run_elapsed(f)
    f._run_stop_coords = lambda remaining_only=False: hr.App._run_stop_coords(f, remaining_only)
    f.client = types.SimpleNamespace(entries=_rbe,
                                     record_run=lambda rid, t_, c: f.recorded.append((rid, t_, c)) or True)
    return f
_obeep = hr.beep
hr.beep = lambda *a, **k: None
try:
    _fr = _mkrun()
    t("run stops: chests + coord groups only (mobs excluded)",
      [n for n, *_ in _fr._run_stop_coords()] == ["a", "b", "g"])
    hr.App._advance_run(_fr, (10, 64, 0))     # open b first (out of order)
    t("run: out-of-order open marks only that stop", _fr.run_done == {"b"} and _fr.mode == "run")
    t("run: remaining_only excludes done stops",
      [n for n, *_ in _fr._run_stop_coords(remaining_only=True)] == ["a", "g"])
    hr.App._advance_run(_fr, (0, 64, 0))
    hr.App._advance_run(_fr, (21, 64, 1))     # the group, matched within 2 blocks
    t("run: all stops done -> finished, time recorded to the leaderboard",
      _fr.mode == "idle" and _fr.recorded == [("r1", 100, 3)] and _fr.session_runs == 1
      and _fr.session_best_run == 100)
    _fr2 = _mkrun()
    hr.App._advance_run(_fr2, (99, 64, 99))   # an open nowhere near any stop
    t("run: unmatched open marks nothing and keeps running",
      _fr2.run_done == set() and _fr2.mode == "run" and not _fr2.recorded)
    _fr3 = _mkrun()
    hr.App._advance_run(_fr3, (13, 64, 0))    # 3 blocks off stop b (10,0) — outside the 2-block match
    t("run: a 3-block near-miss never matches a stop", _fr3.run_done == set())
    # a route with NO matchable stops (mob-only) must never auto-finish at 0:00
    _fr4 = _mkrun()
    _fr4.run_route = {"id": "r2", "nodes": ["m"]}
    hr.App._advance_run(_fr4, None)
    t("run: mob-only route never auto-finishes", _fr4.mode == "run" and not _fr4.recorded)
finally:
    hr.beep = _obeep

# slim DeadReckoner: WASD coasts the position along the last panel-read heading
_drc = hr.DeadReckoner({"move_speed": 4.0})
_drc.pos, _drc.yaw = [0.0, 64.0, 0.0], 180.0   # facing 180: forward = (-sin, -cos) = (0, +1)
_drc.tick(1.0, {"w"})
t("slim DR: W coasts forward along the panel heading", abs(_drc.pos[2] - 4.0) < 1e-6 and abs(_drc.pos[0]) < 1e-6)
_drc.tick(1.0, set())
t("slim DR: no keys, no drift", abs(_drc.pos[2] - 4.0) < 1e-6)
_drc.sync([1.0, 64.0, 5.0], -90.0)
t("slim DR: sync adopts the fix and stores the read heading verbatim", _drc.pos[2] == 5.0 and _drc.yaw == -90.0)

# ---------- auto-pause: menu / covered panel / unfocused game / stand-still >10s ----------
def _mk_pausefake(**kw):
    f = types.SimpleNamespace(run_paused=False, pause_reason=None, run_paused_total=0.0, _pause_at=0.0,
                              _pause_banked_to=0.0, **kw)
    f._set_paused = lambda on, reason, since=None: hr.App._set_paused(f, on, reason, since)
    return f
# back-dating: a stand-still pause freezes the clock as of when you STOPPED, not now, so the idle
# seconds (and the 10s detection grace) never count — this is the "remove the 10 seconds" behaviour
_sp = _mk_pausefake(); _spnow = time.time()
_sp._set_paused(True, "still", since=_spnow - 10)
t("set_paused: back-dates the pause start via `since`", abs(_sp._pause_at - (_spnow - 10)) < 0.2)
_sp._set_paused(False, None)
t("set_paused: banks the back-dated span so idle time is dropped from the clock", _sp.run_paused_total >= 9.5)
_sp2 = _mk_pausefake()
t("set_paused: a fresh pause transitions and reports the change", _sp2._set_paused(True, "manual") is True and _sp2.run_paused)
t("set_paused: re-pausing swaps the reason but keeps the running span",
  _sp2._set_paused(True, "focus") is True and _sp2.pause_reason == "focus" and _sp2._pause_at > 0)
t("set_paused: a redundant resume reports no change", _sp2._set_paused(False, None) is True and _sp2._set_paused(False, None) is False)
# watermark clamp: a stand-still back-date must never reach INTO a span already banked, or that
# overlap would be subtracted from the run twice (a false-fast, cheatable leaderboard time)
_wm = _mk_pausefake(); _wmnow = time.time()
_wm._set_paused(True, "screen"); _wm._pause_at = _wmnow - 5      # menu pause that began 5s ago
_wm._set_paused(False, None)                                     # resume banks ~5s; watermark = now
_bank1 = _wm.run_paused_total
_wm._set_paused(True, "still", since=_wmnow - 11)                # back-date aims BEFORE the banked span
t("set_paused: back-date clamps to the banked watermark (no double-count)", _wm._pause_at >= _wmnow - 0.5)
_wm._set_paused(False, None)
t("set_paused: the clamped span adds ~nothing extra", _wm.run_paused_total - _bank1 < 1.0)
# reason change focus->still carries the back-date: the idle second(s) BEFORE the focus pause began
# must also stop counting (they were spent standing still)
_rc2 = _mk_pausefake(); _rcnow = time.time()
_rc2._set_paused(True, "focus"); _rc2._pause_at = _rcnow - 1     # focus pause began 1s ago
_rc2._set_paused(True, "still", since=_rcnow - 15)               # ...but they stopped moving 15s ago
t("set_paused: focus->still extends the freeze back to when movement stopped",
  _rc2.pause_reason == "still" and _rc2._pause_at <= _rcnow - 14.5)
_rc3 = _mk_pausefake()
_rc3._set_paused(True, "still", since=time.time() - 12); _pa3 = _rc3._pause_at
_rc3._set_paused(True, "manual")                                 # button press while auto-paused
t("set_paused: manual override keeps the accrued span start", _rc3.pause_reason == "manual" and _rc3._pause_at == _pa3)

def _mk_evalfake(**kw):
    base = dict(mode="run", run_route={"id": "r"}, run_paused=False, pause_reason=None, run_paused_total=0.0,
                _pause_at=0.0, _pause_banked_to=0.0, PAUSE_STILL_SEC=hr.App.PAUSE_STILL_SEC,
                hud=types.SimpleNamespace(misses=0), _last_moved=time.time(),
                cfg={"only_when_game_focused": False, "window_title": "Hytale"})
    base.update(kw)
    f = types.SimpleNamespace(**base)
    f._set_paused = lambda on, reason, since=None: hr.App._set_paused(f, on, reason, since)
    f._style_pause = lambda: None
    f._advance_run = lambda opened=None: None
    return f
_saved_gf = hr.game_focused
hr.game_focused = lambda title: False
_ef = _mk_evalfake(cfg={"only_when_game_focused": True, "window_title": "Hytale"})
hr.App._auto_pause_eval(_ef)
t("auto-pause: an unfocused game freezes the run", _ef.run_paused and _ef.pause_reason == "focus")
hr.game_focused = lambda title: True
hr.App._auto_pause_eval(_ef)
t("auto-pause: focus returning auto-resumes", _ef.run_paused is False)
hr.game_focused = _saved_gf
_ef2 = _mk_evalfake(hud=types.SimpleNamespace(misses=3))
hr.App._auto_pause_eval(_ef2)
t("auto-pause: a covered/unreadable panel (menu or chest UI) freezes the run", _ef2.run_paused and _ef2.pause_reason == "screen")
_ef3 = _mk_evalfake(_last_moved=time.time() - 15)
hr.App._auto_pause_eval(_ef3)
t("auto-pause: standing still past the grace freezes the run", _ef3.run_paused and _ef3.pause_reason == "still")
t("auto-pause: the idle span is back-dated so those seconds are excluded", _ef3._pause_at <= _ef3._last_moved + 0.2)
_ef4 = _mk_evalfake(run_paused=True, pause_reason="manual", _pause_at=time.time() - 5, _last_moved=time.time() - 99)
hr.App._auto_pause_eval(_ef4)
t("auto-pause: a manual pause is never auto-touched", _ef4.run_paused and _ef4.pause_reason == "manual")
_ef5 = _mk_evalfake(mode="idle", run_route=None, run_paused=True, pause_reason="still", _pause_at=time.time() - 3)
hr.App._auto_pause_eval(_ef5)
t("auto-pause: leaving the run clears an automatic pause", _ef5.run_paused is False)

# ---------- pre-reset nudge: alert once in the last 15 min with chests still up ----------
_nnow = time.time()
class _NudgeClient:
    def __init__(self, reset_in, avail, cal=True):
        self._r = _nnow + reset_in; self._n = avail; self.calibration = cal
    def next_reset_epoch(self): return self._r
    def chests(self): return [{"gx": i, "gy": 64, "gz": 0} for i in range(self._n)]
    def chest_on_cooldown(self, e): return False
def _mknudge(reset_in, avail, ign="Rev", cal=True):
    f = types.SimpleNamespace(cfg={"ign": ign}, client=_NudgeClient(reset_in, avail, cal),
                              _reset_nudged=False, statuses=[])
    f.set_status = lambda m, c=None: f.statuses.append(m)
    return f
_fn = _mknudge(600, 5)
hr.App._reset_nudge(_fn)
t("reset nudge: fires when reset <15m with chests up", _fn._reset_nudged and any("Daily reset" in s for s in _fn.statuses))
_fn.statuses.clear(); hr.App._reset_nudge(_fn)
t("reset nudge: fires only ONCE per cycle", _fn.statuses == [])
_fn.client._r = _nnow + 5000; hr.App._reset_nudge(_fn)
t("reset nudge: re-arms once the reset passes", _fn._reset_nudged is False)
_fn2 = _mknudge(300, 0)
hr.App._reset_nudge(_fn2)
t("reset nudge: silent when no chests are up", _fn2.statuses == [] and _fn2._reset_nudged is False)
_fn3 = _mknudge(300, 5, ign="")
hr.App._reset_nudge(_fn3)
t("reset nudge: silent without an IGN", _fn3.statuses == [])
_fn4 = _mknudge(3600, 5)  # >15 min away
hr.App._reset_nudge(_fn4)
t("reset nudge: silent when the reset is far off", _fn4.statuses == [])
# uncalibrated: stay silent AND don't burn the once-per-window flag, so it fires once calibrated
_fn5 = _mknudge(600, 5, cal=None)
hr.App._reset_nudge(_fn5)
t("reset nudge: silent + not consumed while uncalibrated", _fn5.statuses == [] and _fn5._reset_nudged is False)
_fn5.client.calibration = {"type": "calibration"}  # now calibrated, still in the window
hr.App._reset_nudge(_fn5)
t("reset nudge: fires once the map is calibrated within the window", any("Daily reset" in s for s in _fn5.statuses))

# ---------- _learn_leg: mode-independent, order-independent, bounded travel logging ----------
_ll_legs = []
class _LLClient:
    def observe_travel(self, a, b, secs): _ll_legs.append((tuple(a), tuple(b), round(secs)))
    def flush_travel(self): pass
fll = types.SimpleNamespace(client=_LLClient(), session_legs=0, cfg={"travel_max_sec": 300},
                            _last_open=((0, 64, 0), 100.0))
_note = hr.App._learn_leg(fll, (10, 64, 0), 104.0)
t("learn_leg: records a consecutive pair (pair-keyed, mode-independent)",
  _ll_legs == [((0, 64, 0), (10, 64, 0), 4)] and "4s" in _note and fll.session_legs == 1)
for _prev, _now, _label in [(((0, 64, 0), 100.0), 100.5, "sub-second dropped"),
                            (((0, 64, 0), 100.0), 100.0 + 999, "AFK gap (> travel_max) dropped"),
                            (((5, 64, 5), 100.0), 104.0, "same-chest self-leg dropped")]:
    _ll_legs.clear(); fll._last_open = _prev
    hr.App._learn_leg(fll, (5 if "self" in _label else 10, 64, 5 if "self" in _label else 0), _now)
    t("learn_leg: " + _label, _ll_legs == [])
_ll_legs.clear(); fll._last_open = None
hr.App._learn_leg(fll, (10, 64, 0), 104.0)
t("learn_leg: no previous open -> nothing to time", _ll_legs == [])

# ---------- MapClient travel store ----------
cfg = dict(hr.DEFAULT_CONFIG); cfg["ign"] = "B"
c = hr.MapClient(cfg); posts = []
c._req = lambda m, u, b=None: posts.append((m, b)) or {}
c.entries = {"traveltimes": {"id": "traveltimes", "type": "travel", "pairs": {"0,64,0|0,64,10": 50}}}
c.observe_travel((0, 64, 0), (0, 64, 10), 60); c.flush_travel(force=True)
# a slower run never worsens the SYMMETRIC min — but it IS the first measurement of the a->b
# DIRECTION, so it posts a new directed leg record (genuine info) while leaving `pairs` untouched.
t("slower keeps the symmetric min", posts and posts[-1][1]["pairs"]["0,64,0|0,64,10"] == 50)
t("slower records the directed leg", posts[-1][1]["legs"][hr.leg_key("0,64,0", "0,64,10")]["t"] == 60)
c.observe_travel((0, 64, 0), (0, 64, 10), 40)
c.observe_travel((5, 64, 5), (9, 64, 9), 12.4)
c.flush_travel(force=True)
t("faster min-merge posted", posts and posts[-1][1]["pairs"]["0,64,0|0,64,10"] == 40)
t("new pair rounded", posts[-1][1]["pairs"][hr.pair_key("5,64,5", "9,64,9")] == 12)
# the directed leg accumulates: faster time wins t, samples build confidence, recency refreshes
_dleg = posts[-1][1]["legs"][hr.leg_key("0,64,0", "0,64,10")]
t("directed leg: min t + confidence count", _dleg["t"] == 40 and _dleg["n"] == 2)
t("directed leg: B->A kept apart from A->B", hr.leg_key("0,64,10", "0,64,0") not in posts[-1][1]["legs"])
t("same coord ignored", (c.observe_travel((1, 64, 1), (1, 64, 1), 9) or True) and hr.pair_key("1,64,1", "1,64,1") not in c._travel_pending)
t("out of range ignored", (c.observe_travel((1, 64, 1), (3, 64, 3), 0.5) or True) and not c._travel_pending and not c._leg_pending)

# route legTimes mining
# re-queue on failed POST
c3 = hr.MapClient(cfg)
def fail(m, u, b=None): raise OSError("net")
c3._req = fail; c3.entries = {}
c3.observe_travel((0, 64, 0), (0, 64, 10), 40)
try: c3.flush_travel(force=True)
except OSError: pass
t("pending re-queued after fail", bool(c3._travel_pending))
c3._req = lambda m, u, b=None: {}; c3._travel_last_flush = 0; c3.flush_travel()
t("retry drains pending", not c3._travel_pending)

# ---------- counters through _on_chest_open ----------
ecfg = {**cfg, "dry_run": True, "write_key": "hd_k"}  # editor: log creates markers directly
client = hr.MapClient(ecfg); client.me = {"ign": "Ed", "role": "editor", "uuid": "u"}
client.calibration = {"type": "calibration", "ax": 0.001, "bx": 0.5, "az": 0.001, "bz": 0.5}
client.entries = {}
fake = types.SimpleNamespace(mode="log", client=client, run_route=None, run_ix=0, target=None,
    statuses=[], count=0, undo_stack=[], record_nodes=[], session_opens=0, session_legs=0, run_opened=0,
    run_paused=False, _last_open=((-5, 64, -5), time.time() - 20), cfg=ecfg,
    dr=types.SimpleNamespace(pos=[0, 64, 0]))
fake.set_status = lambda m, c=None: fake.statuses.append(m)
fake._update_stats = lambda: None
fake._advance_run = lambda opened=None: None
fake._log_chest = lambda wx, wy, wz, ln: hr.App._log_chest(fake, wx, wy, wz, ln)
fake._submit_here = lambda wx, wy, wz, ln, prefix="": hr.App._submit_here(fake, wx, wy, wz, ln, prefix)
fake._zone_check = lambda wx, wy, wz: hr.App._zone_check(fake, wx, wy, wz)
fake._learn_leg = lambda coords, now: hr.App._learn_leg(fake, coords, now)
hr.App._on_chest_open(fake, 0, 64, 0)
t("session counter++", fake.session_opens == 1)
t("log mode no run counter", fake.run_opened == 0)
tt = client.entries.get("traveltimes", {}).get("pairs", {})
t("consecutive-open travel learned", abs(tt.get(hr.pair_key(hr.coord_key(-5, 64, -5), hr.coord_key(0, 64, 0)), 0) - 20) <= 2)
fake.mode = "run"; fake.run_route = {"id": "autoroute", "nodes": []}
hr.App._on_chest_open(fake, 8, 64, 8)
t("run counter++", fake.run_opened == 1)
t("autoroute id not shared", client.opens["8,64,8"].get("r") is None)

# ---------- opens/cooldown regressions ----------
c4 = hr.MapClient({**cfg, "dry_run": True})
c4.record_open(57, 76, -65)
t("cooldown exact", c4.on_cooldown(57, 76, -65) and not c4.on_cooldown(58, 76, -65))
t("y-agnostic", c4.on_cooldown(57, 0, -65, False))
t("record_open returns True with ign", c4.record_open(1, 1, 1))
t("record_open False without ign", not hr.MapClient({**cfg, "ign": "", "dry_run": True}).record_open(1, 1, 1))

# ---------- daily chest reset (all chests relock together at 8 PM US Eastern) ----------
import calendar as _cal
# summer (EDT, UTC-4): 8 PM ET on Jul 14 2026 == Jul 15 00:00 UTC
_jul15 = _cal.timegm((2026, 7, 15, 0, 0, 0, 0, 0, 0))
t("summer reset at 00:00 UTC (EDT)", hr.last_daily_reset(_jul15 + 3600) == _jul15)
t("before the reset -> previous day's", hr.last_daily_reset(_jul15 - 60) == _jul15 - 86400)
t("exact reset instant counts", hr.last_daily_reset(_jul15) == _jul15)
t("next reset is the following day", hr.next_daily_reset(_jul15 + 3600) == _jul15 + 86400)
# winter (EST, UTC-5): 8 PM ET on Jan 14 2026 == Jan 15 01:00 UTC
_jan15 = _cal.timegm((2026, 1, 15, 1, 0, 0, 0, 0, 0))
t("winter reset at 01:00 UTC (EST)", hr.last_daily_reset(_jan15 + 3600) == _jan15)
# custom reset hour honored (e.g. midnight ET)
t("custom hour honored", hr.last_daily_reset(_jul15 + 3600, hour_et=0) != _jul15)
# DST rule boundaries (2007 US rule)
_mar = [w[_cal.SUNDAY] for w in _cal.monthcalendar(2026, 3) if w[_cal.SUNDAY]]
_nov = [w[_cal.SUNDAY] for w in _cal.monthcalendar(2026, 11) if w[_cal.SUNDAY]]
t("dst on in July, off in January", hr._us_dst_on(2026, 7, 15) and not hr._us_dst_on(2026, 1, 15))
t("dst starts 2nd Sunday of March", hr._us_dst_on(2026, 3, _mar[1]) and not hr._us_dst_on(2026, 3, _mar[1] - 1))
t("dst ends 1st Sunday of November", not hr._us_dst_on(2026, 11, _nov[0]) and hr._us_dst_on(2026, 10, 31))
# on_cooldown pivots on the reset instant, not a rolling window
cr = hr.MapClient({**cfg, "dry_run": True})
_cut = cr.reset_cut_ms()
cr.opens = {"1,2,3": {"t": _cut - 1}, "4,5,6": {"t": _cut + 1}}
t("open before last reset -> available", not cr.on_cooldown(1, 2, 3))
t("open after last reset -> locked", cr.on_cooldown(4, 5, 6))
t("y-agnostic respects the reset", cr.on_cooldown(4, 0, 6, False) and not cr.on_cooldown(1, 0, 3, False))

# manual reset (random in-game unlock event): mark_all_reset advances the cut so everything opened
# before the press reads as up; a chest opened AFTER re-locks; the next scheduled reset supersedes it.
mr = hr.MapClient({**cfg, "dry_run": True})
mr.opens = {"1,2,3": {"t": mr.reset_cut_ms() + 1000}}   # freshly opened -> locked
t("manual reset: chest is locked before the press", mr.on_cooldown(1, 2, 3))
mr.mark_all_reset()
t("manual reset: mark_all_reset persists the cut in cfg", isinstance(mr.cfg.get("manual_reset_ms"), int))
t("manual reset: everything opened before the press reads up", not mr.on_cooldown(1, 2, 3))
t("manual reset: cut equals the manual timestamp", abs(mr.reset_cut_ms() - mr.cfg["manual_reset_ms"]) < 1)
mr.opens["1,2,3"] = {"t": mr.cfg["manual_reset_ms"] + 5000}  # re-open after the reset -> locked again
t("manual reset: re-opening after the press re-locks", mr.on_cooldown(1, 2, 3))
# a stale manual reset (before the last scheduled reset) is ignored — the schedule wins
mr.cfg["manual_reset_ms"] = int(mr._scheduled_cut_ms()) - 86400000
t("manual reset: a stale press is superseded by the scheduled reset", mr.reset_cut_ms() == mr._scheduled_cut_ms())
# App._do_reset_cooldowns: marks the client, persists, refreshes the stats
_rcClient = hr.MapClient({**cfg, "dry_run": True})
_rcN = {"stats": 0}
def _mkreset(mode):
    return types.SimpleNamespace(client=_rcClient, cfg=_rcClient.cfg, mode=mode,
        set_status=lambda *a, **k: None,
        _update_stats=lambda: _rcN.__setitem__("stats", _rcN["stats"] + 1))
_osc, _obp = hr.save_config, hr.beep
hr.save_config = lambda *a, **k: None; hr.beep = lambda *a, **k: None
try:
    hr.App._do_reset_cooldowns(_mkreset("run"))
    hr.App._do_reset_cooldowns(_mkreset("log"))
finally:
    hr.save_config, hr.beep = _osc, _obp
t("reset cooldowns: marks the client reset", isinstance(_rcClient.cfg.get("manual_reset_ms"), int))
t("reset cooldowns: each mode refreshes the stats line", _rcN["stats"] == 2)

# ---------- write-key auth + pending requests ----------
CAL = {"type": "calibration", "ax": 0.001, "bx": 0.5, "az": 0.001, "bz": 0.5}


def mkfake(mode, client):
    f = types.SimpleNamespace(mode=mode, client=client, run_route=None, run_ix=0, target=None,
        statuses=[], count=0, undo_stack=[], record_nodes=[], session_opens=0, session_legs=0,
        run_opened=0, run_started=time.time(), run_paused=False, _last_open=None, cfg=client.cfg,
        dr=types.SimpleNamespace(pos=[0, 64, 0]))
    f.set_status = lambda m, c=None: f.statuses.append(m)
    f._update_stats = lambda: None
    f._advance_run = lambda opened=None: None
    f._log_chest = lambda wx, wy, wz, ln: hr.App._log_chest(f, wx, wy, wz, ln)
    f._submit_here = lambda wx, wy, wz, ln, prefix="": hr.App._submit_here(f, wx, wy, wz, ln, prefix)
    f._confirm_here = lambda wx, wy, wz, ln: hr.App._confirm_here(f, wx, wy, wz, ln)
    f._zone_check = lambda wx, wy, wz: hr.App._zone_check(f, wx, wy, wz)
    f._learn_leg = lambda coords, now: hr.App._learn_leg(f, coords, now)
    return f


# can_edit: identity-driven — only a signed-in editor/owner may edit map structure
def _asrole(client, role, ign="Tester"):
    client.me = {"ign": ign, "role": role, "uuid": "u"} if role else None
    return client
co = hr.MapClient({**cfg, "dry_run": True, "write_key": "hd_x"})
t("not signed in -> cannot edit", not co.can_edit() and not co.signed_in())
_asrole(co, "player")
t("signed-in player -> cannot edit map structure", not co.can_edit() and co.signed_in())
_asrole(co, "editor")
t("signed-in editor -> can_edit", co.can_edit())
_asrole(co, "owner")
t("owner -> can_edit", co.can_edit())

# submit_pending dedup + validation
sp = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": ""}); sp.auth_required = True
sp.calibration = CAL; sp.entries = {}
p = sp.submit_pending(57, 76, -65)
t("submit_pending makes pend- entry", p and p["id"].startswith("pend-") and p["type"] == "pending")
t("submit dedup same spot", sp.submit_pending(58, 76, -65) is None)
sp.entries["c9"] = {"id": "c9", "type": "marker", "kind": "chest", "gx": 100, "gy": 64, "gz": 100, "x": .7, "y": .7}
t("submit skips existing chest", sp.submit_pending(100, 64, 100) is None)
t("pendings excluded from chests", len(sp.chests()) == 1 and len(sp.pendings()) == 1)

# non-editor (keyed server, no key) log -> pending; editor -> real chest
ne = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": ""}); ne.auth_required = True
ne.calibration = CAL; ne.entries = {}
f_ne = mkfake("log", ne); hr.App._on_chest_open(f_ne, 20, 64, 20)
t("non-editor log -> pending", len(ne.pendings()) == 1 and len(ne.chests()) == 0)
ed = hr.MapClient({**cfg, "ign": "Blake", "dry_run": True, "write_key": "hd_k"}); ed.auth_required = True
_asrole(ed, "editor", "Blake"); ed.calibration = CAL; ed.entries = {}
f_ed = mkfake("log", ed); hr.App._on_chest_open(f_ed, 30, 64, 30)
t("editor log -> real chest", len(ed.chests()) == 1 and len(ed.pendings()) == 0)

# --- can_edit is purely role-based now ---
kv = hr.MapClient({**cfg, "dry_run": True, "write_key": "hd_k"}); kv.auth_required = True
_asrole(kv, "editor"); t("role editor -> can_edit", kv.can_edit())
_asrole(kv, "player"); t("role player -> cannot edit (routes to pending)", not kv.can_edit())
_asrole(kv, None);     t("no identity + key present -> cannot edit", not kv.can_edit())

# --- _log_chest never loses a find: a rejected editor write falls back to pending ---
lc = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": "hd_k"})
_asrole(lc, "editor", "Rev"); lc.auth_required = True; lc.calibration = CAL; lc.entries = {}
def _boom(*a): raise RuntimeError("editor key rejected by the server")
lc.add_chest = _boom
hr.App._log_chest(mkfake("log", lc), 12, 64, 12, "")
t("failed editor add -> saved as pending, not lost", len(lc.pendings()) == 1 and len(lc.chests()) == 0)

# --- RUN mode logs unmapped chests found mid-run (used to silently skip them) ---
rn = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": "hd_k"})
_asrole(rn, "editor", "Rev"); rn.auth_required = True; rn.calibration = CAL; rn.entries = {}
f_rn = mkfake("run", rn); f_rn.run_route = {"id": "autoroute", "nodes": []}
hr.App._on_chest_open(f_rn, 60, 64, 60)
t("run mode editor logs unmapped chest", len(rn.chests()) == 1 and len(rn.pendings()) == 0)
rn2 = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": ""})
rn2.auth_required = True; rn2.calibration = CAL; rn2.entries = {}
f_rn2 = mkfake("run", rn2)
hr.App._on_chest_open(f_rn2, 61, 64, 61)
t("run mode non-editor -> pending, not lost", len(rn2.pendings()) == 1)
rn.entries["m-ex"] = {"id": "m-ex", "type": "marker", "kind": "chest", "gx": 70, "gy": 64, "gz": 70, "x": .5, "y": .5}
n_before = len(rn.chests())
hr.App._on_chest_open(mkfake("run", rn), 70, 64, 70)
t("run mode mapped chest -> no duplicate", len(rn.chests()) == n_before)

# --- opening a chest is activity: it clears an AUTOMATIC pause so the open still counts,
#     but a MANUAL pause is respected (only the ▶ button resumes it) ---
def _mk_runopen(client, reason):
    f = mkfake("run", client); f.run_route = {"id": "autoroute", "nodes": []}
    f.run_paused = True; f.pause_reason = reason; f.run_paused_total = 0.0; f._pause_at = time.time() - 3
    f._style_pause = lambda: None
    f._set_paused = lambda on, r, since=None: hr.App._set_paused(f, on, r, since)
    return f
ro = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": "hd_k"})
_asrole(ro, "editor", "Rev"); ro.auth_required = True; ro.calibration = CAL; ro.entries = {}
_fa = _mk_runopen(ro, "still"); hr.App._on_chest_open(_fa, 80, 64, 80)
t("auto-pause: opening a chest resumes the run and the open counts", _fa.run_paused is False and _fa.run_opened == 1)
_fm = _mk_runopen(ro, "manual"); hr.App._on_chest_open(_fm, 81, 64, 81)
t("manual pause: opening a chest does NOT auto-resume, and the open isn't counted",
  _fm.run_paused is True and _fm.run_opened == 0)

# --- a connection-level failure (URLError, not RuntimeError) in the pending path must not
# escape _on_chest_open: run mode still advances, the user gets a status, nothing crashes ---
import urllib.error as _uerr
nf = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": ""})
nf.auth_required = True; nf.calibration = CAL; nf.entries = {}
def _nf_boom(*a, **k): raise _uerr.URLError("connection refused")
nf.submit_pending = _nf_boom
f_nf = mkfake("run", nf)
advanced = []
f_nf._advance_run = lambda opened=None: advanced.append(opened)
hr.App._on_chest_open(f_nf, 90, 64, 90)   # must not raise
t("network failure in pending path doesn't crash", any("⚠" in s for s in f_nf.statuses))
t("network failure still advances the run", advanced == [(90, 64, 90)])

# --- a flaky opens POST must not abort the find (record_open guarded) ---
ro = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": "hd_k"})
_asrole(ro, "editor", "Rev"); ro.auth_required = True; ro.calibration = CAL; ro.entries = {}
def _ro_boom(*a, **k): raise RuntimeError("server error 502")
ro.record_open = _ro_boom
f_ro = mkfake("log", ro)
hr.App._on_chest_open(f_ro, 80, 64, 80)
t("opens POST failure doesn't cost the find", len(ro.chests()) == 1)

# --- --dry-run is session-only: it must never persist into the saved config ---
scfg = dict(hr.DEFAULT_CONFIG); scfg["_dry_cli"] = True
t("--dry-run makes the client dry", hr.MapClient(scfg).dry)
t("config dry_run alone still honored", hr.MapClient({**hr.DEFAULT_CONFIG, "dry_run": True}).dry)
t("no flag, no config -> not dry", not hr.MapClient(dict(hr.DEFAULT_CONFIG)).dry)
import json as _json, tempfile as _tempfile
_old_cp = hr.CONFIG_PATH
hr.CONFIG_PATH = os.path.join(_tempfile.mkdtemp(), "cfg.json")
try:
    hr.save_config(scfg)
    with open(hr.CONFIG_PATH, encoding="utf-8") as _f:
        _saved = _json.load(_f)
finally:
    hr.CONFIG_PATH = _old_cp
t("--dry-run not persisted to config", _saved.get("dry_run") is False and "_dry_cli" not in _saved)

# --- _target_plausible rejects far-off OCR misreads (e.g. a decimal read as a comma) ---
_tp = lambda pos, c: hr.App._target_plausible(types.SimpleNamespace(dr=types.SimpleNamespace(pos=pos)), c)
t("target plausible when near", _tp([60.0, 76.0, -63.0], (57, 76, -65)))
t("open from 7-8 blocks away accepted", _tp([0.0, 64.0, 0.0], (0, 64, 7)) and _tp([0.0, 64.0, 0.0], (6, 65, 5)))
t("target implausible when far (misread)", not _tp([60.0, 76.0, -63.0], (57, 0, 76)))
t("target trusted when no position", _tp(None, (999, 0, 999)))
# Y drifts between polls (dead reckoning is X/Z only): stale frames get a wider Y budget
_tpk = lambda pos, c, **kw: hr.App._target_plausible(types.SimpleNamespace(dr=types.SimpleNamespace(pos=pos)), c, **kw)
t("pit drop before open: stale-frame Y budget widened", _tpk([0.0, 89.0, 0.0], (2, 64, 2), fresh_y=False))
t("fresh-frame Y budget stays strict", not _tpk([0.0, 89.0, 0.0], (2, 64, 2), fresh_y=True))
t("sprint-lag distance accepted", _tpk([0.0, 64.0, 0.0], (25, 64, 10), fresh_y=True))
t("wild misread distance still rejected", not _tpk([0.0, 64.0, 0.0], (80, 64, 0), fresh_y=True))

# --- position-fix jump confirmation (correlated misread can't poison the estimate) ---
drk = hr.DeadReckoner(dict(hr.DEFAULT_CONFIG))
drk.sync([100.0, 64.0, 100.0], None)
t("first fix accepted", drk.pos == [100.0, 64.0, 100.0])
drk.sync([400.0, 64.0, 100.0], None)          # 300-block jump: stash, don't apply
t("wild jump not applied on one read", drk.pos[0] == 100.0)
drk.sync([401.0, 64.0, 100.0], None)          # second consistent read: real teleport
t("consistent second read confirms the jump", drk.pos[0] == 401.0)
drk2 = hr.DeadReckoner(dict(hr.DEFAULT_CONFIG))
drk2.sync([100.0, 64.0, 100.0], None)
drk2.sync([400.0, 64.0, 100.0], None)         # one-frame correlated misread
drk2.sync([100.6, 64.0, 100.0], None)         # reality resumes
t("one-frame misread never lands", abs(drk2.pos[0] - 100.6) < 2)
# a MOVING player whose estimate drifted far must not deadlock: consecutive true fixes are
# several blocks apart, so tracking must recover on the second far fix, not demand agreement
drk3 = hr.DeadReckoner(dict(hr.DEFAULT_CONFIG))
drk3.sync([100.0, 64.0, 100.0], None)         # estimate stuck here (simulated bad drift)
drk3.sync([130.0, 64.0, 100.0], None)         # real fix, far from estimate -> held back
drk3.sync([138.0, 64.0, 100.0], None)         # player kept running: 8 blocks from the last fix
t("moving player recovers from drift in two polls", drk3.pos[0] == 138.0)

# --- game-reported speed drives between-poll motion (walk vs sprint), else the learned fallback ---
_cfgsp = dict(hr.DEFAULT_CONFIG); _cfgsp["move_speed"] = 4.17   # single learned blend
drsp = hr.DeadReckoner(_cfgsp)
t("move_speed: falls back to learned when no OCR speed yet", drsp.move_speed() == 4.17)
drsp.sync([0.0, 64.0, 0.0], None, speed=6.4)   # game says we're sprinting
t("move_speed: uses the fresh reported sprint speed", drsp.move_speed() == 6.4)
drsp.sync([0.0, 64.0, 0.0], None, speed=0.0)   # standing still at the poll
t("move_speed: a ~0 reading falls back to learned (won't freeze a just-started move)", drsp.move_speed() == 4.17)
drsp.sync([0.0, 64.0, 0.0], None, speed=99.0)  # garbage OCR
t("move_speed: implausible speed ignored -> learned", drsp.move_speed() == 4.17)
drsp.sync([0.0, 64.0, 0.0], None, speed=None)  # unreadable Speed line
t("move_speed: None leaves the learned speed in charge", drsp.move_speed() == 4.17)
# staleness: a reported speed is only trusted briefly, then reverts to learned
drst = hr.DeadReckoner(_cfgsp)
drst.sync([0.0, 64.0, 0.0], None, speed=6.4)
drst._ocr_speed_t -= 3.0                        # pretend the reading is 3s old
t("move_speed: stale reported speed reverts to learned", drst.move_speed() == 4.17)

# --- submit_pending must reject coords far outside the calibrated map, not clamp them ---
ob = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": ""}); ob.auth_required = True
ob.calibration = CAL; ob.entries = {}
try:
    ob.submit_pending(5000, 64, 5000)
    t("submit_pending rejects out-of-map coords", False)
except RuntimeError as e:
    t("submit_pending rejects out-of-map coords", "outside the map" in str(e))

# verify mode confirms nearest pending at editor's coords
ev = hr.MapClient({**cfg, "ign": "Blake", "dry_run": True, "write_key": "k"}); ev.auth_required = True
ev.calibration = CAL
ev.entries = {"pend-z": {"id": "pend-z", "type": "pending", "gx": 40, "gy": 64, "gz": 40, "x": .4, "y": .4}}
f_v = mkfake("verify", ev); hr.App._on_chest_open(f_v, 41, 64, 40)
t("verify confirms pending at editor coords", ev.marker_at(41, 64, 40) is not None and not ev.pendings())

# a signed-in editor logs finds directly to the map
op = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": "hd_k"})
op.me = {"ign": "Rev", "role": "editor", "uuid": "u"}; op.calibration = CAL; op.entries = {}
f_op = mkfake("log", op); hr.App._on_chest_open(f_op, 50, 64, 50)
t("editor logs directly to the map", len(op.chests()) == 1 and len(op.pendings()) == 0)

# ---------- contributor credits (leaderboard of confirmed finds/removals) ----------
# credit() accumulates per-IGN tallies in the shared contrib singleton
cb = hr.MapClient({**cfg, "ign": "B", "dry_run": True, "write_key": "k"})
cb.entries = {}
cb.credit("BlakeBiz", found=1)
cb.credit("BlakeBiz", found=1, removed=1)
cb.credit("Rev", removed=1)
_cby = cb.entries["contrib"]["by"]
t("credit accumulates per ign", _cby["blakebiz"]["found"] == 2 and _cby["blakebiz"]["removed"] == 1
  and _cby["rev"]["removed"] == 1 and _cby["blakebiz"]["ign"] == "BlakeBiz")
cb.credit("", found=5); cb.credit(None, found=5)
t("credit ignores blank igns", "found" not in _cby.get("", {}) and len(cb.entries["contrib"]["by"]) == 2)

# editor's own direct log stamps foundBy and credits themselves
dl = hr.MapClient({**cfg, "ign": "Blake", "dry_run": True, "write_key": "k"})
dl.auth_required = True; dl.me = {"ign": "Ed", "role": "editor", "uuid": "u"}; dl.calibration = CAL; dl.entries = {}
f_dl = mkfake("log", dl)
hr.App._on_chest_open(f_dl, 33, 64, 33)
_dm = dl.chests()[0]
t("direct log: foundBy stamped + self credited", _dm.get("foundBy") == "Blake"
  and dl.entries["contrib"]["by"]["blake"]["found"] == 1)

# verify-confirming a pending credits the SUBMITTER, not the editor
vc = hr.MapClient({**cfg, "ign": "Blake", "dry_run": True, "write_key": "k"})
vc.auth_required = True; vc.me = {"ign": "Ed", "role": "editor", "uuid": "u"}; vc.calibration = CAL
vc.entries = {"pend-44_64_44": {"id": "pend-44_64_44", "type": "pending", "gx": 44, "gy": 64, "gz": 44,
                                "x": .5, "y": .5, "by": "Scout"}}
f_vc = mkfake("verify", vc)
hr.App._on_chest_open(f_vc, 44, 64, 44)
t("confirmed pending: submitter credited + stamped", vc.entries["contrib"]["by"]["scout"]["found"] == 1
  and vc.marker_at(44, 64, 44).get("foundBy") == "Scout")

# verify mode: opening a reported-missing chest DISMISSES the removal report (it exists)
_vrm = hr.MapClient({**cfg, "ign": "Ed", "dry_run": True, "write_key": "k"})
_vrm.auth_required = True; _vrm.me = {"ign": "Ed", "role": "editor", "uuid": "u"}; _vrm.calibration = CAL
_vrm.entries = {"mV": {"id": "mV", "type": "marker", "kind": "chest", "gx": 77, "gy": 64, "gz": 77, "x": .5, "y": .5},
                "pend-rm-77_64_77": {"id": "pend-rm-77_64_77", "type": "pending", "kind": "remove",
                                     "gx": 77, "gy": 64, "gz": 77, "x": .5, "y": .5, "by": "Scout"}}
_fvr = types.SimpleNamespace(client=_vrm, statuses=[], count=0, undo_stack=[])
_fvr.set_status = lambda m, c=None: _fvr.statuses.append(m)
hr.App._confirm_here(_fvr, 77, 64, 77, "")
t("verify: opening a reported-missing chest dismisses the report",
  not _vrm.pendings() and _vrm.marker_at(77, 64, 77) is not None
  and any("dismissed" in s for s in _fvr.statuses))

# ---------- game-HUD signals: chest counter + reset countdown ----------
# structured panel parse — layout verbatim from the real HUD screenshot (movable panel,
# semi-transparent over the stats menu, one row per area, '>' marks the current one)
_PANEL = [
    ("HISTATU DUNGEON WORLD", 100),
    ("Name: BlakeBiz", 118),                     # background bleed-through
    ("- THE HOLLOW -", 130),
    ("CHESTS reset 8h 33m", 160),
    ("Money Rank: #8 Solmara 58/327", 190),      # background merged into a row's line
    ("Thornvale 0/61", 220),
    ("Glacaris 39/175", 250),
    ("Hours Played: 297", 262),                  # background-only line — ignored
    ("Blackfen 20/118", 280),
    ("> The Hollow 44/99", 310),
]
_pp = hr.parse_hud_panel(_PANEL)
t("hud panel: all five areas parse through the bleed-through", _pp is not None
  and len(_pp["areas"]) == 5 and _pp["areas"]["Solmara"] == (58, 327)
  and _pp["areas"]["The Hollow"] == (44, 99))
t("hud panel: current area from the '>' row", _pp["current"] == "The Hollow")
t("hud panel: reset countdown", _pp["reset"] == 8 * 3600 + 33 * 60)
t("hud panel: band spans the panel", _pp["y0"] == 130 and _pp["y1"] == 310)
_pp2 = hr.parse_hud_panel([("- THORNVALE -", 10), ("CHESTS reset 26m", 20),
                           ("Thornvale 0/61", 30), ("Glacaris 39/175", 40)])
t("hud panel: title names current when no '>' read", _pp2 is not None and _pp2["current"] == "Thornvale")
t("hud panel: unrelated screen text -> None", hr.parse_hud_panel([
    ("VRAM: 5812 / 12282 MB SMALL WIND TEMPLE CHEST", 10),
    ("Draws/Tris: 820 / 1704645", 20)]) is None)
t("hud panel: one lone pair without reset -> None", hr.parse_hud_panel([("Solmara 58/327", 10)]) is None)
t("hud reset: h+m", hr.parse_hud_reset("CHESTS reset 19h 26m") == 19 * 3600 + 26 * 60)
t("hud reset: minutes only", hr.parse_hud_reset("chests reset 26m 46/321") == 26 * 60)
t("hud reset: seconds only", hr.parse_hud_reset("CHESTS > x reset 45s") == 45)
t("hud reset: needs the CHESTS anchor", hr.parse_hud_reset("player reset 30 s ago") is None)
t("hud reset: bare word -> None", hr.parse_hud_reset("chests reset  46/321") is None)
t("hud reset: absent -> None", hr.parse_hud_reset("Position: (1,2,3)") is None)

# counter tick v2 (per-area rows): exactly ONE row moving +1, confirmed by a second agreeing
# reading; location captured at detection; the open queued to the worker with the ROW's area
def mkhud(last_open=None, fallback=(9, 64, 9), mode="log"):
    f = types.SimpleNamespace(_hud_count=None, _hud_pending_open=None, _last_open=last_open,
                              mode=mode, _last_target={"coords": fallback, "at": time.time()},
                              jobs_put=[], statuses=[])
    f._covered_fallback = lambda now: fallback
    f.jobs = types.SimpleNamespace(put=lambda j: f.jobs_put.append(j))
    f.set_status = lambda m, c=None: f.statuses.append(m)
    return f
now0 = time.time()
R0 = {"Solmara": (58, 327), "The Hollow": (44, 99)}
R1 = {"Solmara": (58, 327), "The Hollow": (45, 99)}   # +1 in The Hollow only
fh = mkhud()
hr.App._hud_count_tick(fh, R0, now0)
hr.App._hud_count_tick(fh, R1, now0 + 8)
t("hud tick: single +1 reading never fires", fh.jobs_put == [])
hr.App._hud_count_tick(fh, R1, now0 + 16)
t("hud tick: confirmed +1 queues with the row's area",
  fh.jobs_put == [("hudopen", (9, 64, 9), now0 + 8, "The Hollow")])
fh2 = mkhud()                                      # one-frame misread heals
hr.App._hud_count_tick(fh2, R0, now0)
hr.App._hud_count_tick(fh2, R1, now0 + 8)
hr.App._hud_count_tick(fh2, R0, now0 + 16)
t("hud tick: one-frame misread never fires", fh2.jobs_put == [])
fh3 = mkhud()                                      # two rows moving = re-read noise
hr.App._hud_count_tick(fh3, R0, now0)
hr.App._hud_count_tick(fh3, {"Solmara": (59, 327), "The Hollow": (45, 99)}, now0 + 8)
hr.App._hud_count_tick(fh3, {"Solmara": (59, 327), "The Hollow": (45, 99)}, now0 + 16)
t("hud tick: two rows moving never fires", fh3.jobs_put == [])
fh4 = mkhud(last_open=((1, 1, 1), now0 + 7))       # F-key recorded an open near detection
hr.App._hud_count_tick(fh4, R0, now0)
hr.App._hud_count_tick(fh4, R1, now0 + 8)
hr.App._hud_count_tick(fh4, R1, now0 + 16)
t("hud tick: F-key open near detection dedups", fh4.jobs_put == [])
fh5 = mkhud()                                      # total change = misread, never an open
hr.App._hud_count_tick(fh5, R0, now0)
hr.App._hud_count_tick(fh5, {"Solmara": (58, 327), "The Hollow": (45, 100)}, now0 + 8)
hr.App._hud_count_tick(fh5, {"Solmara": (58, 327), "The Hollow": (45, 100)}, now0 + 16)
t("hud tick: total change never fires", fh5.jobs_put == [])
fh6 = mkhud(mode="idle")
hr.App._hud_count_tick(fh6, R0, now0)
hr.App._hud_count_tick(fh6, R1, now0 + 8)
hr.App._hud_count_tick(fh6, R1, now0 + 16)
t("hud tick: idle mode never fires", fh6.jobs_put == [])
fh7 = mkhud()                                      # OCR flicker: the row vanishes for a frame
hr.App._hud_count_tick(fh7, R0, now0)
hr.App._hud_count_tick(fh7, R1, now0 + 8)          # +1 -> pending
hr.App._hud_count_tick(fh7, {"Solmara": (58, 327)}, now0 + 16)  # Hollow row missing this frame
t("hud tick: missing row keeps the pending alive", fh7._hud_pending_open is not None and fh7.jobs_put == [])
hr.App._hud_count_tick(fh7, R1, now0 + 24)         # row returns at the same value -> confirmed
t("hud tick: reappearing row confirms the open", [j[3] for j in fh7.jobs_put] == ["The Hollow"])

# area stamps + observed area totals
ac = hr.MapClient({**cfg, "ign": "B", "dry_run": True, "write_key": "k"})
ac.calibration = CAL; ac.entries = {}; ac.current_area = "The Hollow"
t("marker carries the HUD area stamp", ac.add_chest(5, 64, 5).get("area") == "The Hollow")
ac.auth_required = True
_apnd = ac.submit_pending(60, 64, 60)
t("pending carries the HUD area stamp", _apnd is not None and _apnd.get("area") == "The Hollow")
at = hr.MapClient({**cfg, "ign": "B", "dry_run": True, "write_key": "k"})
at.auth_required = True; at.me = {"ign": "Ed", "role": "editor", "uuid": "u"}
at.entries = {
    "area-solmara": {"id": "area-solmara", "type": "area", "name": "Solmara", "points": [[0, 0], [1, 0], [1, 1]]},
    "area-the-hollow": {"id": "area-the-hollow", "type": "area", "name": "The Hollow", "points": [[0, 0], [1, 0], [1, 1]]},
    "areatotals": {"id": "areatotals", "type": "areatotals",
                   "areas": {"solmara": {"name": "Solmara", "total": 320},
                             "junk-idle-hollow": {"name": "Idle Hollow", "total": 99}}},
}
fa = types.SimpleNamespace(client=at, _areatotals_t=0, _areatotals_cand=None)
_R = {"Solmara": (58, 327), "The Hollow": (44, 99), "Idle The Hollow": (44, 99)}  # last = OCR bleed
hr.App._push_area_totals(fa, _R, time.time())
t("areatotals: first changed reading never publishes",
  at.entries["areatotals"]["areas"]["solmara"]["total"] == 320)
hr.App._push_area_totals(fa, _R, time.time())
_aa = at.entries["areatotals"]["areas"]
t("areatotals: two agreeing readings publish polygon-known areas",
  _aa["solmara"]["total"] == 327 and _aa["the-hollow"]["total"] == 99)
t("areatotals: bleed-through junk name never published", "idle-the-hollow" not in _aa)
t("areatotals: stored junk slug pruned on push", "junk-idle-hollow" not in _aa)
fa2 = types.SimpleNamespace(client=at, _areatotals_t=0, _areatotals_cand=None)
hr.App._push_area_totals(fa2, {"Solmara": (60, 327)}, time.time())
hr.App._push_area_totals(fa2, {"Solmara": (60, 327)}, time.time())
t("areatotals: unchanged totals -> no push", fa2._areatotals_t == 0)
at2 = hr.MapClient({**cfg, "ign": "B", "dry_run": True, "write_key": ""}); at2.auth_required = True
fa3 = types.SimpleNamespace(client=at2, _areatotals_t=0, _areatotals_cand=None)
hr.App._push_area_totals(fa3, {"Solmara": (58, 327)}, time.time())
t("areatotals: non-editors never push", "areatotals" not in at2.entries)

# reset tick: a NEW observation needs two agreeing readings before it overrides + persists
_rcfg = dict(hr.DEFAULT_CONFIG)
fr2 = types.SimpleNamespace(client=types.SimpleNamespace(reset_override=None), cfg=_rcfg,
                            _hud_reset_cand=None, _update_stats=lambda: None)
_old_cp2 = hr.CONFIG_PATH
hr.CONFIG_PATH = os.path.join(_tempfile.mkdtemp(), "cfg.json")
try:
    hr.App._hud_reset_tick(fr2, 3600, now0)        # first sighting: candidate only
    t("hud reset tick: single reading never commits", fr2.client.reset_override is None)
    hr.App._hud_reset_tick(fr2, 3592, now0 + 8)    # second agreeing reading -> adopted
    t("hud reset tick: two agreeing readings commit + persist",
      abs(fr2.client.reset_override - (now0 + 3600)) < 90 and _rcfg["hud_reset_epoch"] is not None)
    _committed = fr2.client.reset_override
    hr.App._hud_reset_tick(fr2, _committed - (now0 + 30), now0 + 30)  # agrees with current: no-op
    t("hud reset tick: jitter within 90s kept", fr2.client.reset_override == _committed)
    hr.App._hud_reset_tick(fr2, 34000, now0 + 60)  # one-frame digit-drop misread (10h off)
    t("hud reset tick: one-frame misread never commits", fr2.client.reset_override == _committed)
    hr.App._hud_reset_tick(fr2, 7200, now0 + 90)   # genuine change, first sighting
    hr.App._hud_reset_tick(fr2, 7195, now0 + 98)   # confirmed
    t("hud reset tick: confirmed change adopted", abs(fr2.client.reset_override - (now0 + 90 + 7200)) < 90)
finally:
    hr.CONFIG_PATH = _old_cp2

# MapClient honors the override. An observation ON a whole ET hour pins the DST-aware model to
# that hour; an off-hour observation uses the exact ±24h cycle around the instant.
def _offhour(ts):  # nudge a timestamp so it can't be within 3 min of a whole ET hour
    return ts if hr.reset_hour_from_epoch(ts) is None else ts - 600
_h21 = hr._reset_at(2026, 7, 20, 21)              # 9 PM ET on a summer date
t("reset hour derived from an on-hour observation", hr.reset_hour_from_epoch(_h21) == 21)
t("off-hour observation derives no hour", hr.reset_hour_from_epoch(_h21 + 1750) is None)
ovh = hr.MapClient({**cfg, "dry_run": True})
ovh.reset_override = _h21                          # server runs a 9 PM ET reset
t("on-hour override pins the DST model", ovh.reset_cut_ms() == hr.last_daily_reset(hour_et=21) * 1000.0
  and ovh.next_reset_epoch() == hr.next_daily_reset(hour_et=21))
ov = hr.MapClient({**cfg, "dry_run": True})
ov.reset_override = _offhour(time.time() + 3600)  # off-hour observed reset in ~1h
t("off-hour future: cut is ~23h ago", abs(ov.reset_cut_ms() / 1000.0 - (ov.reset_override - 86400)) < 1)
t("off-hour future: next is the observation", ov.next_reset_epoch() == ov.reset_override)
ov.reset_override = _offhour(time.time() - 100)   # off-hour reset just happened
t("off-hour passed: cut is the observation", abs(ov.reset_cut_ms() / 1000.0 - ov.reset_override) < 1)
t("off-hour passed: next is +24h", abs(ov.next_reset_epoch() - (ov.reset_override + 86400)) < 1)
ov.reset_override = _offhour(time.time() - 3 * 86400)  # stale off-hour: back to the config model
t("override stale: falls back to model", ov.reset_cut_ms() == hr.last_daily_reset(hour_et=20) * 1000.0)

# ---------- self-update: download URL resolution ----------
# _resolve_download_url: REAL two-server harness (endpoint 302 -> a DIFFERENT host). This drives
# the actual _NoRedirect opener through urllib's real redirect/error machinery, proving the 302
# Location is read from OUR endpoint and the redirect is NOT auto-followed to the CDN host during
# resolve. (A fake opener would bypass _NoRedirect and couldn't catch a reintroduced follow.)
import http.server as _hs, socketserver as _ss
_hits = {"cdn_calls": 0}
def _mkhandler(kind, redirect_to=None):
    class _H(_hs.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if kind == "cdn":
                _hits["cdn_calls"] += 1
            if redirect_to:
                self.send_response(302); self.send_header("Location", redirect_to); self.end_headers()
            else:
                self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    return _H
_cdn = _ss.TCPServer(("127.0.0.1", 0), _mkhandler("cdn")); _cdn_port = _cdn.server_address[1]
_ep = _ss.TCPServer(("127.0.0.1", 0), _mkhandler("endpoint", "http://127.0.0.1:%d/signed" % _cdn_port))
_ep_port = _ep.server_address[1]
threading.Thread(target=_cdn.serve_forever, daemon=True).start()
threading.Thread(target=_ep.serve_forever, daemon=True).start()
_rs = types.SimpleNamespace(cfg={"write_key": "KEY"})
try:
    _loc = hr.App._resolve_download_url(_rs, "http://127.0.0.1:%d/api/download" % _ep_port)
finally:
    _cdn.shutdown(); _ep.shutdown()
t("resolve dl: returns the 302 Location (signed CDN url)", _loc == "http://127.0.0.1:%d/signed" % _cdn_port)
t("resolve dl: CDN host is NOT auto-followed during resolve", _hits["cdn_calls"] == 0)

# ---------- self-update swap mechanics ----------
_swp = _tempfile.mkdtemp()
_exe = os.path.join(_swp, "app.exe"); _new = os.path.join(_swp, "app.new.exe")
open(_exe, "w").write("OLD"); open(_new, "w").write("NEW")
_bak = hr.self_update_swap(_exe, _new)
t("swap: new binary takes the name", open(_exe).read() == "NEW")
t("swap: old binary kept as backup", _bak.endswith(".old") and open(_bak).read() == "OLD")
open(_exe, "w").write("CUR")
try:
    hr.self_update_swap(_exe, os.path.join(_swp, "missing.exe"))
    t("swap: failure restores the original", False)
except Exception:
    t("swap: failure restores the original", open(_exe).read() == "CUR")

# ---------- zone-review flags (in-game area vs drawn boundary) ----------
# point_in_poly must TOGGLE on crossings (the JS twin once had a set-instead-of-toggle bug
# that classified everything LEFT of a polygon as inside)
_SQ = [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]
t("point_in_poly: inside", hr.point_in_poly(0.7, 0.5, _SQ))
t("point_in_poly: left of the polygon is OUTSIDE", not hr.point_in_poly(0.2, 0.3, _SQ))
t("point_in_poly: right of the polygon is outside", not hr.point_in_poly(1.2, 0.5, _SQ))

def mkzones():
    c = hr.MapClient({**cfg, "ign": "Rev", "dry_run": True, "write_key": ""})
    c.auth_required = True
    c.calibration = {"type": "calibration", "ax": 0.001, "bx": 0.5, "az": 0.001, "bz": 0.5}
    c.entries = {
        "area-thornvale": {"id": "area-thornvale", "type": "area", "name": "Thornvale",
                           "points": [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]},
        "area-glacaris": {"id": "area-glacaris", "type": "area", "name": "Glacaris",
                          "points": [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]},
    }
    f = types.SimpleNamespace(client=c, statuses=[])
    f.set_status = lambda m, col=None: f.statuses.append(m)
    return c, f

# chest at world (-100, 64, 0) -> frac x=0.4 (Thornvale); HUD says Glacaris -> mismatch flag
zc, zf = mkzones()
zc.current_area = "Glacaris"
hr.App._zone_check(zf, -100, 64, 0)
_zn = zc.zone_flag_at(-100, 64, 0)
t("zone mismatch files a flag", _zn is not None and _zn["kind"] == "zone"
  and "Glacaris" in _zn["note"] and "Thornvale" in _zn["note"])
t("zone flag dedups", hr.App._zone_check(zf, -100, 64, 0) is None and
  len([p for p in zc.pendings() if p.get("kind") == "zone"]) == 1)
# agreement -> no flag
zc2, zf2 = mkzones(); zc2.current_area = "Thornvale"
hr.App._zone_check(zf2, -100, 64, 0)
t("matching zone never flags", zc2.zone_flag_at(-100, 64, 0) is None)
# HUD name that isn't a drawn polygon (OCR junk) -> no flag
zc3, zf3 = mkzones(); zc3.current_area = "Idle Glacaris"
hr.App._zone_check(zf3, -100, 64, 0)
t("unknown HUD area never flags", zc3.zone_flag_at(-100, 64, 0) is None)
# chest outside every polygon -> no flag
zc4, zf4 = mkzones(); zc4.current_area = "Glacaris"
hr.App._zone_check(zf4, 5000, 64, 0)
t("unzoned chest never flags", not [p for p in zc4.pendings() if p.get("kind") == "zone"])
# no HUD area at all -> no flag
zc5, zf5 = mkzones(); zc5.current_area = None
hr.App._zone_check(zf5, -100, 64, 0)
t("no HUD reading never flags", zc5.zone_flag_at(-100, 64, 0) is None)
# zone flags are invisible to location-proposal lookups and verify targeting
zc.entries["pend-5_64_5"] = {"id": "pend-5_64_5", "type": "pending", "gx": -100, "gy": 64, "gz": 0,
                             "x": .4, "y": .5}
t("pending_at ignores zone flags", zc.pending_at(-100, 64, 0)["id"] == "pend-5_64_5")
zc.entries.pop("pend-5_64_5")

# ---------- cooldown-locked chests never count (repeated F can't inflate anything) ----------
lk = hr.MapClient({**cfg, "ign": "B", "dry_run": True, "write_key": "k"})
lk.auth_required = True; lk.me = {"ign": "Ed", "role": "editor", "uuid": "u"}; lk.calibration = CAL
lk.entries = {"mL": {"id": "mL", "type": "marker", "kind": "chest", "gx": 21, "gy": 64, "gz": 21, "x": .5, "y": .5}}
f_lk = mkfake("log", lk)
hr.App._on_chest_open(f_lk, 21, 64, 21)            # first open: real
_t1 = lk.opens["21,64,21"]["t"]
time.sleep(0.02)
hr.App._on_chest_open(f_lk, 21, 64, 21)            # spam F on the now-locked chest
hr.App._on_chest_open(f_lk, 21, 64, 21)
t("locked chest: session counter counts once", f_lk.session_opens == 1)
t("locked chest: cooldown timestamp NOT refreshed", lk.opens["21,64,21"]["t"] == _t1)
t("locked chest: status says not counted", any("not counted" in s for s in f_lk.statuses))
f_rk = mkfake("run", lk)
f_rk.run_route = {"id": "autoroute", "nodes": []}
hr.App._on_chest_open(f_rk, 21, 64, 21)            # still on cooldown in run mode
t("locked chest: run counter untouched", f_rk.run_opened == 0)
lk2 = hr.MapClient({**cfg, "ign": "B", "dry_run": True, "write_key": "k"})
lk2.auth_required = True; lk2.me = {"ign": "Ed", "role": "editor", "uuid": "u"}; lk2.calibration = CAL; lk2.entries = {}
f_lg = mkfake("log", lk2)
hr.App._on_chest_open(f_lg, 30, 64, 30)
f_lg._last_open = ((30, 64, 30), time.time() - 20)  # backdate so the leg is timeable
hr.App._on_chest_open(f_lg, 31, 64, 40)            # a second REAL open still counts + times legs
t("real opens still count", f_lg.session_opens == 2 and f_lg.session_legs == 1)

# ---------- per-zone stats for the current HUD zone ----------
zsC = hr.MapClient({**cfg, "ign": "B", "dry_run": True})
zsC.calibration = CAL
zsC.entries = {
    "area-the-hollow": {"id": "area-the-hollow", "type": "area", "name": "The Hollow",
                        "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]},
    "areatotals": {"id": "areatotals", "type": "areatotals",
                   "areas": {"the-hollow": {"name": "The Hollow", "total": 99}}},
    "mA": {"id": "mA", "type": "marker", "kind": "chest", "gx": 1, "gy": 64, "gz": 1, "x": .2, "y": .2},
    "mB": {"id": "mB", "type": "marker", "kind": "chest", "gx": 2, "gy": 64, "gz": 2, "x": .4, "y": .4},
    "mG": {"id": "mG", "type": "marker", "kind": "group", "count": 5, "gx": 3, "gy": 64, "gz": 3, "x": .6, "y": .6},
}
zsC.record_open(1, 64, 1)  # mA is on cooldown for this player
f_zs = types.SimpleNamespace(client=zsC, _hud_area="The Hollow",
                             _hud_count=({"The Hollow": (44, 99)}, time.time()))
_z = hr.App._zone_stats(f_zs)
t("zone stats: mapped counts groups", _z is not None and _z[2] == 7)
t("zone stats: cooldown excluded from 'up'", _z[1] == 6)
t("zone stats: undiscovered from the game total", _z[3] == 92)
f_zs2 = types.SimpleNamespace(client=zsC, _hud_area=None, _hud_count=None)
t("zone stats: no HUD zone -> None", hr.App._zone_stats(f_zs2) is None)
zsC.entries.pop("area-the-hollow")
_z3 = hr.App._zone_stats(f_zs)
t("zone stats: no polygon -> totals only", _z3 == ("The Hollow", None, None, 99))

# ---------- real 1440p panel shapes (verbatim from player screenshots, 2026-07-16) ----------
# this layout prints DEGREE SYMBOLS, a Rotation line right after Target, "Target: -" when idle,
# and floats "Blocked -5.4" / "+120 Block XP" toasts over the panel area
_R1440_IDLE = ("WORLD Position: (4.127, 68.000, -241.742) Chunk: (4, 4, 14) in (0, 2, -8) "
               "Orientation: (-3.0°, 152.6°, 0.0°) South Environment: Env_Zone3_Mountains "
               "Weather: Histatu_Glacaris_Blizzard Heightmap: 67 Velocity: (-0.004, 0.000, 0.002) "
               "Wish Dir: (0.000, 0.000) Speed: 0.00 State (old): Idle Mode: Adventure Target: -")
t("1440p: position parses", hr.parse_position(_R1440_IDLE) == (4.127, 68.0, -241.742))
t("1440p: yaw parses through degree symbols", hr.parse_yaw(_R1440_IDLE) == hr.wrap_deg(152.6))
t("1440p: idle 'Target: -' parses to None", hr.parse_target_block(_R1440_IDLE) is None)
_R1440_AIM = ("Position: (-12.813, 68.000, -227.189) Orientation: (-28.6°, -84.4°, 0.0°) East "
              "Target: Block @ (-11.000, 67.000, -227.000) "
              "*Furniture_Dungeon_Chest_Epic_State_Default_CloseWindow "
              "Rotation: (0.0°, 0.0°, 0.0°) North (0, 0, 0) Hitbox: 0 BoxId: 0 "
              "Blocked -5.4 +120 Block XP Lv. 87")
_r_aim = hr.parse_target_block(_R1440_AIM)
t("1440p: aimed chest parses (Rotation line not adopted)", _r_aim is not None
  and hr.chest_coords(_r_aim) == (-11, 67, -227))
# with the target's own coords smeared, the trailing Rotation/(0,0,0) must never be adopted
t("1440p: smeared coords + trailing rotation -> None", hr.parse_target_block(
    "Position: (-12.813, 68.000, -227.189) Target: Block @ sm##red garbage here totally "
    "Rotation: (0.0°, 0.0°, 0.0°) North (0, 0, 0) Hitbox: 0") is None)
# degree-symbol triples can never be read as coordinate triples ("+120 Block XP" anchors, too)
t("1440p: 'Blocked' never anchors as Block", hr.parse_target_block(
    "Blocked -5.4 +120 Block XP Lv. 87 19,315/27,499 XP") is None)
# chest-open occlusion at this layout: the panel's line TAILS survive (label hidden) -> no parse
t("1440p: occluded label fails closed", hr.parse_position("00, -227.142) 1, 2, -8) 3.1°, 0.0°) East") is None)

# --- update version comparison ---
t("parse_version basic", hr.parse_version("v1.2.3") == (1, 2, 3))
t("parse_version short", hr.parse_version("1.4") == (1, 4))
t("parse_version junk -> (0,)", hr.parse_version("nightly") == (0,))
t("version ordering: newer tag > current", hr.parse_version("v1.2.0") > hr.parse_version("1.1.9"))
t("version ordering: same not newer", not (hr.parse_version("v1.0.0") > hr.parse_version("1.0.0")))
t("version ordering: patch bump", hr.parse_version("v1.0.1") > hr.parse_version("v1.0.0"))

# --- resolution-adaptive OCR scales (raised to ~3000px target: 1440p digits were dropping) ---
t("ocr_scales returns two factors in 2..5", all(2 <= s <= 5 for s in hr.ocr_scales(730)) and len(hr.ocr_scales(730)) == 2)
t("ocr_scales: 1440p strip (~973px) now upscales >=3", min(hr.ocr_scales(973)) >= 3)
t("ocr_scales: 1080p strip (~730px) upscales harder", hr.ocr_scales(730) == [4, 5])
t("ocr_scales: 4K strip (~1460px) stays modest", max(hr.ocr_scales(1460)) <= 3)
t("ocr_scales: tiny region clamps to <=5", max(hr.ocr_scales(200)) == 5)
t("ocr_scales: huge region clamps to >=2", min(hr.ocr_scales(5000)) == 2)

# --- interior-digit-loss fail-open (the 1440p '-106 -> -1' report) ---
# document that a decimal-SURVIVING truncation parses cleanly: NUMF requires decimals, and a
# digit-dropped '-1.000' HAS them — so this can't be caught at the parse layer (the real fix is
# the OCR-resolution bump that stops the digit from dropping in the first place).
t("interior-digit-loss still parses (why it needs the resolution fix)",
  hr.parse_position("Position: (-1.000, 67.000, -2.000) Chunk") == (-1.0, 67.0, -2.0))
# but a genuinely occluded read (no decimals) is still rejected
t("occlusion without decimals still rejected", hr.parse_position("Position: (-1, 67.000, -2.000) x") is None)

# --- optional log-by-aim key shares the 'chest' action ---
_hk = lambda **kw: hr.App._hotkey_map(types.SimpleNamespace(cfg={"hotkey_chest": "F", "hotkey_undo": "F10", **kw}))
t("hotkey_map: no log key by default", "chest" in _hk().values() and len(_hk()) == 2)
t("hotkey_map: log key maps to chest action", _hk(hotkey_log="G").get("G") == "chest" and _hk(hotkey_log="G").get("F") == "chest")
t("hotkey_map: blank log key is ignored", "" not in _hk(hotkey_log="").keys())

# --- covered-panel fallback: chest_coords + fresh-cache gating ---
t("chest_coords: chest block -> rounded ints", hr.chest_coords({"coords": (57.6, 76.0, -65.2), "block": "Furniture_Village_Chest_Large"}) == (58, 76, -65))
t("chest_coords: non-chest block -> None", hr.chest_coords({"coords": (1, 2, 3), "block": "Oak_Door"}) is None)
t("chest_coords: no block -> None", hr.chest_coords({"coords": (1, 2, 3), "block": None}) is None)
t("chest_coords: None target -> None", hr.chest_coords(None) is None)
# _near: spatial gate (distance + facing)
_near = lambda pos, yaw, coords: hr.App._near(types.SimpleNamespace(), pos, yaw, coords)
_face = hr.bearing_to(0, 0, 0, 4)  # yaw that looks from (0,0) toward a chest at (0,4)
t("_near: close + facing -> True", _near([0.0, 64.0, 0.0], _face, (0, 64, 4)) is True)
t("_near: too far -> False", _near([0.0, 64.0, 0.0], _face, (0, 64, 20)) is False)
t("_near: facing away -> False", _near([0.0, 64.0, 0.0], hr.wrap_deg(_face + 180), (0, 64, 4)) is False)
t("_near: no yaw uses distance only", _near([0.0, 64.0, 0.0], None, (0, 64, 4)) is True)

# _covered_fallback: a covered-panel open is attributed to the chest you're STANDING AT + FACING;
# run mode considers only the route's REMAINING stops; log uses the recent cache. All gated.
_now = time.time()
def _cf(mode, last_target, pos, yaw, stops=None):
    f = types.SimpleNamespace(mode=mode, _last_target=last_target,
                              dr=types.SimpleNamespace(pos=pos, yaw=yaw),
                              run_route=({"nodes": []} if stops is not None else None))
    f._near = lambda p, y, c: hr.App._near(f, p, y, c)
    f._run_stop_coords = lambda remaining_only=False: (stops or [])
    return hr.App._covered_fallback(f, _now)
t("fb run: at a route stop you're facing -> that stop",
  _cf("run", None, [0.0, 64.0, 0.0], _face, stops=[("n0", 0, 64, 3)]) == (0, 64, 3))
# you open the NEARER stop you're standing at, even with another stop also in range —
# opening one chest must never skip a different route chest you didn't open
t("fb run: opens the stop you're AT, not a farther stop also in range",
  _cf("run", None, [0.0, 64.0, 0.0], _face,
      stops=[("n0", 0, 64, 3), ("n1", 0, 64, 8)]) == (0, 64, 3))
t("fb run: not standing at any route stop -> None",
  _cf("run", None, [0.0, 64.0, 0.0], _face, stops=[("n0", 0, 64, 40)]) is None)
t("fb log: fresh cache you're at -> its coords", _cf("log", {"coords": (0, 64, 4), "at": _now}, [0.0, 64.0, 0.0], _face) == (0, 64, 4))
t("fb log: 7-block open still falls back", _cf("log", {"coords": (0, 64, 7), "at": _now}, [0.0, 64.0, 0.0], _face) == (0, 64, 7))
t("fb log: cache behind you -> None", _cf("log", {"coords": (0, 64, 4), "at": _now}, [0.0, 64.0, 0.0], hr.wrap_deg(_face + 180)) is None)
t("fb log: stale cache -> None", _cf("log", {"coords": (0, 64, 4), "at": _now - 10}, [0.0, 64.0, 0.0], _face) is None)
t("fb: no position estimate -> None", _cf("log", {"coords": (0, 64, 4), "at": _now}, None, 0.0) is None)
t("fb record: NO cache fallback (shared route)", _cf("record", {"coords": (0, 64, 4), "at": _now}, [0.0, 64.0, 0.0], _face) is None)
# verify must never fall back to the pending's submitted coords (that would rubber-stamp it) —
# but the editor's OWN recent aim-cache reading is fine
t("fb verify: NO fallback without the editor's own reading", _cf("verify", None, [0.0, 64.0, 0.0], _face) is None)
t("fb verify: editor's own aim cache allowed", _cf("verify", {"coords": (0, 64, 4), "at": _now}, [0.0, 64.0, 0.0], _face) == (0, 64, 4))
t("_near: chest stacked far in Y -> False", _near([0.0, 64.0, 0.0], None, (0, 80, 2)) is False)

# _do_chest end-to-end with mocked frame/OCR
def mkchestfake(hud_result, last_target, mode="log", target=None, pos=None, yaw=None, client=None):
    if client is None:
        client = hr.MapClient({**cfg, "dry_run": True})
        client.entries = {}
    f = types.SimpleNamespace(mode=mode, cfg=cfg, _last_target=last_target, target=target, opened=[], statuses=[],
                              client=client, _success_log_t=0, _debug=None)
    f._log_read_debug = lambda reason, hud: None
    f.dr = types.SimpleNamespace(snapshot=lambda: 0, sync=lambda *a, **k: False, pos=pos, yaw=yaw)
    f.hud = types.SimpleNamespace(read=lambda frame, want_target=False, thorough=False: hud_result)
    f._on_chest_open = lambda wx, wy, wz: f.opened.append((wx, wy, wz))
    f.set_status = lambda m, c=None: f.statuses.append(m)
    f._near = lambda p, y, c: hr.App._near(f, p, y, c)
    f._covered_fallback = lambda now: hr.App._covered_fallback(f, now)
    f._target_plausible = lambda c, **kw: hr.App._target_plausible(f, c, **kw)
    f._log_read_debug = lambda *a, **k: None
    return f
_orig_grab = hr.grab_game
hr.grab_game = lambda cfg: None  # OCR is mocked via hud.read, so the frame is unused
try:
    ff = mkchestfake({"position": None, "yaw": None, "target": None},
                     {"coords": (7, 64, 7), "at": time.time()}, pos=[7.0, 64.0, 5.0], yaw=None)
    hr.App._do_chest(ff)
    t("covered panel logs from fresh nearby cache", ff.opened == [(7, 64, 7)])
    t("cache is single-use (cleared after fallback)", ff._last_target is None)
    ff2 = mkchestfake({"position": None, "yaw": None, "target": {"coords": (3, 64, 3), "block": "Village_Chest"}},
                      None, pos=[3.0, 64.0, 4.0])
    hr.App._do_chest(ff2)
    t("direct read logs chest + warms cache", ff2.opened == [(3, 64, 3)] and ff2._last_target["coords"] == (3, 64, 3))
    # a far-off "chest" read (another panel line's numbers) is rejected even when THIS frame's
    # Position line didn't parse — the gate runs against the dead-reckoned position
    ff7 = mkchestfake({"position": None, "yaw": None, "target": {"coords": (0, 180, 0), "block": "Village_Chest"}},
                      None, pos=[60.0, 76.0, -63.0])
    hr.App._do_chest(ff7)
    t("unsynced frame still gates far misreads", ff7.opened == [] and any("not near it" in s for s in ff7.statuses))
    # no position estimate at all -> an unverifiable read must not reach the shared map
    ff8 = mkchestfake({"position": None, "yaw": None, "target": {"coords": (0, 0, 0), "block": "Village_Chest"}},
                      None, pos=None)
    hr.App._do_chest(ff8)
    t("no position fix -> unverifiable read refused", ff8.opened == [] and any("no position fix" in s for s in ff8.statuses))
    ff3 = mkchestfake({"position": None, "yaw": None, "target": {"coords": (1, 1, 1), "block": "Oak_Door"}},
                      {"coords": (9, 9, 9), "at": time.time()}, pos=[9.0, 9.0, 9.0])
    hr.App._do_chest(ff3)
    t("non-chest block ignored, no fallback", ff3.opened == [])
    # nameless coords at an UNKNOWN spot never log directly — but they no longer block the
    # spatially-gated covered-panel fallback (pre-fix 4K behavior, where names never parse)
    ff5 = mkchestfake({"position": None, "yaw": None, "target": {"coords": (2, 2, 2), "block": None}},
                      {"coords": (8, 8, 8), "at": time.time()}, pos=[8.0, 8.0, 8.0])
    hr.App._do_chest(ff5)
    t("nameless unknown read -> gated fallback, never its own coords", ff5.opened == [(8, 8, 8)])
    ff5b = mkchestfake({"position": None, "yaw": None, "target": {"coords": (2, 2, 2), "block": None}},
                       None, pos=[8.0, 8.0, 8.0])
    hr.App._do_chest(ff5b)
    t("nameless unknown read + no cache -> nothing logged", ff5b.opened == [])
    # nameless coords that MATCH a known chest location are safely an open of that chest
    kc = hr.MapClient({**cfg, "dry_run": True})
    kc.entries = {"mK": {"id": "mK", "type": "marker", "kind": "chest", "gx": 4, "gy": 64, "gz": 4, "x": .5, "y": .5}}
    ff9 = mkchestfake({"position": None, "yaw": None, "target": {"coords": (4, 64, 4), "block": None}},
                      None, pos=[3.0, 64.0, 3.0], client=kc)
    hr.App._do_chest(ff9)
    t("nameless read at a KNOWN marker -> opens it", ff9.opened == [(4, 64, 4)])
    ff4 = mkchestfake({"position": None, "yaw": None, "target": None},
                      {"coords": (5, 5, 5), "at": time.time()}, pos=[50.0, 5.0, 50.0])  # cache far away
    hr.App._do_chest(ff4)
    t("blank read + far cache does not log", ff4.opened == [] and any("Log-by-aim" in s for s in ff4.statuses))
    ff6 = mkchestfake({"position": None, "yaw": None, "target": None},
                      {"coords": (5, 5, 5), "at": time.time() - hr.FALLBACK_MAX_AGE - 1}, pos=[5.0, 5.0, 5.0])
    hr.App._do_chest(ff6)
    t("blank read + stale cache does not log", ff6.opened == [])
finally:
    hr.grab_game = _orig_grab

# ---------- Capture Doctor: one-time per-device setup probes ----------
_FULLTXT = ("Position: (60.677, 76.000, -63.178) Orientation: (-6.4, 78.0, 0.0) West "
            "Velocity: (-4.24, 0.000, 4.24) Speed: 6.48 Wish Dir: (-1.000, 1.000)")
_POSTXT = "Position: (60.677, 76.000, -63.178)"

# score_strip_scales ranks by how much of the panel parses (position outweighs everything)
_oo = hr.ocr_text
hr.ocr_text = lambda strip, sc: {3: _POSTXT, 4: _FULLTXT}.get(sc, "static noise")
try:
    _rk = hr.score_strip_scales("strip")
    t("setup scales: fullest parse ranks first", _rk[0][0] == 4 and _rk[0][1] == 11)
    t("setup scales: position-only second", _rk[1][0] == 3 and _rk[1][1] == 6)
    t("setup scales: garbage scales score 0", all(r[1] == 0 for r in _rk[2:]))
    t("setup scales: field flags reported", _rk[0][2]["yaw"] and not _rk[1][2]["yaw"])
    # position must DOMINATE: a scale reading ONLY the loose motion trio + yaw (5 pts) can
    # never outrank one that reads Position alone (6 pts)
    _MOTION = "Orientation: (-6.4, 78.0, 0.0) West Velocity: (-4.24, 0.000, 4.24) Speed: 6.48 Wish Dir: (-1.000, 1.000)"
    hr.ocr_text = lambda strip, sc: {2: _MOTION, 5: _POSTXT}.get(sc, "")
    _rk2 = hr.score_strip_scales("strip")
    t("setup scales: Position outranks all motion lines combined", _rk2[0][0] == 5 and _rk2[0][1] == 6)
    hr.ocr_text = lambda strip, sc: _POSTXT  # every scale equal -> cheapest (smallest) wins ties
    t("setup scales: ties break to the cheaper scale", hr.score_strip_scales("s")[0][0] == hr.SETUP_SCALES[0])
    # effective width outside the runtime hint gate is skipped outright — above 7500 is a
    # ~70MP resize; below 2400 is the mid-number digit-drop regime that could lock an
    # unusable (gate-rejected) hint with a wrong-but-parseable position
    _calls = []
    hr.ocr_text = lambda strip, sc: _calls.append(sc) or _POSTXT
    hr.score_strip_scales(types.SimpleNamespace(width=1460))
    t("setup scales: >7500px effective width never attempted", 6 not in _calls and 5 in _calls)
    _calls2 = []
    hr.ocr_text = lambda strip, sc: _calls2.append(sc) or _POSTXT
    hr.score_strip_scales(types.SimpleNamespace(width=730))   # 1080p strip: x2/x3 are sub-2400
    t("setup scales: sub-2400px effective width never attempted (gate parity)",
      2 not in _calls2 and 3 not in _calls2 and 4 in _calls2)
finally:
    hr.ocr_text = _oo

# probe_panel aggregates scales + per-field hit-rates across frames; injectable capture
class _FFrame:
    size = (1920, 1080)
    def crop(self, box): return self
    width, height = 1920, 1080
_oscore, _ofind = hr.score_strip_scales, hr.find_game_window
hr.find_game_window = lambda title: (0, 0, 800, 600)
_pp_calls = {"n": 0}
def _fake_scores(strip):
    _pp_calls["n"] += 1
    ok = _pp_calls["n"] > 1   # frame 1: nothing parses; frames 2-3: scale 4 reads position+yaw
    f_good = {"position": ok, "yaw": ok, "speed": False, "velocity": False, "wishdir": False}
    f_none = {k: False for k in f_good}
    return [(4, (5 if ok else 0), f_good), (2, 0, f_none)]
hr.score_strip_scales = _fake_scores
try:
    _pp = hr.probe_panel({"window_title": "Hytale"}, samples=3, delay=0, grab=lambda cfg: _FFrame(),
                         sleep=lambda s: None)
    t("probe_panel: all frames captured", _pp["frames"] == 3 and _pp["window"] is True)
    t("probe_panel: best scale = highest total score", _pp["best"] == 4 and _pp["scores"][4] == 10)
    t("probe_panel: field hit-rates across frames", abs(_pp["fields"]["position"] - 2 / 3) < 1e-9
      and _pp["fields"]["speed"] == 0.0)
    hr.score_strip_scales = lambda strip: [(4, 0, {k: False for k in ("position", "yaw", "speed", "velocity", "wishdir")})]
    _pp0 = hr.probe_panel({"window_title": "H"}, samples=2, delay=0, grab=lambda cfg: _FFrame(),
                          sleep=lambda s: None)
    t("probe_panel: nothing parsed -> no best scale (no false lock)", _pp0["best"] is None)
    # a scale that parses only motion lines (positive score, position never) must NOT become the hint
    hr.score_strip_scales = lambda strip: [(2, 3, {"position": False, "yaw": True, "speed": True,
                                                   "velocity": False, "wishdir": False})]
    _ppm = hr.probe_panel({"window_title": "H"}, samples=2, delay=0, grab=lambda cfg: _FFrame(),
                          sleep=lambda s: None)
    t("probe_panel: a position-blind scale never becomes the hint", _ppm["best"] is None)
    # position RELIABILITY outranks total score: 3/3 position frames beat 1/3 + motion padding
    _rel = {"n": 0}
    def _rel_scores(strip):
        _rel["n"] += 1
        f_pad = {"position": _rel["n"] == 1, "yaw": True, "speed": True, "velocity": True, "wishdir": True}
        f_pos = {"position": True, "yaw": False, "speed": False, "velocity": False, "wishdir": False}
        return [(3, (11 if _rel["n"] == 1 else 5), f_pad), (4, 6, f_pos)]
    hr.score_strip_scales = _rel_scores
    _ppr = hr.probe_panel({"window_title": "H"}, samples=3, delay=0, grab=lambda cfg: _FFrame(),
                          sleep=lambda s: None)
    t("probe_panel: every-frame position beats a higher-scoring sometimes-position scale", _ppr["best"] == 4)
    def _boomgrab(cfg): raise RuntimeError("no screen")
    _ppf = hr.probe_panel({"window_title": "H"}, samples=2, delay=0, grab=_boomgrab, sleep=lambda s: None)
    t("probe_panel: capture failure -> zero frames, no crash", _ppf["frames"] == 0 and _ppf["best"] is None)
finally:
    hr.score_strip_scales, hr.find_game_window = _oscore, _ofind

# probe_movement: scripted reader + fake clock — measures pace and checks yaw vs travel direction
def _mk_reader(outs):
    it = iter(outs)
    return types.SimpleNamespace(read=lambda frame: next(it))
_clk = {"t": 1000.0}
def _fnow(): return _clk["t"]
def _fsleep(s): _clk["t"] += s
def _mv(x, yaw, speed=6.0, exact=True):
    return {"position": (x, 64.0, -50.0), "yaw": yaw, "yaw_exact": exact, "speed": speed}
_clk["t"] = 1000.0
# running +x at 9 blocks/s: heading = atan2(-(dx), 0) = -90; matching yaw -> yaw_ok
_mvres = hr.probe_movement({}, seconds=4, interval=1.0, grab=lambda cfg: "f",
                           reader=_mk_reader([_mv(0, -90), _mv(9, -90), _mv(18, -90), _mv(27, -90)]),
                           sleep=_fsleep, now=_fnow)
t("probe_movement: pace = median of per-segment speeds", _mvres["hits"] == 4 and abs(_mvres["speed_bps"] - 9.0) < 0.01)
t("probe_movement: game speed = median of Speed lines", _mvres["game_speed"] == 6.0)
t("probe_movement: yaw agreeing with travel direction -> ok", _mvres["yaw_ok"] is True)
_clk["t"] = 1000.0
_mvbad = hr.probe_movement({}, seconds=4, interval=1.0, grab=lambda cfg: "f",
                           reader=_mk_reader([_mv(0, 90), _mv(9, 90), _mv(18, 90), _mv(27, 90)]),
                           sleep=_fsleep, now=_fnow)
t("probe_movement: yaw 180-degrees off travel -> flagged", _mvbad["yaw_ok"] is False)
_clk["t"] = 1000.0
_mv1 = hr.probe_movement({}, seconds=3, interval=1.0, grab=lambda cfg: "f",
                         reader=_mk_reader([_mv(0, 0), {"position": None, "yaw": None, "speed": None},
                                            {"position": None, "yaw": None, "speed": None}]),
                         sleep=_fsleep, now=_fnow)
t("probe_movement: <2 fixes -> no speed, no yaw verdict", _mv1["speed_bps"] is None and _mv1["yaw_ok"] is None)
# stationary alt-tab lead-in + zero Speed lines: those segments/samples must NOT drag the estimate.
# zeros are a strict MAJORITY of the Speed samples on purpose — an unfiltered median would be 0.0,
# so this assertion actually pins the >0.5 filter (with a zero minority it couldn't)
_clk["t"] = 1000.0
_mvlead = hr.probe_movement({}, seconds=5, interval=1.0, grab=lambda cfg: "f",
                            reader=_mk_reader([_mv(0, -90, speed=0.0), _mv(0, -90, speed=0.0),
                                               _mv(0, -90, speed=0.0), _mv(9, -90), _mv(18, -90)]),
                            sleep=_fsleep, now=_fnow)
t("probe_movement: stationary lead-in excluded from the pace", abs(_mvlead["speed_bps"] - 9.0) < 0.01)
t("probe_movement: zero Speed lines excluded from the game-speed median", _mvlead["game_speed"] == 6.0)
# a single corrupted coordinate (mid-number digit drop ~100 b/s) is filtered, not averaged in
_clk["t"] = 1000.0
_mvcor = hr.probe_movement({}, seconds=5, interval=1.0, grab=lambda cfg: "f",
                           reader=_mk_reader([_mv(0, -90), _mv(9, -90), _mv(18, -90), _mv(27, -90),
                                              _mv(130, -90)]),
                           sleep=_fsleep, now=_fnow)
t("probe_movement: a corrupted endpoint coordinate is rejected", abs(_mvcor["speed_bps"] - 9.0) < 0.01)
# coarse compass yaw (yaw_exact False, quantized to 90-degree buckets) must never fail the device
_clk["t"] = 1000.0
_mvcmp = hr.probe_movement({}, seconds=4, interval=1.0, grab=lambda cfg: "f",
                           reader=_mk_reader([_mv(0, -45, exact=False), _mv(9, -45, exact=False),
                                              _mv(18, -45, exact=False), _mv(27, -45, exact=False)]),
                           sleep=_fsleep, now=_fnow)
t("probe_movement: compass-fallback yaw -> no verdict (not a false fail)", _mvcmp["yaw_ok"] is None)
# a mid-test TURN disqualifies those segments instead of failing a healthy yaw
_clk["t"] = 1000.0
_mvturn = hr.probe_movement({}, seconds=4, interval=1.0, grab=lambda cfg: "f",
                            reader=_mk_reader([_mv(0, -90), _mv(9, -90), _mv(18, 0), _mv(18.1, 0)]),
                            sleep=_fsleep, now=_fnow)
t("probe_movement: turning segments are disqualified, agreeing ones still vote", _mvturn["yaw_ok"] is True)
# timestamps must be FRAME-GRAB time: a slow first read (cold OCR) must not compress the span.
# the fake frame embeds the true x at grab time; the fake read then burns asymmetric OCR time.
_clk["t"] = 1000.0
_readn = {"n": 0}
def _slowgrab(cfg):
    return 9.0 * (_clk["t"] - 1000.0)         # "frame" = true x when the frame was grabbed
def _slowread(frame):
    _readn["n"] += 1
    _clk["t"] += 1.2 if _readn["n"] == 1 else 0.1   # cold first read, cheap band reads after
    return {"position": (frame, 64.0, -50.0), "yaw": -90, "yaw_exact": True, "speed": 9.0}
_mvts = hr.probe_movement({}, seconds=4, interval=1.0, grab=_slowgrab,
                          reader=types.SimpleNamespace(read=_slowread), sleep=_fsleep, now=_fnow)
t("probe_movement: grab-time timestamps -> slow cold OCR cannot inflate the pace",
  _mvts["speed_bps"] is not None and abs(_mvts["speed_bps"] - 9.0) < 0.01)

# apply_setup_results: folds probes into cfg; partial results apply independently
_cfgA = {"move_speed": 4.5, "setup_done": 0, "setup_health": None, "ocr_scale_hint": None}
_nA = hr.apply_setup_results(_cfgA, panel={"frames": 3, "best": 4, "fields": {"position": 1.0}, "size": (1920, 1080)},
                             movement={"reads": 5, "hits": 5, "speed_bps": 9.42, "game_speed": 6.4, "yaw_ok": True})
t("setup apply: locks the measured OCR zoom", _cfgA["ocr_scale_hint"] == 4)
t("setup apply: a sprint pace does NOT overwrite the walking move_speed", _cfgA["move_speed"] == 4.5)
t("setup apply: marks done + healthy baseline", _cfgA["setup_done"] == hr.SETUP_VERSION
  and _cfgA["setup_health"]["panel"]["best"] == 4 and _cfgA["setup_health"]["skipped"] is False)
t("setup apply: human notes describe the tuning", any("zoom" in n for n in _nA))
_cfgB = {"move_speed": 4.5, "setup_done": 0, "setup_health": None, "ocr_scale_hint": None}
_nB = hr.apply_setup_results(_cfgB, movement={"reads": 5, "hits": 5, "speed_bps": 4.9, "game_speed": None, "yaw_ok": None})
t("setup apply: a walking-gait pace seeds move_speed", _cfgB["move_speed"] == 4.9
  and any("walk" in n for n in _nB))
_cfgC = {"move_speed": 4.5, "setup_done": 0, "setup_health": None, "ocr_scale_hint": None}
hr.apply_setup_results(_cfgC, movement={"reads": 3, "hits": 2, "speed_bps": 55.0, "game_speed": None, "yaw_ok": None})
t("setup apply: an implausible measured pace is ignored", _cfgC["move_speed"] == 4.5)
_cfgD = {"move_speed": 4.5, "setup_done": 0, "setup_health": None, "ocr_scale_hint": None}
hr.apply_setup_results(_cfgD, completed=False)
t("setup apply: closing early still marks done (no re-nag) but flags skipped",
  _cfgD["setup_done"] == hr.SETUP_VERSION and _cfgD["setup_health"]["skipped"] is True
  and _cfgD["ocr_scale_hint"] is None)
# a PARTIAL re-run merges into the stored baseline: re-timing the pace must not erase the panel
# block (the capture-regression nag keys off it)
_cfgE = {"move_speed": 4.5, "setup_done": hr.SETUP_VERSION,
         "setup_health": {"at": 1, "skipped": False,
                          "panel": {"best": 4, "fields": {"position": 1.0}, "size": [1920, 1080]}},
         "ocr_scale_hint": 4}
hr.apply_setup_results(_cfgE, movement={"reads": 5, "hits": 5, "speed_bps": 8.8, "game_speed": 6.4, "yaw_ok": True})
t("setup apply: movement-only re-run keeps the stored panel baseline",
  _cfgE["setup_health"]["panel"]["fields"]["position"] == 1.0
  and _cfgE["setup_health"]["movement"]["speed_bps"] == 8.8)

# HudReader tries the device's measured-best scale FIRST (sweep stays as fallback)
class _FStrip:
    def __init__(self, w, h): self.width, self.height, self.size = w, h, (w, h)
    def crop(self, box): return _FStrip(box[2] - box[0], box[3] - box[1])
_olines = hr.ocr_lines
_seen_scales = []
def _rec_lines(region, scale):
    _seen_scales.append(scale)
    return [("Position: (60.677, 76.000, -63.178) Orientation: (-6.4, 78.0, 0.0) West", 10)]
hr.ocr_lines = _rec_lines
try:
    _hr1 = hr.HudReader({"ocr_scale_hint": 6})
    _hr1.read(_FStrip(1920, 1080))
    t("scale hint: tried first on the panel strip", _seen_scales[0] == 6)
    _seen_scales.clear()
    _hr2 = hr.HudReader({"ocr_scale_hint": None})
    _hr2.read(_FStrip(1920, 1080))
    t("scale hint: absent -> normal computed sweep", _seen_scales[0] == hr.ocr_scales(int(1920 * 0.38))[0])
    _seen_scales.clear()
    _hr3 = hr.HudReader({"ocr_scale_hint": 99})   # out-of-range junk from a hand-edited config
    _hr3.read(_FStrip(1920, 1080))
    t("scale hint: junk value ignored", _seen_scales[0] != 99)
    # a stale hint from a DIFFERENT resolution is gated by effective OCR width: too small would
    # reintroduce the mid-number digit-drop regime; too large is a ~70MP resize per poll
    _seen_scales.clear()
    _hr4 = hr.HudReader({"ocr_scale_hint": 2})    # 730px strip * 2 = 1460 < 2400 -> unsafe, unused
    _hr4.read(_FStrip(1920, 1080))
    t("scale hint: below the safe effective width -> ignored", _seen_scales[0] != 2)
    _seen_scales.clear()
    _hr5 = hr.HudReader({"ocr_scale_hint": 6})    # 1520px strip * 6 = 9120 > 7500 -> absurd, unused
    _hr5.read(_FStrip(4000, 2000))
    t("scale hint: absurd effective width -> ignored", _seen_scales[0] != 6)
finally:
    hr.ocr_lines = _olines

# capture-health regression nag: once per session, only with a healthy stored baseline, and any
# malformed hand-edited setup_health must fail quiet (the tick pipeline runs after it)
def _mk_nagfake(health, misses=15, mode="run"):
    f = types.SimpleNamespace(_setup_nag=False, mode=mode, statuses=[],
                              hud=types.SimpleNamespace(misses=misses),
                              cfg={"setup_health": health})
    f.set_status = lambda m, c=None: f.statuses.append(m)
    return f
_ng = _mk_nagfake({"panel": {"fields": {"position": 1.0}}})
hr.App._setup_nag_check(_ng)
t("setup nag: healthy baseline + hard misses -> one hint", len(_ng.statuses) == 1 and _ng._setup_nag)
hr.App._setup_nag_check(_ng)
t("setup nag: fires at most once per session", len(_ng.statuses) == 1)
_ng2 = _mk_nagfake({"panel": {"fields": {"position": 1.0}}}, misses=25)
hr.App._setup_nag_check(_ng2)
t("setup nag: >= threshold (a skipped ==12 tick cannot mute it)", len(_ng2.statuses) == 1)
_ng3 = _mk_nagfake(None)
hr.App._setup_nag_check(_ng3)
t("setup nag: no baseline -> silent", _ng3.statuses == [])
_ng4 = _mk_nagfake({"panel": {"fields": {"position": 0.0}}})
hr.App._setup_nag_check(_ng4)
t("setup nag: unhealthy baseline -> silent (panel never read at setup either)", _ng4.statuses == [])
for _bad in ("yes", ["x"], {"panel": "broken"}, {"panel": {"fields": None}},
             {"panel": {"fields": {"position": "high"}}}):
    _ngb = _mk_nagfake(_bad)
    hr.App._setup_nag_check(_ngb)   # must not raise
    t("setup nag: malformed setup_health %r -> quiet, no crash" % (_bad,), _ngb.statuses == [])
_ng5 = _mk_nagfake({"panel": {"fields": {"position": 1.0}}}, misses=3)
hr.App._setup_nag_check(_ng5)
t("setup nag: healthy reads -> silent", _ng5.statuses == [] and not _ng5._setup_nag)
_ng6 = _mk_nagfake({"panel": {"fields": {"position": 1.0}}}, mode="idle")
hr.App._setup_nag_check(_ng6)
t("setup nag: idle mode -> silent (no OCR expected)", _ng6.statuses == [])

print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
