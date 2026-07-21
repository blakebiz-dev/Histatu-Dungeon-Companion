// Unit tests for api/dungeon.js validation + merge/cap/auth helpers.
// Run:  node api/__tests__/dungeon-validate.test.js
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "dungeon.js"), "utf8");
const mod = { exports: {} };
new Function("module", "exports", "require", "process",
  src + "\nmodule.exports._validEntry = validEntry;"
)(mod, mod.exports, require, { env: {} });
const v = mod.exports._validEntry;

let fail = 0;
const t = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + " " + name); if (!cond) fail++; };

t("chesttype removed -> rejected (any id)", v({ id: "mythic", type: "chesttype", name: "x", color: "#112233", weight: 5, loot: [] }) === null);
t("mobcat without mc- prefix rejected", v({ id: "boss", type: "mobcat", name: "Boss", mobcoin: 5, dailyLimit: null }) === null);
const mc = v({ id: "mc-boss", type: "mobcat", name: "Boss", mobcoin: 5, dailyLimit: 300 });
t("mobcat with mc- prefix ok", mc !== null && mc.mobcoin === 5 && mc.dailyLimit === 300);
t("mobcat dailyLimit too large rejected", v({ id: "mc-boss", type: "mobcat", name: "B", mobcoin: 5, dailyLimit: 2e15 }) === null);
t("mobcat null dailyLimit stays null", v({ id: "mc-x", type: "mobcat", name: "X", mobcoin: 1 }).dailyLimit === null);
const mob = v({ id: "m1", kind: "mob", x: 0.5, y: 0.5, spawnAmount: 0, xp: 10, category: "boss" });
t("mob marker keeps explicit spawnAmount 0", mob !== null && mob.spawnAmount === 0);
t("mob marker keeps category slug", mob.category === "boss");
t("route still validates", v({ id: "r1", type: "route", nodes: ["a", "b"], legTimes: [5], name: "R" }) !== null);
t("chest marker no longer carries rarity", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, rarity: "mythic", diff: 1 }); return m !== null && !("rarity" in m) && m.diff === 1; })());


t("calibration valid", (() => { const c = v({ id: "calibration", type: "calibration", ax: 0.0031, bx: 0.52, az: 0.0029, bz: 0.48, setBy: "Blake" }); return c && c.ax === 0.0031 && c.setBy === "Blake"; })());
t("calibration wrong id rejected", v({ id: "cal2", type: "calibration", ax: 1, bx: 0, az: 1, bz: 0 }) === null);
t("calibration missing coeff rejected", v({ id: "calibration", type: "calibration", ax: 1, bx: 0, az: 1 }) === null);
t("calibration non-finite rejected", v({ id: "calibration", type: "calibration", ax: "abc", bx: 0, az: 1, bz: 0 }) === null);
t("companion marker payload accepted", (() => { const m = v({ id: "cap19c4f2e3a01042", type: "marker", kind: "chest", x: 0.41, y: 0.33, gx: -29, gy: 65, gz: -4, name: "", note: "" }); return m && m.kind === "chest" && m.gx === -29; })());
t("companion mob payload accepted", (() => { const m = v({ id: "cap19c4f2e3a01043", type: "marker", kind: "mob", x: 0.4, y: 0.3, gx: 1, gy: 2, gz: 3, xp: 0, spawnAmount: 1, category: "normal" }); return m && m.category === "normal" && m.spawnAmount === 1; })());
// teleport: a navigation waypoint — accepted kind, base fields only, no chest/mob-specific data
t("teleport marker accepted with base fields only", (() => { const m = v({ id: "tp1", type: "marker", kind: "teleport", x: 0.6, y: 0.4, gx: 120, gy: 70, gz: -30, name: "North spire" }); return m && m.kind === "teleport" && m.gx === 120 && m.name === "North spire"; })());
t("teleport carries no chest/mob fields (no diff/spawnAmount coercion)", (() => { const m = v({ id: "tp2", kind: "teleport", x: 0.5, y: 0.5, diff: 2, spawnAmount: 9, count: 5 }); return m && m.diff === undefined && m.spawnAmount === undefined && m.count === undefined; })());
t("unknown marker kind still falls back to chest", (() => { const m = v({ id: "c9", kind: "portal", x: 0.5, y: 0.5 }); return m && m.kind === "chest"; })());
t("chest difficulty defaults to Normal (1)", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5 }); return m && m.diff === 1; })());
t("chest difficulty from the fixed set kept", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, diff: 2 }); return m && m.diff === 2; })());
t("chest difficulty 5 (Impossible) kept", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, diff: 5 }); return m && m.diff === 5; })());
t("chest difficulty off-set falls back to 1", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, diff: 3 }); return m && m.diff === 1; })());
t("chest difficulty junk falls back to 1", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, diff: "hard" }); return m && m.diff === 1; })());
t("group/mob markers get no diff field", (() => { const g = v({ id: "g1", kind: "group", x: 0.5, y: 0.5, count: 3 }); const mo = v({ id: "m1", kind: "mob", x: 0.5, y: 0.5 }); return g && g.diff === undefined && mo && mo.diff === undefined; })());


t("opens valid", (() => { const o = v({ id: "opens-blake", type: "opens", ign: "Blake", opens: { "57,76,-65": { t: 1783966765957, r: "r1abc" }, "-14,64,-30": { t: 1783966765000 } } }); return o && o.opens["57,76,-65"].r === "r1abc" && o.opens["-14,64,-30"].t === 1783966765000 && o.opens["-14,64,-30"].r === undefined; })());
t("opens bad id prefix rejected", v({ id: "open-blake", type: "opens", ign: "Blake", opens: {} }) === null);
t("opens missing ign rejected", v({ id: "opens-blake", type: "opens", ign: "", opens: {} }) === null);
t("opens bad coord key rejected", v({ id: "opens-blake", type: "opens", ign: "B", opens: { "57,76": { t: 1 } } }) === null);
t("opens bad timestamp rejected", v({ id: "opens-blake", type: "opens", ign: "B", opens: { "1,2,3": { t: "soon" } } }) === null);
t("opens bad route id rejected", v({ id: "opens-blake", type: "opens", ign: "B", opens: { "1,2,3": { t: 5, r: "bad id!" } } }) === null);
t("opens array rejected", v({ id: "opens-blake", type: "opens", ign: "B", opens: [] }) === null);
t("opens too many keys rejected", (() => { const o = {}; for (let i = 0; i < 601; i++) o[i + ",1,1"] = { t: 1 }; return v({ id: "opens-blake", type: "opens", ign: "B", opens: o }) === null; })());


t("chesttype with opens- id rejected", v({ id: "opens-blake", type: "chesttype", name: "x", color: "#112233", weight: 5, loot: [] }) === null);
t("route with opens- id rejected", v({ id: "opens-blake", type: "route", nodes: ["a"], name: "R" }) === null);
t("route with mc- id rejected", v({ id: "mc-boss", type: "route", nodes: ["a"], name: "R" }) === null);
t("marker with opens- id rejected", v({ id: "opens-blake", kind: "chest", x: 0.5, y: 0.5 }) === null);
t("marker with mc- id rejected", v({ id: "mc-boss", kind: "chest", x: 0.5, y: 0.5 }) === null);
t("opens future timestamp clamped not rejected", (() => { const o = v({ id: "opens-b", type: "opens", ign: "B", opens: { "1,2,3": { t: 3.9e12 } } }); return o && o.opens["1,2,3"].t <= Date.now() + 5 * 60 * 1000 + 1000; })());
t("opens sane timestamp kept exact", (() => { const now = Date.now(); const o = v({ id: "opens-b", type: "opens", ign: "B", opens: { "1,2,3": { t: now - 5000 } } }); return o && o.opens["1,2,3"].t === now - 5000; })());


t("travel valid", (() => { const tr = v({ id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 42.6, "-7,8,9|1,2,3": 1 } }); return tr && tr.pairs["1,2,3|4,5,6"] === 43 && tr.pairs["-7,8,9|1,2,3"] === 1; })());
t("travel wrong id rejected", v({ id: "traveltimes2", type: "travel", pairs: {} }) === null);
t("travel bad pair key rejected", v({ id: "traveltimes", type: "travel", pairs: { "1,2|3,4,5": 5 } }) === null);
t("travel zero seconds rejected", v({ id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 0 } }) === null);
t("travel huge seconds rejected", v({ id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 4000 } }) === null);
// directed legs (Phase 2): optional, additive — an ABSENT legs field stays undefined (the marker of
// a legs-unaware client; the POST handler uses it to preserve stored legs on editor overwrites)
t("travel legs absent -> undefined (legs-unaware marker)", (() => { const tr = v({ id: "traveltimes", type: "travel", pairs: {} }); return tr && tr.legs === undefined; })());
t("travel legs explicitly {} -> defined empty (wholesale-aware client)", (() => { const tr = v({ id: "traveltimes", type: "travel", pairs: {}, legs: {} }); return tr && typeof tr.legs === "object" && Object.keys(tr.legs).length === 0; })());
t("travel legs valid directed record", (() => { const tr = v({ id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 12.7, n: 3, at: 1700000000 } } }); const r = tr && tr.legs["1,2,3>4,5,6"]; return r && r.t === 13 && r.n === 3 && r.at === 1700000000; })());
t("travel legs bad key (symmetric |) rejected", v({ id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3|4,5,6": { t: 12, n: 1, at: 1 } } }) === null);
t("travel legs zero seconds rejected", v({ id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 0, n: 1, at: 1 } } }) === null);
t("travel legs n<1 rejected", v({ id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 12, n: 0, at: 1 } } }) === null);
t("travel legs n over cap rejected", v({ id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 12, n: 999, at: 1 } } }) === null);
t("travel legs far-future stamp rejected", v({ id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 12, n: 1, at: Math.floor(Date.now() / 1000) + 999999 } } }) === null);
t("travel legs non-object record rejected", v({ id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": 12 } }) === null);
t("travel legs array rejected", v({ id: "traveltimes", type: "travel", pairs: {}, legs: [] }) === null);
t("chesttype cannot claim traveltimes", v({ id: "traveltimes", type: "chesttype", name: "x", color: "#112233", weight: 5, loot: [] }) === null);
t("chesttype cannot claim calibration", v({ id: "calibration", type: "chesttype", name: "x", color: "#112233", weight: 5, loot: [] }) === null);
t("marker cannot claim traveltimes", v({ id: "traveltimes", kind: "chest", x: 0.5, y: 0.5 }) === null);
t("route cannot claim calibration", v({ id: "calibration", type: "route", nodes: ["a"], name: "R" }) === null);


t("marker soft-delete ts kept", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, gx: 1, gy: 2, gz: 3, deleted: 1784000000000 }); return m && m.deleted === 1784000000000; })());
t("marker bad deleted dropped", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, deleted: "soon" }); return m && m.deleted === undefined; })());
t("marker deleted too large dropped", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, deleted: 5e12 }); return m && m.deleted === undefined; })());
t("mob keeps soft-delete", (() => { const m = v({ id: "m1", kind: "mob", x: 0.5, y: 0.5, deleted: 123 }); return m && m.deleted === 123; })());


t("runs valid", (() => { const r = v({ id: "runs", type: "runs", best: { "run1abc|blake": { ign: "Blake", t: 272, c: 20, at: 1784100000000 } }, recent: [{ r: "run1abc", ign: "Blake", t: 272, c: 20, at: 1784100000000 }] }); return r && r.best["run1abc|blake"].t === 272 && r.recent[0].r === "run1abc"; })());
t("runs wrong id rejected", v({ id: "runs2", type: "runs", best: {} }) === null);
t("runs bad key rejected", v({ id: "runs", type: "runs", best: { "run1abc": { ign: "B", t: 1, c: 0, at: 0 } } }) === null);
t("runs bad time rejected", v({ id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 0, c: 0, at: 0 } } }) === null);
t("runs missing ign rejected", v({ id: "runs", type: "runs", best: { "r|b": { ign: "", t: 5, c: 0, at: 0 } } }) === null);
t("runs recent bad route id rejected", v({ id: "runs", type: "runs", recent: [{ r: "bad id!", ign: "B", t: 5, c: 0, at: 0 }] }) === null);
t("runs recent too long rejected", (() => { const a = []; for (let i = 0; i < 301; i++) a.push({ r: "r", ign: "B", t: 5, c: 0, at: 0 }); return v({ id: "runs", type: "runs", recent: a }) === null; })());
t("runs too many best keys rejected", (() => { const o = {}; for (let i = 0; i < 4001; i++) o["r" + i + "|b"] = { ign: "B", t: 5, c: 0, at: 0 }; return v({ id: "runs", type: "runs", best: o }) === null; })());
t("runs rounds fractional time/chests", (() => { const r = v({ id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 5.7, c: 3.2, at: 1 } } }); return r.best["r|b"].t === 6 && r.best["r|b"].c === 3; })());
t("runs stats absent -> left undefined (editor-preserve signal)", (() => { const r = v({ id: "runs", type: "runs", best: {}, recent: [] }); return r && r.stats === undefined; })());
t("runs stats accepted + rounded when present", (() => { const r = v({ id: "runs", type: "runs", stats: { loop: { n: 3, sum: 900.4, min: 280.6, max: 340.2, at: 5 } } }); return r && r.stats.loop.n === 3 && r.stats.loop.sum === 900 && r.stats.loop.min === 281 && r.stats.loop.max === 340; })());
t("runs stats bad route id rejected", v({ id: "runs", type: "runs", stats: { "bad id!": { n: 1, sum: 1, min: 1, max: 1, at: 0 } } }) === null);
t("runs stats non-numeric rejected", v({ id: "runs", type: "runs", stats: { loop: { n: "x", sum: 1, min: 1, max: 1 } } }) === null);
t("chesttype cannot claim runs", v({ id: "runs", type: "chesttype", name: "x", color: "#112233", weight: 5, loot: [] }) === null);
t("marker cannot claim runs", v({ id: "runs", kind: "chest", x: 0.5, y: 0.5 }) === null);

t("double gx2 fields now ignored", (() => { const m = v({ id: "c1", kind: "chest", x: 0.5, y: 0.5, gx: 57, gy: 76, gz: -65, gx2: 58, gy2: 76, gz2: -65 }); return m && m.gx2 === undefined && m.gy2 === undefined; })());

// ---- pending + auth ----
t("pending valid", (() => { const p = v({ id: "pend-abc123", type: "pending", gx: 57, gy: 76, gz: -65, x: 0.4, y: 0.3, by: "Rev" }); return p && p.gx === 57 && p.by === "Rev" && p.type === "pending"; })());
t("pending id derived from coords (client id ignored)", (() => { const p = v({ id: "whatever", type: "pending", gx: 57, gy: 76, gz: -65, x: 0.4, y: 0.3 }); return p && p.id === "pend-57_76_-65"; })());
t("pending same spot -> same id (server dedup)", (() => { const a = v({ id: "a", type: "pending", gx: 57.2, gy: 76, gz: -65, x: 0.4, y: 0.3 }); const b = v({ id: "b", type: "pending", gx: 56.9, gy: 76, gz: -65, x: 0.9, y: 0.9 }); return a && b && a.id === b.id; })());
t("pending missing coords rejected", v({ id: "pend-x", type: "pending", x: 0.5, y: 0.5 }) === null);
t("pending missing map pos rejected", v({ id: "pend-x", type: "pending", gx: 1, gy: 2, gz: 3 }) === null);
t("pending large valid coords still derive a fitting id", (() => { const p = v({ id: "p", type: "pending", gx: 1234567, gy: 7654321, gz: -1234567, x: 0.5, y: 0.5 }); return p && p.id === "pend-1234567_7654321_-1234567" && /^pend-[\w-]{1,34}$/.test(p.id); })());
t("pending removal report: pend-rm id + kind kept", (() => { const p = v({ id: "x", type: "pending", kind: "remove", gx: 57, gy: 76, gz: -65, x: 0.4, y: 0.3, by: "Rev" }); return p && p.id === "pend-rm-57_76_-65" && p.kind === "remove"; })());
t("pending removal + proposal at same spot get distinct ids", (() => { const a = v({ id: "a", type: "pending", gx: 5, gy: 6, gz: 7, x: 0.5, y: 0.5 }); const b = v({ id: "b", type: "pending", kind: "remove", gx: 5, gy: 6, gz: 7, x: 0.5, y: 0.5 }); return a && b && a.id !== b.id; })());
t("pending junk kind ignored (stays a proposal)", (() => { const p = v({ id: "x", type: "pending", kind: "zap", gx: 1, gy: 2, gz: 3, x: 0.5, y: 0.5 }); return p && p.id === "pend-1_2_3" && p.kind === undefined; })());
t("pending zone flag: pend-zn id + kind + note", (() => { const p = v({ id: "x", type: "pending", kind: "zone", gx: 5, gy: 64, gz: 5, x: 0.5, y: 0.5, area: "Glacaris", note: "opened in Glacaris, but mapped inside Thornvale" }); return p && p.id === "pend-zn-5_64_5" && p.kind === "zone" && p.area === "Glacaris" && /Thornvale/.test(p.note); })());
t("pending all three kinds get distinct ids", (() => { const mk = k => v({ id: "x", type: "pending", kind: k, gx: 9, gy: 9, gz: 9, x: 0.5, y: 0.5 }); const ids = [mk(undefined), mk("remove"), mk("zone")].map(p => p.id); return new Set(ids).size === 3; })());

// ---- areas: editor-drawn polygons + HUD-observed totals ----
t("area valid + id derived from name", (() => { const a = v({ id: "x", type: "area", name: "The Hollow", points: [[0.1, 0.1], [0.5, 0.1], [0.3, 0.6]] }); return a && a.id === "area-the-hollow" && a.points.length === 3 && a.name === "The Hollow"; })());
t("area needs >=3 points", v({ id: "x", type: "area", name: "A", points: [[0, 0], [1, 1]] }) === null);
t("area clamps point fracs", (() => { const a = v({ id: "x", type: "area", name: "A", points: [[2, -1], [0.5, 0.1], [0.3, 0.6]] }); return a && a.points[0][0] === 1 && a.points[0][1] === 0; })());
t("area without a name rejected", v({ id: "x", type: "area", points: [[0, 0], [1, 0], [1, 1]] }) === null);
t("areatotals valid singleton", (() => { const at = v({ id: "areatotals", type: "areatotals", areas: { "the-hollow": { name: "The Hollow", total: 99 } } }); return at && at.areas["the-hollow"].total === 99; })());
t("areatotals junk rejected", v({ id: "areatotals", type: "areatotals", areas: { x: { total: 0 } } }) === null
  && v({ id: "nope", type: "areatotals", areas: {} }) === null);
t("marker area stamp kept + capped", (() => { const m = v({ id: "m1", type: "marker", kind: "chest", x: 0.5, y: 0.5, area: "The Hollow" }); return m && m.area === "The Hollow"; })());
t("pending area stamp kept", (() => { const p = v({ id: "x", type: "pending", gx: 1, gy: 2, gz: 3, x: 0.5, y: 0.5, area: "Thornvale" }); return p && p.area === "Thornvale"; })());
t("marker can't claim area- ids", v({ id: "area-foo", type: "marker", kind: "chest", x: 0.5, y: 0.5 }) === null);
t("route can't claim the areatotals id", v({ id: "areatotals", type: "route", nodes: [] }) === null);

// ---- contributor tally singleton ----
t("contrib valid round-trip", (() => { const c = v({ id: "contrib", type: "contrib", by: { blakebiz: { ign: "BlakeBiz", found: 297, removed: 2 } } }); return c && c.by.blakebiz.found === 297 && c.by.blakebiz.removed === 2 && c.by.blakebiz.ign === "BlakeBiz"; })());
t("contrib id must be the singleton", v({ id: "contrib-2", type: "contrib", by: {} }) === null);
t("contrib bad slug rejected", v({ id: "contrib", type: "contrib", by: { "no spaces!": { found: 1, removed: 0 } } }) === null);
t("contrib negative counts rejected", v({ id: "contrib", type: "contrib", by: { a: { found: -5, removed: 0 } } }) === null);
t("contrib absurd counts rejected", v({ id: "contrib", type: "contrib", by: { a: { found: 1e9, removed: 0 } } }) === null);
t("marker can't claim the contrib id", v({ id: "contrib", type: "marker", kind: "chest", x: 0.5, y: 0.5 }) === null);
t("marker foundBy kept + capped", (() => { const m = v({ id: "m1", type: "marker", kind: "chest", x: 0.5, y: 0.5, foundBy: "BlakeBiz" + "x".repeat(40) }); return m && m.foundBy.length === 20; })());
t("marker without foundBy stays clean", (() => { const m = v({ id: "m1", type: "marker", kind: "chest", x: 0.5, y: 0.5 }); return m && !("foundBy" in m); })());
t("marker cannot claim pend- id", v({ id: "pend-x", kind: "chest", x: 0.5, y: 0.5 }) === null);
t("chesttype cannot claim pend- id", v({ id: "pend-x", type: "chesttype", name: "n", color: "#112233", weight: 5, loot: [] }) === null);

// ---- new helpers: mergeEntry + clientIp + keylessSoft ----
const helpMod = { exports: {} };
new Function("module","exports","require","process",
  src + "\nmodule.exports._merge = mergeEntry; module.exports._clientIp = clientIp; module.exports._keylessSoft = keylessSoft; module.exports._MERGE = MERGE_TYPES; module.exports._SOFT = KEYLESS_SOFT; module.exports._MAX = MAX_ENTRIES; module.exports._ADD = KEYLESS_ADD;"
)(helpMod, helpMod.exports, require, { env: {} });
const merge = helpMod.exports._merge, clientIp = helpMod.exports._clientIp;
const KADD = helpMod.exports._ADD;

t("keylessSoft: opens/pending get lower cap, others not", helpMod.exports._keylessSoft("opens") && helpMod.exports._keylessSoft("pending") && !helpMod.exports._keylessSoft("runs") && !helpMod.exports._keylessSoft("marker"));
t("MERGE_TYPES set", helpMod.exports._MERGE.has("runs") && helpMod.exports._MERGE.has("travel") && helpMod.exports._MERGE.has("opens") && !helpMod.exports._MERGE.has("pending"));
t("caps: keyless soft < hard cap (editor headroom)", helpMod.exports._SOFT < helpMod.exports._MAX);

// merge: runs — empty submission cannot wipe existing best/recent
t("merge runs: empty POST does NOT wipe", (() => {
  const prev = { id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 100, c: 5, at: 1 } }, recent: [{ r: "r", ign: "B", t: 100, c: 5, at: 1 }] };
  const m = merge(prev, { id: "runs", type: "runs", best: {}, recent: [] });
  return m.best["r|b"].t === 100 && m.recent.length === 1;
})());
t("merge runs: keeps the FASTER time only", (() => {
  const prev = { id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 100, c: 5, at: 1 } }, recent: [] };
  const slower = merge(prev, { id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 200, c: 5, at: 2 } }, recent: [] });
  const faster = merge(prev, { id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 50, c: 5, at: 3 } }, recent: [] });
  return slower.best["r|b"].t === 100 && faster.best["r|b"].t === 50;
})());
t("merge runs: adds a new route record", (() => {
  const prev = { id: "runs", type: "runs", best: { "r1|b": { ign: "B", t: 100, c: 5, at: 1 } }, recent: [] };
  const m = merge(prev, { id: "runs", type: "runs", best: { "r2|c": { ign: "C", t: 80, c: 4, at: 2 } }, recent: [] });
  return m.best["r1|b"].t === 100 && m.best["r2|c"].t === 80;
})());
t("merge runs: recent keeps all prev + at most KEYLESS_ADD fresh", (() => {
  const pr = []; for (let i = 0; i < 200; i++) pr.push({ r: "r", ign: "B", t: i + 1, c: 1, at: i });
  const nx = []; for (let i = 0; i < 200; i++) nx.push({ r: "r", ign: "C", t: i + 1, c: 1, at: 1000 + i });
  const m = merge({ id: "runs", type: "runs", best: {}, recent: pr }, { id: "runs", type: "runs", best: {}, recent: nx });
  return m.recent.length === 200 + KADD && m.recent.filter(x => x.ign === "B").length === 200;
})());
// merge: travel — keep min, no wipe
t("merge travel: empty POST does NOT wipe, keeps min", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 40 } };
  const wipe = merge(prev, { id: "traveltimes", type: "travel", pairs: {} });
  const faster = merge(prev, { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 30 } });
  const slower = merge(prev, { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 99 } });
  return wipe.pairs["1,2,3|4,5,6"] === 40 && faster.pairs["1,2,3|4,5,6"] === 30 && slower.pairs["1,2,3|4,5,6"] === 40;
})());
// merge: directed legs — min-t / MAX-n / max-at: idempotent AND commutative, so retried or
// re-posted cumulative snapshots converge (a sum on n would double it every re-post)
t("merge legs: min t, max n, newest at", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 40, n: 3, at: 100 } } };
  const m = merge(prev, { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 5, at: 200 } } });
  const r = m.legs["1,2,3>4,5,6"];
  return r.t === 30 && r.n === 5 && r.at === 200;
})());
t("merge legs: a slower run keeps min t but still builds confidence + recency", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 1, at: 100 } } };
  const m = merge(prev, { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 99, n: 2, at: 200 } } });
  const r = m.legs["1,2,3>4,5,6"];
  return r.t === 30 && r.n === 2 && r.at === 200;
})());
t("merge legs: IDEMPOTENT — re-posting the same cumulative snapshot changes nothing", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 7, at: 500 } } };
  const post = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 7, at: 500 } } };
  const once = merge(prev, post), twice = merge(once, post), thrice = merge(twice, post);
  const r = thrice.legs["1,2,3>4,5,6"];
  return r.t === 30 && r.n === 7 && r.at === 500;  // a sum-merge would have made n 14, 21, 28...
})());
t("merge legs: n stays capped at LEG_CAP_N", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 249, at: 100 } } };
  const m = merge(prev, { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 250, at: 200 } } });
  return m.legs["1,2,3>4,5,6"].n === 250;
})());
t("merge legs: empty/old-client POST preserves stored directed legs", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 2, at: 100 } } };
  const oldClient = merge(prev, { id: "traveltimes", type: "travel", pairs: { "7,8,9|1,2,3": 5 } });  // no legs field
  return oldClient.legs["1,2,3>4,5,6"].t === 30 && oldClient.pairs["7,8,9|1,2,3"] === 5;
})());
t("merge legs: commutative (order of two writers doesn't matter)", (() => {
  const base = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 40, n: 1, at: 100 } } };
  const A = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 35, n: 2, at: 150 } } };
  const B = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30, n: 3, at: 200 } } };
  const ab = merge(merge(base, A), B).legs["1,2,3>4,5,6"];
  const ba = merge(merge(base, B), A).legs["1,2,3>4,5,6"];
  return ab.t === ba.t && ab.n === ba.n && ab.at === ba.at && ab.t === 30 && ab.n === 3 && ab.at === 200;
})());
t("merge legs: corrupt stored record (missing fields) can't NaN the merge", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 30 } } };  // no n/at
  const m = merge(prev, { id: "traveltimes", type: "travel", pairs: {}, legs: { "1,2,3>4,5,6": { t: 25, n: 2, at: 200 } } });
  const r = m.legs["1,2,3>4,5,6"];
  return r.t === 25 && r.n === 2 && r.at === 200;
})());
// merge: opens — cannot erase another player's opens, keeps newest per key
t("merge opens: cannot erase existing keys", (() => {
  const prev = { id: "opens-b", type: "opens", ign: "B", opens: { "1,2,3": { t: 500 }, "4,5,6": { t: 500 } } };
  const m = merge(prev, { id: "opens-b", type: "opens", ign: "B", opens: {} });
  return m.opens["1,2,3"].t === 500 && m.opens["4,5,6"].t === 500;
})());
t("merge opens: keeps the NEWEST open per key", (() => {
  const prev = { id: "opens-b", type: "opens", ign: "B", opens: { "1,2,3": { t: 500 } } };
  const older = merge(prev, { id: "opens-b", type: "opens", ign: "B", opens: { "1,2,3": { t: 100 } } });
  const newer = merge(prev, { id: "opens-b", type: "opens", ign: "B", opens: { "1,2,3": { t: 900 } } });
  return older.opens["1,2,3"].t === 500 && newer.opens["1,2,3"].t === 900;
})());
t("merge: null prev keeps a small first write intact (bounded)", (() => { const n = { id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 1, c: 1, at: 1 } }, recent: [] }; const m = merge(null, n); return m.type === "runs" && m.best["r|b"].t === 1 && Object.keys(m.best).length === 1; })());
t("merge: type-mismatched prev is treated as a bounded first write", (() => { const n = { id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 2, c: 1, at: 1 } }, recent: [] }; const m = merge({ id: "x", type: "travel", pairs: { "a|b": 5 } }, n); return m.type === "runs" && m.best["r|b"].t === 2 && m.pairs === undefined; })());

// --- eviction protection (recheck findings) ---
// opens: an attacker's 600 fresh keys must NOT evict the victim's stored keys
t("merge opens: fresh-key flood is bounded and cannot evict stored real opens", (() => {
  const prev = { id: "opens-v", type: "opens", ign: "V", opens: { "0,0,0": { t: 10 }, "0,0,1": { t: 10 } } };
  const forged = {}; for (let i = 0; i < 700; i++) forged["9," + i + ",9"] = { t: 999999 };
  const m = merge(prev, { id: "opens-v", type: "opens", ign: "V", opens: forged });
  return Object.keys(m.opens).length === 2 + KADD && m.opens["0,0,0"].t === 10 && m.opens["0,0,1"].t === 10;
})());
// defense-in-depth: even if accumulation pushes over the 600 cap, the trim keeps prev (real) keys
t("merge opens trim: over-cap trim preserves pre-existing keys, drops newest extras", (() => {
  const prevOpens = {}; for (let i = 0; i < 595; i++) prevOpens["p" + i + ",0,0"] = { t: 1 };
  prevOpens["REAL,0,0"] = { t: 1 }; // 596 stored keys incl a real one with the oldest t
  const forged = {}; for (let i = 0; i < 32; i++) forged["f" + i + ",0,0"] = { t: 999999 };
  const m = merge({ id: "opens-v", type: "opens", ign: "V", opens: prevOpens }, { id: "opens-v", type: "opens", ign: "V", opens: forged });
  return Object.keys(m.opens).length === 600 && m.opens["REAL,0,0"] !== undefined;
})());
t("merge opens: a keyless submission adds at most KEYLESS_ADD new keys", (() => {
  const forged = {}; for (let i = 0; i < 500; i++) forged["1," + i + ",1"] = { t: 5 };
  const m = merge({ id: "opens-v", type: "opens", ign: "V", opens: {} }, { id: "opens-v", type: "opens", ign: "V", opens: forged });
  return Object.keys(m.opens).length === KADD;
})());
// runs.recent: an attacker's 300 fake entries must NOT evict the real feed
t("merge runs: 300 fake recent cannot wipe the real feed", (() => {
  const prev = []; for (let i = 0; i < 300; i++) prev.push({ r: "real", ign: "R", t: i + 1, c: 1, at: i });
  const fake = []; for (let i = 0; i < 300; i++) fake.push({ r: "fake", ign: "X", t: i + 1, c: 1, at: 9e11 + i });
  const m = merge({ id: "runs", type: "runs", best: {}, recent: prev }, { id: "runs", type: "runs", best: {}, recent: fake });
  const realCount = m.recent.filter(x => x.r === "real").length;
  const fakeCount = m.recent.filter(x => x.r === "fake").length;
  return m.recent.length === 300 && fakeCount <= KADD && realCount >= 300 - KADD;
})());
// runs.best / travel.pairs: merged singleton must stay bounded even across many keyless adds
t("merge runs.best: bounded new records per submission", (() => {
  const nb = {}; for (let i = 0; i < 500; i++) nb["r" + i + "|x"] = { ign: "X", t: 5, c: 1, at: 1 };
  const m = merge({ id: "runs", type: "runs", best: {}, recent: [] }, { id: "runs", type: "runs", best: nb, recent: [] });
  return Object.keys(m.best).length === KADD;
})());
t("merge travel.pairs: bounded new pairs per submission", (() => {
  const np = {}; for (let i = 0; i < 500; i++) np[i + ",0,0|" + i + ",0,1"] = 5;
  const m = merge({ id: "traveltimes", type: "travel", pairs: {} }, { id: "traveltimes", type: "travel", pairs: np });
  return Object.keys(m.pairs).length === KADD;
})());
t("merge travel: existing pair still improves regardless of add-cap", (() => {
  const prev = { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 40 } };
  const m = merge(prev, { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 20 } });
  return m.pairs["1,2,3|4,5,6"] === 20;
})());
t("merge travel.legs: bounded new directed legs per submission", (() => {
  const nl = {}; for (let i = 0; i < 500; i++) nl[i + ",0,0>" + i + ",0,1"] = { t: 5, n: 1, at: 1 };
  const m = merge({ id: "traveltimes", type: "travel", pairs: {}, legs: {} }, { id: "traveltimes", type: "travel", pairs: {}, legs: nl });
  return Object.keys(m.legs).length === KADD;
})());
t("merge travel.legs: fake legs at cap cannot evict a real stored leg", (() => {
  const prevLegs = { "9,9,9>8,8,8": { t: 30, n: 5, at: 100 } };
  const nl = {}; for (let i = 0; i < 500; i++) nl[i + ",0,0>" + i + ",0,1"] = { t: 5, n: 1, at: 9e9 };
  const m = merge({ id: "traveltimes", type: "travel", pairs: {}, legs: prevLegs }, { id: "traveltimes", type: "travel", pairs: {}, legs: nl });
  return m.legs["9,9,9>8,8,8"].t === 30;  // stored leg survives regardless of the flood
})());
// runs.at clamp: forged far-future recency is clamped
t("runs: far-future at is clamped to ~now", (() => { const r = v({ id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 5, c: 1, at: 3.9e12 } }, recent: [] }); return r && r.best["r|b"].at <= Date.now() + 6 * 60 * 1000; })());

// ---- route ratings ----
t("rating valid, id derived from route", (() => { const r = v({ id: "rate-r1abc", type: "rating", route: "r1abc", by: { "blake": { stars: 4, comment: "solid loop", ign: "Blake", at: 1784100000000 } } }); return r && r.id === "rate-r1abc" && r.by["blake"].stars === 4 && r.by["blake"].comment === "solid loop"; })());
t("rating stars clamped to integer", (() => { const r = v({ id: "rate-r1", type: "rating", route: "r1", by: { "b": { stars: 4.7, comment: "", ign: "B", at: 1 } } }); return r.by["b"].stars === 5; })());
t("rating stars out of range rejected", v({ id: "rate-r1", type: "rating", route: "r1", by: { "b": { stars: 6, at: 1 } } }) === null);
t("rating stars zero rejected", v({ id: "rate-r1", type: "rating", route: "r1", by: { "b": { stars: 0, at: 1 } } }) === null);
t("rating missing route rejected", v({ id: "rate-x", type: "rating", by: { "b": { stars: 3, at: 1 } } }) === null);
t("rating bad slug key rejected", v({ id: "rate-r1", type: "rating", route: "r1", by: { "bad slug!": { stars: 3, at: 1 } } }) === null);
t("rating far-future at clamped", (() => { const r = v({ id: "rate-r1", type: "rating", route: "r1", by: { "b": { stars: 3, at: 3.9e12 } } }); return r.by["b"].at <= Date.now() + 6 * 60 * 1000; })());
t("marker cannot claim rate- id", v({ id: "rate-r1", kind: "chest", x: 0.5, y: 0.5 }) === null);
t("route cannot claim rate- id", v({ id: "rate-r1", type: "route", nodes: ["a"], name: "R" }) === null);

// rating merge: a rater can't erase others; updates own; bounded new raters
t("merge rating: keeps others, updates own by newest at", (() => {
  const prev = { id: "rate-r1", type: "rating", route: "r1", by: { "alice": { stars: 5, comment: "a", ign: "Alice", at: 10 }, "bob": { stars: 4, comment: "b", ign: "Bob", at: 10 } } };
  const older = merge(prev, { id: "rate-r1", type: "rating", route: "r1", by: { "alice": { stars: 1, comment: "z", ign: "Alice", at: 5 } } });
  const newer = merge(prev, { id: "rate-r1", type: "rating", route: "r1", by: { "alice": { stars: 2, comment: "new", ign: "Alice", at: 99 } } });
  return older.by["alice"].stars === 5 && older.by["bob"].stars === 4 && newer.by["alice"].stars === 2 && newer.by["bob"].stars === 4;
})());
t("merge rating: empty POST cannot wipe ratings", (() => {
  const prev = { id: "rate-r1", type: "rating", route: "r1", by: { "alice": { stars: 5, comment: "a", ign: "Alice", at: 10 } } };
  const m = merge(prev, { id: "rate-r1", type: "rating", route: "r1", by: {} });
  return Object.keys(m.by).length === 1 && m.by["alice"].stars === 5;
})());
t("merge rating: bounded new raters per submission", (() => {
  const nb = {}; for (let i = 0; i < 200; i++) nb["u" + i] = { stars: 1, comment: "spam", ign: "U", at: i + 1 };
  const m = merge(null, { id: "rate-r1", type: "rating", route: "r1", by: nb });
  return Object.keys(m.by).length === KADD;
})());

// --- FIRST-WRITE must be bounded too (a keyless caller can't seed a full fake dataset) ---
t("merge opens FIRST write (null prev) bounded to KEYLESS_ADD keys", (() => {
  const forged = {}; for (let i = 0; i < 600; i++) forged["9," + i + ",9"] = { t: 5 };
  const m = merge(null, { id: "opens-victim", type: "opens", ign: "V", opens: forged });
  return Object.keys(m.opens).length === KADD;
})());
t("merge runs.best FIRST write (null prev) bounded to KEYLESS_ADD", (() => {
  const nb = {}; for (let i = 0; i < 4000; i++) nb["r" + i + "|x"] = { ign: "X", t: 1, c: 1, at: 1 };
  const m = merge(null, { id: "runs", type: "runs", best: nb, recent: [] });
  return Object.keys(m.best).length === KADD;
})());
t("merge travel.pairs FIRST write (null prev) bounded to KEYLESS_ADD", (() => {
  const np = {}; for (let i = 0; i < 4000; i++) np[i + ",0,0|" + i + ",0,1"] = 1;
  const m = merge(null, { id: "traveltimes", type: "travel", pairs: np });
  return Object.keys(m.pairs).length === KADD;
})());
t("merge runs.recent FIRST write bounded to KEYLESS_ADD", (() => {
  const nx = []; for (let i = 0; i < 300; i++) nx.push({ r: "r", ign: "X", t: i + 1, c: 1, at: i });
  const m = merge(null, { id: "runs", type: "runs", best: {}, recent: nx });
  return m.recent.length === KADD;
})());
// --- once a collection is at cap, a keyless add can NEVER evict a stored (real) key ---
t("merge best at cap: keyless add evicts new keys, never stored ones", (() => {
  const prevBest = {}; for (let i = 0; i < 4000; i++) prevBest["real" + i + "|r"] = { ign: "R", t: 300, c: 1, at: 1 };
  const nb = {}; for (let i = 0; i < 32; i++) nb["fake" + i + "|x"] = { ign: "X", t: 1, c: 1, at: 1 };
  const m = merge({ id: "runs", type: "runs", best: prevBest, recent: [] }, { id: "runs", type: "runs", best: nb, recent: [] });
  const keys = Object.keys(m.best);
  return keys.length === 4000 && keys.every(k => k.startsWith("real")) && m.best["real0|r"].t === 300;
})());
t("merge travel at cap: fake t=1 pairs cannot evict real pairs", (() => {
  const prevPairs = {}; for (let i = 0; i < 4000; i++) prevPairs[i + ",0,0|" + i + ",0,1"] = 50;
  const np = {}; for (let i = 0; i < 32; i++) np["f" + i + ",9,9|f" + i + ",9,8"] = 1;
  const m = merge({ id: "traveltimes", type: "travel", pairs: prevPairs }, { id: "traveltimes", type: "travel", pairs: np });
  const keys = Object.keys(m.pairs);
  return keys.length === 4000 && keys.every(k => !k.startsWith("f"));
})());

// clientIp: never trust the leftmost x-forwarded-for
t("clientIp: prefers x-real-ip", clientIp({ headers: { "x-real-ip": "9.9.9.9", "x-forwarded-for": "1.1.1.1, 2.2.2.2" } }) === "9.9.9.9");
t("clientIp: falls back to RIGHTMOST xff hop, not leftmost", clientIp({ headers: { "x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3" } }) === "3.3.3.3");
t("clientIp: spoofed leftmost xff does NOT change bucket", (() => {
  const a = clientIp({ headers: { "x-forwarded-for": "6.6.6.6, 8.8.8.8" } });
  const b = clientIp({ headers: { "x-forwarded-for": "7.7.7.7, 8.8.8.8" } }); // attacker rotated leftmost
  return a === b && a === "8.8.8.8";
})());
t("clientIp: unknown when no headers", clientIp({ headers: {} }) === "unknown");

// auth: build a keyed module instance
const keyedMod = { exports: {} };
new Function("module","exports","require","process",
  src + "\nmodule.exports._authed = authed; module.exports._openTypes = OPEN_POST_TYPES; module.exports._writeKey = writeKey;"
)(keyedMod, keyedMod.exports, require, { env: { DUNGEON_WRITE_KEY: "s3cret" } });
const A = keyedMod.exports._authed, OT = keyedMod.exports._openTypes;
t("auth: open types set", OT.has("opens") && OT.has("runs") && OT.has("travel") && OT.has("pending") && !OT.has("marker") && !OT.has("route") && !OT.has("calibration"));
t("auth: correct x-write-key passes", A({ headers: { "x-write-key": "s3cret" } }) === true);
t("auth: bearer token passes", A({ headers: { authorization: "Bearer s3cret" } }) === true);
t("auth: wrong key fails", A({ headers: { "x-write-key": "nope" } }) === false);
t("auth: wrong-length key fails", A({ headers: { "x-write-key": "s3cre" } }) === false);
t("auth: no header fails", A({ headers: {} }) === false);

// master-key path is OFF (never an open map) when no DUNGEON_WRITE_KEY is set — identity model
// fails closed; a caller with no master key is simply "not the master", not "everyone".
const openMod = { exports: {} };
new Function("module","exports","require","process",
  src + "\nmodule.exports._authed = authed;"
)(openMod, openMod.exports, require, { env: {} });
t("auth: master path OFF (not open) when no key set", openMod.exports._authed({ headers: {} }) === false);
t("auth: even a presented key is rejected when no master key configured", openMod.exports._authed({ headers: { "x-write-key": "anything" } }) === false);

process.exit(fail ? 1 : 0);