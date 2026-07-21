// Shared dungeon-map database for the Dungeon Loot Map tool.
// Storage: the same Upstash Redis store as api/items.js, under a separate hash key.
// Entries are either markers (chest / group / mob) or routes. No npm dependencies.

const KEY = "histatu:dungeon";
const MAX_ENTRIES = 3000;   // hard ceiling on total stored entries
// Keyless, unbounded-id writes (opens-* logs, pend-* requests) are refused past this lower
// ceiling, so a flood of them always leaves >=(MAX_ENTRIES - KEYLESS_SOFT) slots for editor
// markers — a keyless spammer can never freeze the map.
const KEYLESS_SOFT = 2000;

function storeCfg() {
  const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  if (url && token) return { url, token };
  for (const key of Object.keys(process.env)) {
    for (const suffix of ["REST_API_URL", "REDIS_REST_URL"]) {
      if (!key.endsWith(suffix)) continue;
      const prefix = key.slice(0, key.length - suffix.length);
      const t = process.env[prefix + suffix.replace("URL", "TOKEN")];
      const u = process.env[key];
      if (u && t && /^https:\/\//.test(u)) return { url: u, token: t };
    }
  }
  return null;
}

async function redis(c, cmd) {
  const r = await fetch(c.url, {
    method: "POST",
    headers: { Authorization: "Bearer " + c.token, "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  });
  if (!r.ok) throw new Error("storage http " + r.status);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j.result;
}

const ID_RE = /^[\w-]{1,40}$/;
const SLUG_RE = /^[\w-]{1,30}$/;      // chest-type ids referenced by markers
const COLOR_RE = /^#[0-9a-fA-F]{6}$/;
// per-chest route-difficulty multipliers (Fast / Normal / Slow / Hard / Impossible). Applied to
// route ESTIMATES only (never measured times); 1 = Normal is the default.
const DIFF_SET = new Set([0.75, 1, 1.5, 2, 5]);

function str(v, max) { return typeof v === "string" ? v.trim().slice(0, max) : ""; }
function frac(v) { const n = Number(v); return isFinite(n) ? Math.max(0, Math.min(1, n)) : null; }
function coord(v) { if (v == null || v === "") return null; const n = Number(v); return isFinite(n) && Math.abs(n) < 1e9 ? n : null; }
function nonneg(v, cap) { const n = Number(v); return isFinite(n) && n >= 0 && n <= cap ? n : null; }

// Re-validated server-side so a tampered client can't store malformed entries for everyone.
// ids other entry types must never claim: prefixed namespaces + singleton entries
function reservedId(id) {
  return /^(mc|opens|pend|rate|area|player)-/.test(id) || id === "calibration" || id === "traveltimes"
    || id === "runs" || id === "contrib" || id === "areatotals" || id === "pkeys";
}

// ---- identity & write authorization -----------------------------------------
// Identity model: players sign in with their HYTALE ACCOUNT on the website (OAuth2 device flow —
// they authorize on accounts.hytale.com themselves; this server sees their profile exactly once
// and never stores any Hytale token). That binds their in-game name to their account UUID and
// mints a personal key ("hd_…"). Every write requires a key; what a key may write depends on the
// ROLE stored on its player binding:
//   owner   — the OWNER_IGN account: everything, plus grants/revokes editor and releases bindings
//   editor  — map structure (markers/areas/routes/types/calibration/bulk), verify, cleanup
//   player  — their OWN opens/runs/ratings/travel observations + pending submissions
// Keys are stored as SHA-256 hashes only, resettable any time by signing in again (the fresh
// sign-in rotates the key, so a stolen key dies the moment the real owner re-verifies).
// DUNGEON_WRITE_KEY remains honored as a break-glass master override (deliberately absent from
// every UI) so an auth outage can never lock the owner out.
const OWNER_IGN = (process.env.OWNER_IGN || "").trim();
const HYTALE_OAUTH_BASE = (process.env.HYTALE_OAUTH_BASE || "https://oauth.accounts.hytale.com").replace(/\/+$/, "");
const HYTALE_DATA_BASE = (process.env.HYTALE_ACCOUNT_DATA_BASE || "https://account-data.hytale.com").replace(/\/+$/, "");
// Hypixel's own pre-registered device-flow client. There is no third-party client registration
// program today; env-configurable so it can be swapped the day one exists.
const HYTALE_CLIENT_ID = (process.env.HYTALE_OAUTH_CLIENT_ID || "hytale-server").trim();
const HYTALE_SCOPE = (process.env.HYTALE_OAUTH_SCOPE || "openid offline auth:server").trim();
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PLAYER_KEY_RE = /^hd_[A-Za-z0-9_-]{20,80}$/;

function slugify(s) {
  const t = String(s || "").trim().toLowerCase().replace(/[^\w]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24);
  return t || "player";
}
function sha256hex(s) { return require("crypto").createHash("sha256").update(String(s)).digest("hex"); }
function newPlayerKey() { return "hd_" + require("crypto").randomBytes(24).toString("base64url"); }

function writeKey() {
  const k = (process.env.DUNGEON_WRITE_KEY || "").trim();
  return k || null;
}
function presentedKey(req) {
  const h = (req.headers && (req.headers["x-write-key"] || req.headers["authorization"])) || "";
  return String(h).replace(/^Bearer\s+/i, "").trim();
}
function presentedPlayerKey(req) {
  return String((req.headers && req.headers["x-player-key"]) || "").trim();
}
function authed(req) {
  const key = writeKey();
  if (!key) return false; // no master key configured -> master path simply off (identity still works)
  const given = presentedKey(req);
  if (!given) return false;
  // hash both sides before the constant-time compare: equal-length digests mean neither the
  // key's length nor its content leaks through timing (the source is public — assume attackers
  // read this)
  const crypto = require("crypto");
  const a = crypto.createHash("sha256").update(given).digest();
  const b = crypto.createHash("sha256").update(key).digest();
  return crypto.timingSafeEqual(a, b);
}
// Resolve who is calling: the master key acts as owner (break-glass); otherwise the x-player-key
// is looked up via the pkeys index (keyHash -> uuid) and its player binding. Returns
// {uuid?, ign?, slug?, role, master?} or null. The double-check against the binding's own
// keyHash makes a stale index entry (key rotated mid-flight) fail closed.
async function actor(c, req) {
  if (authed(req)) return { role: "owner", master: true, ign: OWNER_IGN || "owner", slug: slugify(OWNER_IGN || "owner") };
  const pk = presentedPlayerKey(req);
  if (!pk || !PLAYER_KEY_RE.test(pk)) return null;
  const h = sha256hex(pk);
  const idxRaw = await redis(c, ["HGET", KEY, "pkeys"]);
  if (!idxRaw) return null;
  let idx; try { idx = JSON.parse(idxRaw); } catch (e) { return null; }
  const uuid = idx && idx.map ? idx.map[h] : null;
  if (!uuid) return null;
  const pRaw = await redis(c, ["HGET", KEY, "player-" + uuid]);
  if (!pRaw) return null;
  let p; try { p = JSON.parse(pRaw); } catch (e) { return null; }
  if (p.keyHash !== h) return null;
  const role = p.role === "owner" || p.role === "editor" ? p.role : "player";
  return { uuid: p.uuid, ign: p.ign, slug: p.slug, role };
}
function isEditor(who) { return !!(who && (who.role === "owner" || who.role === "editor")); }

// ---- Hytale sign-in (OAuth2 device flow, RFC 8628 — run entirely server-side) ----------------
// The website asks us to start a sign-in; we hand back Hytale's user code + verification URL and
// keep only the device_code (in Redis, 10-minute TTL, keyed by an opaque handle). The site polls;
// we poll Hytale's token endpoint at most once per `interval`. On approval we call get-profiles
// ONCE to learn the account's game profile(s), then DISCARD every Hytale token immediately —
// nothing from Hytale's side is ever persisted here.
const AUTH_STATE_PREFIX = "histatu:auth:";
async function authStateGet(c, handle) {
  if (!/^[A-Za-z0-9_-]{10,40}$/.test(String(handle || ""))) return null;
  const raw = await redis(c, ["GET", AUTH_STATE_PREFIX + handle]);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (e) { return null; }
}
async function authStateSet(c, handle, st) {
  await redis(c, ["SET", AUTH_STATE_PREFIX + handle, JSON.stringify(st), "EX", "600"]);
}
async function authStateDel(c, handle) {
  try { await redis(c, ["DEL", AUTH_STATE_PREFIX + handle]); } catch (e) { /* TTL cleans up anyway */ }
}
async function hytaleForm(url, params) {
  const body = Object.keys(params).map((k) => encodeURIComponent(k) + "=" + encodeURIComponent(params[k])).join("&");
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "histatu-dungeon" }, body });
}

// Bind a verified game profile to a player record + fresh key. Fails (rather than steals) when the
// in-game name is already bound to a DIFFERENT account — a name changing hands is an owner
// decision (release the old binding first), never something a sign-in does silently. Re-verifying
// the SAME account rotates the key (that's the reset flow) and keeps any granted role.
async function bindPlayer(c, profile) {
  const slug = slugify(profile.username);
  const flat = (await redis(c, ["HGETALL", KEY])) || [];
  let existing = null;
  for (let i = 0; i < flat.length; i += 2) {
    if (!String(flat[i]).startsWith("player-")) continue;
    let p; try { p = JSON.parse(flat[i + 1]); } catch (e) { continue; }
    if (!p || p.type !== "player") continue;
    if (p.uuid === profile.uuid) existing = p;
    else if (p.slug === slug) return { error: "that in-game name is already linked to a different account — ask the map owner to release it" };
  }
  let role = existing && (existing.role === "editor" || existing.role === "owner") ? existing.role : "player";
  if (OWNER_IGN && slugify(OWNER_IGN) === slug) role = "owner"; // the owner account is owner, always
  const key = newPlayerKey();
  const keyHash = sha256hex(key);
  const player = { id: "player-" + profile.uuid, type: "player", uuid: profile.uuid,
    ign: profile.username, slug, role, keyHash,
    verifiedAt: existing ? existing.verifiedAt : Date.now(), updatedAt: Date.now() };
  // pkeys index: drop any hash previously pointing at this uuid (old key dies NOW), add the new one
  let idx = { map: {} };
  const idxRaw = await redis(c, ["HGET", KEY, "pkeys"]);
  if (idxRaw) { try { const j = JSON.parse(idxRaw); if (j && j.map) idx = j; } catch (e) { /* rebuild */ } }
  for (const h in idx.map) if (idx.map[h] === profile.uuid) delete idx.map[h];
  idx.map[keyHash] = profile.uuid;
  await redis(c, ["HSET", KEY, player.id, JSON.stringify(player), "pkeys", JSON.stringify(idx)]);
  return { player, key };
}
// entry types anyone may POST without the write key
const OPEN_POST_TYPES = new Set(["opens", "runs", "travel", "pending", "rating"]);
// unbounded-id keyless types get the lower KEYLESS_SOFT ceiling instead of MAX_ENTRIES
function keylessSoft(type) { return type === "opens" || type === "pending" || type === "rating"; }
// shared aggregates that a KEYLESS submitter may only add to — never wipe or shrink. Without
// this, one anonymous POST of an empty {runs}/{traveltimes}/{opens-*} replaces everyone's data.
// Editors (with the key) still overwrite wholesale, so cleanup / backup-restore keep working.
const MERGE_TYPES = new Set(["runs", "travel", "opens", "rating"]);
// how many NEW records one keyless submission may contribute, so a single anonymous POST can't
// dominate/evict a shared collection (the honest client adds ~1 at a time).
const KEYLESS_ADD = 32;
const BEST_MAX = 4000, PAIRS_MAX = 4000, RECENT_MAX = 300, OPENS_MAX = 600, RATERS_MAX = 1000, LEGS_MAX = 4000;
const LEG_CAP_N = 250; // cap a directed leg's stored sample count so confidence stays a small int

// Bound a {key:{...}} map to `cap` keys WITHOUT ever evicting a pre-existing (already-stored)
// key — so a keyless caller can never push a stored record out. `prevKeys` is the set that was
// already stored; over-cap trimming only drops keys this request newly introduced.
function capKeepPrev(map, prevKeys, cap) {
  const keys = Object.keys(map);
  if (keys.length <= cap) return map;
  const out = {};
  let n = 0;
  for (const k of keys) if (prevKeys[k]) { out[k] = map[k]; n++; }          // keep every stored key
  for (const k of keys) if (!prevKeys[k] && n < cap) { out[k] = map[k]; n++; } // fill headroom
  return out;
}

// Merge a keyless submission into the stored value so it can only improve/extend the data —
// never shrink, evict, or overwhelm what's already stored. Rules for EVERY path (including the
// FIRST write, where prev is null — that must be bounded too, or a keyless caller could seed a
// full fake dataset before the real owner ever writes):
//   * an existing key can only be IMPROVED (min run/travel time, newest open) — never removed;
//   * at most KEYLESS_ADD brand-new keys may be introduced per request;
//   * over the hard cap, only THIS request's new keys are dropped, never pre-existing ones.
// Editors bypass this entirely and overwrite wholesale (see the POST handler).
function mergeEntry(prev, next) {
  const p = (prev && prev.type === next.type) ? prev : null;
  if (next.type === "runs") {
    const best = Object.assign({}, (p && p.best) || {});
    const prevKeys = {}; for (const k in best) prevKeys[k] = 1;
    const nb = next.best || {};
    let added = 0;
    for (const k in nb) {
      if (best[k]) { if (nb[k].t < best[k].t) best[k] = nb[k]; }        // improve an existing record
      else if (added < KEYLESS_ADD) { best[k] = nb[k]; added++; }        // bounded new records
    }
    // recent feed: keep ALL prior entries; prepend only a few genuinely-new ones, then cap.
    const seen = {};
    for (const it of (p && p.recent) || []) seen[it.r + "|" + it.ign + "|" + it.t + "|" + it.at] = 1;
    const fresh = [];
    for (const it of (next.recent || [])) {
      const rk = it.r + "|" + it.ign + "|" + it.t + "|" + it.at;
      if (seen[rk]) continue; seen[rk] = 1; fresh.push(it);
      if (fresh.length >= KEYLESS_ADD) break;
    }
    const recent = fresh.concat((p && p.recent) || []).slice(0, RECENT_MAX);
    // Per-route LIFETIME stats: every FRESH completion (record or not, ties included) bumps that
    // route's run count + time sum + min/max, so nothing is lost when the recent feed rolls over —
    // the site estimates a realistic route time from these. Tied to the fresh-dedup above, so it's
    // idempotent: a retried or re-posted run is already `seen` and never double-counted. (A wholesale
    // editor overwrite that omits stats has them folded back in by the POST handler, like legs.)
    const stats = Object.assign({}, (p && p.stats) || {});
    const statKeys = {}; for (const k in stats) statKeys[k] = 1;
    for (const it of fresh) {
      const s = stats[it.r] || { n: 0, sum: 0, min: it.t, max: it.t, at: 0 };
      stats[it.r] = { n: s.n + 1, sum: s.sum + it.t, min: Math.min(s.min, it.t),
                      max: Math.max(s.max, it.t), at: Math.max(s.at || 0, it.at || 0) };
    }
    return { id: next.id, type: "runs", best: capKeepPrev(best, prevKeys, BEST_MAX), recent,
             stats: capKeepPrev(stats, statKeys, BEST_MAX), updatedAt: next.updatedAt };
  }
  if (next.type === "travel") {
    const pairs = Object.assign({}, (p && p.pairs) || {});
    const prevKeys = {}; for (const k in pairs) prevKeys[k] = 1;
    const np = next.pairs || {};
    let added = 0;
    for (const k in np) {
      if (pairs[k] != null) { if (np[k] < pairs[k]) pairs[k] = np[k]; }   // improve an existing pair
      else if (added < KEYLESS_ADD) { pairs[k] = np[k]; added++; }        // bounded new pairs
    }
    // DIRECTED legs {"a>b": {t, n, at}} merge with min t / MAX n / max at. All three ops are
    // IDEMPOTENT as well as commutative — clients send their cumulative view (stored n + this
    // cycle's new samples), so a retried, reordered, or re-posted snapshot converges instead of
    // re-adding counts (a sum here would double n every flush and saturate the confidence gate).
    // Two truly concurrent writers may under-count n by one observation — max, not sum — which
    // errs on the SAFE side of the trust gate. A keyless caller adds at most KEYLESS_ADD new
    // directions; existing ones only improve. cur fields are coerced defensively (stored data
    // could predate this schema or be hand-edited); v is already sanitized by validEntry.
    const legs = Object.assign({}, (p && p.legs) || {});
    const prevLegKeys = {}; for (const k in legs) prevLegKeys[k] = 1;
    const nl = next.legs || {};
    let addedL = 0;
    for (const k in nl) {
      const v = nl[k], cur = legs[k];
      if (cur) legs[k] = { t: Math.min(Number(cur.t) || v.t, v.t),
                           n: Math.min(LEG_CAP_N, Math.max(Number(cur.n) || 1, v.n)),
                           at: Math.max(Number(cur.at) || 0, v.at) };
      else if (addedL < KEYLESS_ADD) { legs[k] = v; addedL++; }
    }
    return { id: next.id, type: "travel",
             pairs: capKeepPrev(pairs, prevKeys, PAIRS_MAX),
             legs: capKeepPrev(legs, prevLegKeys, LEGS_MAX),
             updatedAt: next.updatedAt };
  }
  if (next.type === "opens") {
    const opens = Object.assign({}, (p && p.opens) || {});
    const prevKeys = {}; for (const k in opens) prevKeys[k] = 1;
    const no = next.opens || {};
    let added = 0;
    for (const k in no) {
      if (opens[k]) { if (no[k].t > opens[k].t) opens[k] = no[k]; }       // refresh an existing open
      else if (added < KEYLESS_ADD) { opens[k] = no[k]; added++; }        // bounded new opens
    }
    return { id: next.id, type: "opens", ign: next.ign, opens: capKeepPrev(opens, prevKeys, OPENS_MAX), updatedAt: next.updatedAt };
  }
  if (next.type === "rating") {
    // per-route ratings keyed by rater ign-slug: a rater may (re)set their OWN rating (newest
    // wins), can never erase anyone else's, and one request adds at most KEYLESS_ADD new raters.
    const by = Object.assign({}, (p && p.by) || {});
    const prevKeys = {}; for (const k in by) prevKeys[k] = 1;
    const nb = next.by || {};
    let added = 0;
    for (const k in nb) {
      if (by[k]) { if (nb[k].at > by[k].at) by[k] = nb[k]; }              // rater updates their own
      else if (added < KEYLESS_ADD) { by[k] = nb[k]; added++; }           // bounded new raters
    }
    return { id: next.id, type: "rating", route: next.route,
      by: capKeepPrev(by, prevKeys, RATERS_MAX), updatedAt: next.updatedAt };
  }
  return next;
}

// The rate limiter must key on an identifier the caller can't forge. On Vercel the client can
// prepend its own X-Forwarded-For value (the real edge IP is appended to the RIGHT), so the
// leftmost token is attacker-controlled. Prefer x-real-ip (set by the platform); fall back to
// the rightmost forwarded hop, never the leftmost.
function clientIp(req) {
  const h = req.headers || {};
  const real = String(h["x-real-ip"] || "").trim();
  if (real) return real;
  const xff = String(h["x-forwarded-for"] || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  return xff.length ? xff[xff.length - 1] : "unknown";
}

function validEntry(b) {
  if (!b || typeof b !== "object") return null;
  const id = typeof b.id === "string" && ID_RE.test(b.id) ? b.id : null;
  if (!id) return null;

  if (b.type === "pending") {
    // a crowd-sourced request awaiting an editor's attention. Three kinds:
    //   (default)        "this spot has an unmapped chest"  -> editor confirms into a marker
    //   kind: "remove"   "this mapped chest doesn't exist"  -> editor verifies, then deletes it
    //   kind: "zone"     "this chest's in-game area disagrees with the map's boundary"
    //                    -> editor adjusts the area polygon, then resolves the flag
    // Anyone may submit any of them; nothing changes on the map until an editor acts.
    const gx = coord(b.gx), gy = coord(b.gy), gz = coord(b.gz);
    const x = frac(b.x), y = frac(b.y);
    if (gx === null || gy === null || gz === null || x === null || y === null) return null;
    const kind = b.kind === "remove" ? "remove" : b.kind === "zone" ? "zone" : null;
    // The id is DERIVED from the block coordinate, not taken from the client — so repeated
    // reports of the same spot collapse into one entry (honest de-dup + spam limiter).
    const prefix = kind === "zone" ? "pend-zn-" : kind === "remove" ? "pend-rm-" : "pend-";
    const pid = prefix + Math.round(gx) + "_" + Math.round(gy) + "_" + Math.round(gz);
    if (!/^pend-(rm-|zn-)?[\w-]{1,34}$/.test(pid)) return null; // guard absurd coordinates
    const out = { id: pid, type: "pending", gx, gy, gz, x, y,
      by: str(b.by, 20), note: str(b.note, 200), at: Date.now() };
    if (kind) out.kind = kind;
    if (b.area != null && b.area !== "") out.area = str(b.area, 24);
    return out;
  }

  if (b.type === "rating") {
    // per-route ratings/comments, keyed by rater ign-slug. Anyone may rate; a rater can only
    // (re)set their own entry. id is derived from the route id so one route == one entry.
    const route = typeof b.route === "string" && SLUG_RE.test(b.route) ? b.route : null;
    if (!route) return null;
    const rid = "rate-" + route;
    if (!/^rate-[\w-]{1,35}$/.test(rid)) return null;
    if (!b.by || typeof b.by !== "object" || Array.isArray(b.by)) return null;
    const keys = Object.keys(b.by);
    if (keys.length > 500) return null;                 // per-submission size guard
    const atCap = Date.now() + 5 * 60 * 1000;
    const out = { id: rid, type: "rating", route, by: {}, updatedAt: Date.now() };
    for (const k of keys) {
      if (!SLUG_RE.test(k)) return null;                // rater ign slug
      const v = b.by[k];
      if (!v || typeof v !== "object") return null;
      const stars = Number(v.stars);
      if (!isFinite(stars) || stars < 1 || stars > 5) return null;
      const at = Number(v.at);
      if (!isFinite(at) || at < 0 || at > 4e12) return null;
      out.by[k] = { stars: Math.round(stars), comment: str(v.comment, 300),
        ign: str(v.ign, 20), at: Math.min(at, atCap) };
    }
    return out;
  }

  if (b.type === "route") {
    if (reservedId(id)) return null;
    if (!Array.isArray(b.nodes) || b.nodes.length > 300) return null;
    const nodes = [];
    for (const nid of b.nodes) { if (typeof nid !== "string" || !ID_RE.test(nid)) return null; nodes.push(nid); }
    const out = { id, type: "route", name: str(b.name, 60) || "Untitled route", nodes: nodes,
      author: str(b.author, 40), note: str(b.note, 200), updatedAt: Date.now() };
    // invalid times degrade to null rather than rejecting the whole route
    out.totalTime = b.totalTime == null || b.totalTime === "" ? null : nonneg(b.totalTime, 1e7);
    if (b.legTimes != null) {
      if (!Array.isArray(b.legTimes) || b.legTimes.length > 300) return null;
      // an N-stop route has at most N-1 legs — never store phantom legs that would inflate totals
      out.legTimes = b.legTimes.slice(0, Math.max(0, nodes.length - 1))
        .map(function (t) { return (t == null || t === "") ? null : nonneg(t, 1e7); });
    } else out.legTimes = [];
    return out;
  }

  // NOTE: chest rarity / drop tables ("chesttype") were removed — loot odds are unknowable and
  // change too often to log; chests-OPENED is the metric everywhere. A chesttype POST is now
  // simply rejected (falls through to null). Mob categories (mobcat) are unrelated and stay.

  if (b.type === "opens") {
    // per-player chest-open log: coordinate key -> {t: epoch ms, r?: route id}.
    // Drives the per-player chest cooldown (companion app + site): a chest opened since the
    // daily 8 PM Eastern reset is locked until the next reset — never a per-chest timer.
    if (!/^opens-[\w-]{1,24}$/.test(id)) return null;
    const ign = str(b.ign, 20);
    if (!ign) return null;
    if (!b.opens || typeof b.opens !== "object" || Array.isArray(b.opens)) return null;
    const keys = Object.keys(b.opens);
    if (keys.length > 600) return null;
    const out = { id, type: "opens", ign, opens: {}, updatedAt: Date.now() };
    const tCap = Date.now() + 5 * 60 * 1000; // clamp clock skew so it can't inflate cooldowns
    for (const k of keys) {
      if (!/^-?\d{1,7},-?\d{1,7},-?\d{1,7}$/.test(k)) return null;
      const v = b.opens[k];
      if (!v || typeof v !== "object") return null;
      const t = Number(v.t);
      if (!isFinite(t) || t < 0 || t > 4e12) return null;
      const o = { t: Math.min(t, tCap) };
      if (typeof v.r === "string" && v.r) {
        if (!ID_RE.test(v.r)) return null;
        o.r = v.r;
      }
      out.opens[k] = o;
    }
    return out;
  }

  if (b.type === "runs") {
    // singleton run history: best time per route+player (leaderboards) + a recent feed.
    if (id !== "runs") return null;
    const KEY_RE = /^[\w-]{1,40}\|[\w-]{1,24}$/; // routeId|ignSlug
    const out = { id, type: "runs", best: {}, recent: [], updatedAt: Date.now() };
    const atCap = Date.now() + 5 * 60 * 1000; // clamp clock skew / forged future recency
    const validRun = function (v) {
      if (!v || typeof v !== "object") return null;
      const ign = str(v.ign, 20); if (!ign) return null;
      const t = Number(v.t); if (!isFinite(t) || t < 1 || t > 1e7) return null;
      const c = nonneg(v.c, 100000); if (c === null) return null;
      const at = Number(v.at); if (!isFinite(at) || at < 0 || at > 4e12) return null;
      return { ign, t: Math.round(t), c: Math.round(c), at: Math.min(at, atCap) };
    };
    if (b.best != null) {
      if (typeof b.best !== "object" || Array.isArray(b.best)) return null;
      const keys = Object.keys(b.best);
      if (keys.length > 4000) return null;
      for (const k of keys) {
        if (!KEY_RE.test(k)) return null;
        const r = validRun(b.best[k]); if (!r) return null;
        out.best[k] = r;
      }
    }
    if (b.recent != null) {
      if (!Array.isArray(b.recent) || b.recent.length > 300) return null;
      for (const item of b.recent) {
        if (!item || typeof item !== "object") return null;
        if (typeof item.r !== "string" || !ID_RE.test(item.r)) return null;
        const r = validRun(item); if (!r) return null;
        out.recent.push({ r: item.r, ign: r.ign, t: r.t, c: r.c, at: r.at });
      }
    }
    // per-route lifetime stats {routeId: {n, sum, min, max, at}}. Server-maintained (see mergeEntry);
    // accepted here only so a backup/restore round-trips it. LEFT UNDEFINED when absent, which the POST
    // handler reads as "editor didn't send stats — preserve the stored aggregate" (mirrors travel legs).
    if (b.stats != null) {
      if (typeof b.stats !== "object" || Array.isArray(b.stats)) return null;
      const skeys = Object.keys(b.stats);
      if (skeys.length > 4000) return null;
      out.stats = {};
      for (const k of skeys) {
        if (!ID_RE.test(k)) return null;                       // routeId
        const v = b.stats[k];
        if (!v || typeof v !== "object") return null;
        const n = nonneg(v.n, 1e9), sum = nonneg(v.sum, 1e12);
        const mn = Number(v.min), mx = Number(v.max), at = Number(v.at);
        if (n === null || sum === null || !isFinite(mn) || !isFinite(mx)) return null;
        out.stats[k] = { n: Math.round(n), sum: Math.round(sum), min: Math.round(mn), max: Math.round(mx),
                         at: (isFinite(at) && at >= 0 && at <= 4e12) ? Math.round(at) : 0 };
      }
    }
    return out;
  }

  if (b.type === "area") {
    // a named region polygon drawn by an editor on the map (normalized fracs). Markers are
    // assigned to areas by point-in-polygon on the site — nothing is stamped retroactively.
    const name = str(b.name, 24);
    if (!name) return null;
    const slug = name.toLowerCase().replace(/[^\w]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24);
    if (!SLUG_RE.test(slug)) return null;
    const aid = "area-" + slug;   // derived: one polygon per area name
    if (!Array.isArray(b.points) || b.points.length < 3 || b.points.length > 64) return null;
    const pts = [];
    for (const p of b.points) {
      if (!Array.isArray(p) || p.length !== 2) return null;
      const x = frac(p[0]), y = frac(p[1]);
      if (x === null || y === null) return null;
      pts.push([x, y]);
    }
    return { id: aid, type: "area", name,
      color: COLOR_RE.test(b.color) ? b.color : "#7c6df2",
      points: pts, updatedAt: Date.now() };
  }

  if (b.type === "areatotals") {
    // singleton: the game HUD's true chest count per area ("Thornvale 0/61" -> 61), observed
    // by the runner. Editor-gated like contrib. Powers the "N undiscovered" rollups.
    if (id !== "areatotals") return null;
    if (!b.areas || typeof b.areas !== "object" || Array.isArray(b.areas)) return null;
    const keys = Object.keys(b.areas);
    if (keys.length > 40) return null;
    const out = { id, type: "areatotals", areas: {}, updatedAt: Date.now() };
    for (const k of keys) {
      if (!SLUG_RE.test(k)) return null;
      const v = b.areas[k];
      if (!v || typeof v !== "object") return null;
      const total = nonneg(v.total, 9999);
      if (total === null || total < 1) return null;
      out.areas[k] = { name: str(v.name, 24), total: Math.round(total) };
    }
    return out;
  }

  if (b.type === "contrib") {
    // singleton contributor tallies: ign-slug -> {ign, found, removed}. found = chests they
    // located that were successfully added (directly as an editor, or via a confirmed pending);
    // removed = their missing-chest reports an editor confirmed. Writes are EDITOR-GATED (not in
    // OPEN_POST_TYPES): crediting happens at confirmation time, which is always an editor action,
    // so a keyless client can never inflate its own numbers.
    if (id !== "contrib") return null;
    if (!b.by || typeof b.by !== "object" || Array.isArray(b.by)) return null;
    const keys = Object.keys(b.by);
    if (keys.length > 500) return null;
    const out = { id, type: "contrib", by: {}, updatedAt: Date.now() };
    for (const k of keys) {
      if (!SLUG_RE.test(k)) return null;
      const v = b.by[k];
      if (!v || typeof v !== "object") return null;
      const found = nonneg(v.found, 1e6), removed = nonneg(v.removed, 1e6);
      if (found === null || removed === null) return null;
      out.by[k] = { ign: str(v.ign, 20), found: Math.round(found), removed: Math.round(removed) };
    }
    return out;
  }

  if (b.type === "travel") {
    // singleton: minimum player-run travel time (seconds) between chest pairs,
    // keyed "x1,y1,z1|x2,y2,z2" (coordinates sorted lexically). These override
    // the distance/speed estimates in the auto-route generator.
    if (id !== "traveltimes") return null;
    if (!b.pairs || typeof b.pairs !== "object" || Array.isArray(b.pairs)) return null;
    const keys = Object.keys(b.pairs);
    if (keys.length > 4000) return null;
    const C = "-?\\d{1,7},-?\\d{1,7},-?\\d{1,7}";
    const PAIR_RE = new RegExp("^" + C + "\\|" + C + "$");
    const out = { id, type: "travel", pairs: {}, updatedAt: Date.now() };
    for (const k of keys) {
      if (!PAIR_RE.test(k)) return null;
      const t = Number(b.pairs[k]);
      if (!isFinite(t) || t < 1 || t > 3600) return null;
      out.pairs[k] = Math.round(t);
    }
    // optional DIRECTED leg records {"a>b": {t, n, at}} — min directed secs, sample count, epoch.
    // When the field is ABSENT (a legs-unaware older client) out.legs stays undefined — the POST
    // handler uses that as the signal to preserve whatever legs are stored, so an old editor's
    // wholesale overwrite can't wipe data it doesn't know exists. (The keyless merge path
    // preserves stored legs either way.)
    if (b.legs != null) {
      out.legs = {};
      if (typeof b.legs !== "object" || Array.isArray(b.legs)) return null;
      const lkeys = Object.keys(b.legs);
      if (lkeys.length > LEGS_MAX) return null;
      const LEG_RE = new RegExp("^" + C + ">" + C + "$");
      const nowS = Math.floor(Date.now() / 1000);
      for (const k of lkeys) {
        if (!LEG_RE.test(k)) return null;
        const v = b.legs[k];
        if (!v || typeof v !== "object" || Array.isArray(v)) return null;
        const t = Number(v.t), n = Number(v.n), at = Number(v.at);
        if (!isFinite(t) || t < 1 || t > 3600) return null;
        if (!isFinite(n) || n < 1 || n > LEG_CAP_N) return null;
        // reject far-future stamps (allow a day of clock skew) so a hostile client can't pin a leg
        // permanently "fresh" and immune to recency decay.
        if (!isFinite(at) || at < 0 || at > nowS + 86400) return null;
        out.legs[k] = { t: Math.round(t), n: Math.round(n), at: Math.round(at) };
      }
    }
    return out;
  }

  if (b.type === "calibration") {
    // singleton world->map transform: mapU = ax*worldX + bx, mapV = az*worldZ + bz
    if (id !== "calibration") return null;
    const out = { id, type: "calibration", updatedAt: Date.now() };
    for (const k of ["ax", "bx", "az", "bz"]) {
      const n = Number(b[k]);
      if (!isFinite(n) || Math.abs(n) > 1e9) return null;
      out[k] = n;
    }
    out.setBy = str(b.setBy, 40);
    return out;
  }

  if (b.type === "mobcat") {
    // a mob category: mobcoins paid per kill and an optional daily payout limit
    if (!/^mc-/.test(id)) return null; // mobcats live in the mc- namespace so they can't clobber other entry types
    const mc = Number(b.mobcoin);
    const out = { id, type: "mobcat",
      name: str(b.name, 30) || "Category",
      mobcoin: isFinite(mc) && mc >= 0 && mc <= 1e12 ? mc : 0,
      updatedAt: Date.now() };
    if (b.dailyLimit == null || b.dailyLimit === "") out.dailyLimit = null;
    else {
      const dl = Number(b.dailyLimit);
      if (!isFinite(dl) || dl < 0 || dl > 1e15) return null;
      out.dailyLimit = dl;
    }
    return out;
  }

  // marker
  if (reservedId(id)) return null;
  const kind = b.kind === "group" || b.kind === "mob" || b.kind === "teleport" ? b.kind : "chest";
  const x = frac(b.x), y = frac(b.y);
  if (x === null || y === null) return null;
  const out = { id, type: "marker", kind, x, y,
    gx: coord(b.gx), gy: coord(b.gy), gz: coord(b.gz),
    name: str(b.name, 60), note: str(b.note, 200), updatedAt: Date.now() };
  // soft-delete timestamp — hidden but restorable from the recycle bin for ~7 days
  if (b.deleted != null && b.deleted !== "") {
    const d = Number(b.deleted);
    if (isFinite(d) && d >= 0 && d <= 4e12) out.deleted = d;
  }
  if (kind === "chest") {
    // (chest rarity removed — drop odds are unknowable; a chest is just a chest.)
    // route-difficulty multiplier (see DIFF_SET): scales ROUTE ESTIMATES for reaching this chest,
    // never measured leg times. Defaults to 1 (Normal). The tool re-sends the current value when a
    // chest is rediscovered, so an editor-set difficulty sticks. (This is PHYSICAL reach difficulty,
    // not loot rarity — kept.)
    out.diff = DIFF_SET.has(Number(b.diff)) ? Number(b.diff) : 1;
    // provenance: the IGN that located this chest (direct editor log, or a confirmed pending's
    // submitter) — powers the contributor leaderboard's per-marker attribution
    if (b.foundBy != null && b.foundBy !== "") out.foundBy = str(b.foundBy, 20);
    // best-effort area name observed in the game HUD at log time. Advisory only — the map
    // assigns markers to areas by the editor-drawn polygon bounds, not this stamp.
    if (b.area != null && b.area !== "") out.area = str(b.area, 24);
  } else if (kind === "group") {
    const cnt = Number(b.count);
    if (!isFinite(cnt) || cnt < 1 || cnt > 999) return null;
    out.count = Math.round(cnt);
  } else if (kind === "mob") {
    out.xp = nonneg(b.xp, 1e12); if (out.xp === null) out.xp = 0;
    out.spawnAmount = nonneg(b.spawnAmount, 1e6); if (out.spawnAmount === null) out.spawnAmount = 0;
    out.difficulty = str(b.difficulty, 30); // legacy free-text; category is the structured field
    if (typeof b.category === "string" && SLUG_RE.test(b.category)) out.category = b.category;
  }
  // teleport: a navigation waypoint — just the base x/y/gx/gy/gz/name/note fields, no extra data
  return out;
}

async function dailyBackup(c) {
  try {
    const fresh = await redis(c, ["SET", KEY + ":stamp", "1", "EX", "86400", "NX"]);
    if (fresh) await redis(c, ["COPY", KEY, KEY + ":backup", "REPLACE"]);
  } catch (e) { /* best effort */ }
}

// Insert-or-update KEYS[1][ARGV[1]] = ARGV[2] with two ceilings, both enforced only for a NEW id:
//   ARGV[3] = hard total cap (all entries);
//   ARGV[4] = "1" when this is a keyless unbounded-id write (opens-*/pend-*), in which case
//   ARGV[5] caps the count of KEYLESS fields ONLY — so keyless spam is bounded independently of
//   how much editor content exists (editor growth never starves new players' opens/pending).
const UPSERT_LUA =
  "if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0 then " +
  "  if redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[3]) then return -1 end " +
  "  if ARGV[4] == '1' then " +
  "    local n = 0 " +
  "    for _, f in ipairs(redis.call('HKEYS', KEYS[1])) do " +
  "      if string.sub(f,1,6) == 'opens-' or string.sub(f,1,5) == 'pend-' or string.sub(f,1,5) == 'rate-' then n = n + 1 end " +
  "    end " +
  "    if n >= tonumber(ARGV[5]) then return -1 end " +
  "  end " +
  "end " +
  "return redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])";

// Bulk insert-or-update: ARGV[1] = hard total cap, then (id, json) pairs. The NEW-id count, the
// ceiling check, and every HSET happen in ONE script, so concurrent bulk writes can't both read a
// stale HLEN and overshoot MAX_ENTRIES (the single-POST path gets the same atomicity from
// UPSERT_LUA). All-or-nothing: over the cap, nothing is written.
// NOTE: this string is ONE line of Lua — it must never contain "--" (a Lua line comment would
// comment out the entire script; Redis then runs nothing, returns nil, and the caller would
// report success without storing anything). A test asserts this.
const BULK_LUA =
  "local fresh = 0 " +
  "for i = 2, #ARGV, 2 do " +
  "  if redis.call('HEXISTS', KEYS[1], ARGV[i]) == 0 then fresh = fresh + 1 end " +
  "end " +
  "if fresh > 0 and redis.call('HLEN', KEYS[1]) + fresh > tonumber(ARGV[1]) then return -1 end " +
  "for i = 2, #ARGV, 2 do " +
  "  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) " +
  "end " +
  "return (#ARGV - 1) / 2";

module.exports = async (req, res) => {
  const c = storeCfg();
  if (!c) return res.status(503).json({ error: "shared storage not configured" });
  try {
    // body is parsed up-front so the sign-in flow can be recognised BEFORE the write limiter:
    // device-flow polling (every ~5s for up to 10 min) must not eat the 30/min write budget —
    // it has its own tighter start-limiter and interval throttling instead.
    let body = null;
    if (req.method === "POST" || req.method === "PUT") {
      try { body = req.body; } catch (e) { return res.status(400).json({ error: "invalid JSON" }); }
    }
    const authFlow = !!(body && (body.type === "authstart" || body.type === "authpoll" || body.type === "authfinish"));
    if (req.method !== "GET" && !authFlow) {
      const ip = clientIp(req);
      const rlKey = "histatu:rl:dungeon:" + ip; // separate window from the items API so mapping sessions don't starve price edits
      const n = await redis(c, ["INCR", rlKey]);
      if (n === 1) await redis(c, ["EXPIRE", rlKey, 60]);
      // NX: repair a crashed INCR-without-EXPIRE (key stuck with no TTL) WITHOUT extending the
      // window on every rejected request — an unconditional EXPIRE here let a client retrying
      // faster than 60s re-arm the window forever (a 429 livelock: the fixed window could never
      // lapse while the client kept retrying, so the retry loop never terminated).
      else if (n > 30) {
        try { await redis(c, ["EXPIRE", rlKey, 60, "NX"]); } catch (e) { /* store without EXPIRE-NX: skipping the TTL repair is safe; never turn a 429 into a 5xx */ }
        return res.status(429).json({ error: "too many changes — slow down" });
      }
    }
    // Brute-force guard on credentials: a PRESENTED-but-unresolvable key (master OR player) counts
    // against a per-IP failure window, GET included — GETs answer identity and skip the write
    // limiter above, and with this source public that would otherwise be an unlimited-rate yes/no
    // oracle. Only failures count, so legitimate users are never throttled by their own traffic.
    const who = await actor(c, req);
    if (!who && (presentedKey(req) || presentedPlayerKey(req))) {
      const afKey = "histatu:af:dungeon:" + clientIp(req);
      const fails = await redis(c, ["INCR", afKey]);
      if (fails === 1) await redis(c, ["EXPIRE", afKey, 600]);
      if (fails > 15) return res.status(429).json({ error: "too many bad key attempts — wait a few minutes" });
    }
    if (req.method === "GET") {
      const flat = (await redis(c, ["HGETALL", KEY])) || [];
      const entries = {};
      for (let i = 0; i < flat.length; i += 2) {
        const id = flat[i];
        if (id === "pkeys") continue; // key-hash index — server internal, never served
        try {
          const e = JSON.parse(flat[i + 1]);
          if (e && e.type === "player") delete e.keyHash; // bindings are public, hashes are not
          entries[id] = e;
        } catch (e) { /* skip corrupt */ }
      }
      res.setHeader("Cache-Control", "no-store");
      // me: the caller's verified identity (from x-player-key) — lets the site and the app show an
      // honest "✓ name · role" badge. keyValid kept for older clients (true = some valid credential).
      return res.status(200).json({ entries, authRequired: true, keyValid: !!who,
        me: who ? { ign: who.ign, role: who.role, uuid: who.uuid || null } : null });
    }
    if (req.method === "POST" || req.method === "PUT") {
      // ---- Hytale sign-in flow (has its own limits: polling would exhaust the 30/min window) ----
      if (body && body.type === "authstart") {
        const arl = "histatu:rl:auth:" + clientIp(req); // starts are the costly call — 6 per 10 min
        const an = await redis(c, ["INCR", arl]);
        if (an === 1) await redis(c, ["EXPIRE", arl, 600]);
        if (an > 6) return res.status(429).json({ error: "too many sign-in attempts — wait a few minutes" });
        let d;
        try {
          const r = await hytaleForm(HYTALE_OAUTH_BASE + "/oauth2/device/auth",
            { client_id: HYTALE_CLIENT_ID, scope: HYTALE_SCOPE });
          if (!r.ok) throw new Error("device auth " + r.status);
          d = await r.json();
        } catch (e) { return res.status(502).json({ error: "could not reach Hytale sign-in — try again shortly" }); }
        if (!d || !d.device_code || !d.user_code) return res.status(502).json({ error: "unexpected response from Hytale sign-in" });
        const handle = require("crypto").randomBytes(16).toString("base64url");
        // honor the server's interval exactly (0 included), defaulting to RFC 8628's 5s, capped sane
        const ivRaw = Number(d.interval);
        const iv = Number.isFinite(ivRaw) && ivRaw >= 0 ? Math.min(ivRaw, 30) : 5;
        await authStateSet(c, handle, { dc: d.device_code, interval: iv, last: 0 });
        return res.status(200).json({ handle, userCode: d.user_code,
          verificationUri: d.verification_uri || "https://accounts.hytale.com/device",
          verificationUriComplete: d.verification_uri_complete || null,
          expiresIn: Math.min(Number(d.expires_in) || 600, 600),
          interval: iv });
      }
      if (body && (body.type === "authpoll" || body.type === "authfinish")) {
        const st = await authStateGet(c, body.handle);
        if (!st) return res.status(400).json({ error: "sign-in expired — start again" });
        if (body.type === "authfinish" || st.profiles) {
          // account has several game profiles: the site sends back the chosen uuid
          const list = st.profiles || [];
          const pick = body.type === "authfinish"
            ? list.find((p) => p.uuid === String(body.uuid || "")) : null;
          if (body.type === "authpoll" && st.profiles) return res.status(200).json({ chooseFrom: list });
          if (!pick) return res.status(400).json({ error: "pick one of the offered profiles" });
          const bound = await bindPlayer(c, pick);
          if (bound.error) { await authStateDel(c, body.handle); return res.status(409).json({ error: bound.error }); }
          await authStateDel(c, body.handle);
          return res.status(200).json({ done: true, playerKey: bound.key,
            ign: bound.player.ign, role: bound.player.role, uuid: bound.player.uuid });
        }
        // respect Hytale's polling interval no matter how fast the site polls us (interval 0 = no wait)
        const now = Date.now();
        const iv = typeof st.interval === "number" ? st.interval : 5;
        if (now - (st.last || 0) < iv * 1000) return res.status(200).json({ pending: true });
        let t;
        try {
          const r = await hytaleForm(HYTALE_OAUTH_BASE + "/oauth2/token", {
            grant_type: "urn:ietf:params:oauth:grant-type:device_code",
            device_code: st.dc, client_id: HYTALE_CLIENT_ID });
          t = await r.json().catch(() => null);
          if (!t) throw new Error("token parse");
        } catch (e) { return res.status(502).json({ error: "could not reach Hytale sign-in — try again shortly" }); }
        if (t.error === "authorization_pending") { st.last = now; await authStateSet(c, body.handle, st); return res.status(200).json({ pending: true }); }
        if (t.error === "slow_down") { st.last = now; st.interval = (st.interval || 5) + 5; await authStateSet(c, body.handle, st); return res.status(200).json({ pending: true }); }
        if (t.error || !t.access_token) { await authStateDel(c, body.handle); return res.status(400).json({ error: "sign-in was denied or timed out — start again" }); }
        // approved: read the account's game profile(s) ONCE, then every Hytale token is discarded
        let profiles = [];
        try {
          const pr = await fetch(HYTALE_DATA_BASE + "/my-account/get-profiles",
            { headers: { Authorization: "Bearer " + t.access_token, "User-Agent": "histatu-dungeon" } });
          if (!pr.ok) throw new Error("profiles " + pr.status);
          const pj = await pr.json();
          profiles = (Array.isArray(pj && pj.profiles) ? pj.profiles : [])
            .map((p) => ({ uuid: String(p.uuid || "").toLowerCase(), username: str(p.username, 24) }))
            .filter((p) => UUID_RE.test(p.uuid) && p.username);
        } catch (e) { await authStateDel(c, body.handle); return res.status(502).json({ error: "signed in, but could not read your game profile — try again" }); }
        if (!profiles.length) { await authStateDel(c, body.handle); return res.status(400).json({ error: "that Hytale account has no game profile" }); }
        if (profiles.length > 1) { st.profiles = profiles; delete st.dc; await authStateSet(c, body.handle, st); return res.status(200).json({ chooseFrom: profiles }); }
        const bound = await bindPlayer(c, profiles[0]);
        if (bound.error) { await authStateDel(c, body.handle); return res.status(409).json({ error: bound.error }); }
        await authStateDel(c, body.handle);
        return res.status(200).json({ done: true, playerKey: bound.key,
          ign: bound.player.ign, role: bound.player.role, uuid: bound.player.uuid });
      }
      // ---- role management: the owner grants/revokes editor on verified players ----
      if (body && body.type === "role") {
        if (!who || who.role !== "owner") return res.status(403).json({ error: "owner only" });
        const uuid = String(body.uuid || "").toLowerCase();
        const role = body.role === "editor" ? "editor" : body.role === "player" ? "player" : null;
        if (!UUID_RE.test(uuid) || !role) return res.status(400).json({ error: "need uuid + role editor|player" });
        const pRaw = await redis(c, ["HGET", KEY, "player-" + uuid]);
        if (!pRaw) return res.status(404).json({ error: "no such player" });
        let p; try { p = JSON.parse(pRaw); } catch (e) { return res.status(500).json({ error: "corrupt player entry" }); }
        if (p.role === "owner") return res.status(400).json({ error: "the owner role is fixed to the OWNER_IGN account" });
        p.role = role; p.updatedAt = Date.now();
        await redis(c, ["HSET", KEY, p.id, JSON.stringify(p)]);
        return res.status(200).json({ ok: true, uuid: p.uuid, ign: p.ign, role: p.role });
      }
      // BULK map-structure write (editor only): one request re-places up to 100 markers/areas —
      // built for ⤾ Re-align pins, where hundreds of single POSTs would take ~20 min against the
      // 30/min rate limit. Every item passes the same validEntry sanitizer; only map STRUCTURE
      // types are allowed (never the shared merge-typed aggregates — runs/travel/opens/rating keep
      // their add-only merge semantics and must not gain a wholesale side door).
      if (body && body.type === "bulk") {
        if (!isEditor(who)) return res.status(403).json({ error: "editor access required" });
        if (!Array.isArray(body.entries) || body.entries.length < 1 || body.entries.length > 100)
          return res.status(400).json({ error: "bulk needs 1-100 entries" });
        const byId = {};
        for (const raw of body.entries) {
          const v = validEntry(raw);
          if (!v || (v.type !== "marker" && v.type !== "area"))
            return res.status(400).json({ error: "bulk allows valid marker/area entries only" });
          byId[v.id] = v; // duplicate ids within one request: last one wins, counted once
        }
        const items = Object.keys(byId).map((k) => byId[k]);
        await dailyBackup(c);
        // total-entry ceiling still applies to NEW ids (existing ids are pure position updates);
        // the count-check + writes run atomically in one script (see BULK_LUA)
        const args = ["EVAL", BULK_LUA, "1", KEY, String(MAX_ENTRIES)];
        for (const it of items) args.push(it.id, JSON.stringify(it));
        const stored = await redis(c, args);
        if (stored === -1) return res.status(400).json({ error: "entry limit reached" });
        return res.status(200).json({ ok: true, count: items.length });
      }
      const entry = validEntry(body);
      if (!entry) return res.status(400).json({ error: "invalid entry" });
      // ---- permission matrix: every write is a signed-in write ----
      // Map STRUCTURE needs an editor; PERSONAL data needs the key bound to that very identity —
      // a player physically cannot write opens/runs/ratings under anyone else's name, which is
      // the whole point of verified sign-in. Editors/owner keep wholesale powers for cleanup and
      // backup-restore.
      const editorOnly = !OPEN_POST_TYPES.has(entry.type);
      if (editorOnly && !isEditor(who)) return res.status(403).json({ error: "editor access required" });
      if (!editorOnly && !who) return res.status(403).json({ error: "sign in on the website first (it gives you a key for the app)" });
      if (!isEditor(who)) {
        if (entry.type === "opens") {
          if (entry.id !== "opens-" + who.slug) return res.status(403).json({ error: "you can only write your own open log" });
          entry.ign = who.ign; // server-authoritative attribution
        }
        if (entry.type === "runs") {
          for (const k in entry.best || {}) {
            if (k.slice(k.lastIndexOf("|") + 1) !== who.slug) return res.status(403).json({ error: "you can only submit your own runs" });
            if (entry.best[k]) entry.best[k].ign = who.ign; // display name is server-authoritative too
          }
          for (const it of entry.recent || []) {
            if (slugify(it.ign) !== who.slug) return res.status(403).json({ error: "you can only submit your own runs" });
            it.ign = who.ign;
          }
        }
        if (entry.type === "rating") {
          for (const k in entry.by || {}) {
            if (k !== who.slug) return res.status(403).json({ error: "you can only rate as yourself" });
            if (entry.by[k]) entry.by[k].ign = who.ign; // stop a forged display name in the comment
          }
        }
        if (entry.type === "pending") entry.by = who.ign; // honest crowd-source attribution
      }
      await dailyBackup(c);
      let toStore = entry;
      // a KEYLESS write to a shared aggregate may only add/improve — merge it into the stored
      // value so an anonymous empty/forged payload can't wipe or shrink everyone's data.
      // Editors (with the key) still overwrite wholesale (needed for cleanup / backup-restore).
      // (The HGET+EVAL below is a read-modify-write, not atomic: two keyless writes to the same
      // aggregate within one round-trip can drop one addition. It's self-healing — the loser
      // resubmits — and far safer than the old wholesale overwrite, so we accept the small window.)
      if (MERGE_TYPES.has(entry.type) && !isEditor(who)) {
        const prevRaw = await redis(c, ["HGET", KEY, entry.id]);
        let prev = null;
        if (prevRaw) { try { prev = JSON.parse(prevRaw); } catch (e) { /* replace corrupt */ } }
        toStore = mergeEntry(prev, entry);
      } else if (entry.type === "travel" && entry.legs === undefined) {
        // EDITOR overwrite from a legs-UNAWARE build (the field was absent from the POST, so
        // validEntry left it undefined): fold the stored directed legs in, so a wholesale write
        // can't wipe a field the client doesn't know exists. A legs-aware editor that explicitly
        // sends legs (even {}) still overwrites wholesale — that stays available for cleanup.
        const prevRaw = await redis(c, ["HGET", KEY, entry.id]);
        let prevLegs = {};
        if (prevRaw) {
          try {
            const prev = JSON.parse(prevRaw);
            if (prev && prev.legs && typeof prev.legs === "object" && !Array.isArray(prev.legs)) prevLegs = prev.legs;
          } catch (e) { /* replace corrupt */ }
        }
        toStore = Object.assign({}, entry, { legs: prevLegs });
      } else if (entry.type === "runs" && entry.stats === undefined) {
        // EDITOR runs overwrite from a stats-UNAWARE build: fold the stored per-route lifetime run
        // stats back in, so a wholesale write (leaderboard cleanup, backup-restore) can't wipe the
        // run aggregate the client never sends. An editor that explicitly sends stats still overwrites.
        const prevRaw = await redis(c, ["HGET", KEY, entry.id]);
        let prevStats = {};
        if (prevRaw) {
          try {
            const prev = JSON.parse(prevRaw);
            if (prev && prev.stats && typeof prev.stats === "object" && !Array.isArray(prev.stats)) prevStats = prev.stats;
          } catch (e) { /* replace corrupt */ }
        }
        toStore = Object.assign({}, entry, { stats: prevStats });
      }
      // keyless opens/pending are bounded by their OWN count (KEYLESS_SOFT), leaving editor headroom
      const keyless = keylessSoft(entry.type) ? "1" : "0";
      const result = await redis(c, ["EVAL", UPSERT_LUA, "1", KEY,
        entry.id, JSON.stringify(toStore), String(MAX_ENTRIES), keyless, String(KEYLESS_SOFT)]);
      if (result === -1) return res.status(400).json({ error: "entry limit reached" });
      return res.status(200).json({ ok: true, entry: toStore });
    }
    if (req.method === "DELETE") {
      const id = (req.query && req.query.id) || "";
      // player-binding ids ("player-" + full uuid) legitimately exceed ID_RE's 40-char cap
      if (!ID_RE.test(id) && !/^player-[0-9a-f-]{36}$/i.test(id)) return res.status(400).json({ error: "invalid id" });
      if (id === "pkeys") return res.status(403).json({ error: "no" }); // the index is not an entry
      if (id.startsWith("player-")) {
        // releasing an identity binding (e.g. a renamed account holding a now-contested name) is
        // an OWNER decision — it invalidates that player's key
        if (!who || who.role !== "owner") return res.status(403).json({ error: "owner only" });
        const uuid = id.slice("player-".length);
        let idx = { map: {} };
        const idxRaw = await redis(c, ["HGET", KEY, "pkeys"]);
        if (idxRaw) { try { const j = JSON.parse(idxRaw); if (j && j.map) idx = j; } catch (e) { /* rebuild */ } }
        for (const h in idx.map) if (idx.map[h] === uuid) delete idx.map[h];
        await dailyBackup(c);
        await redis(c, ["HSET", KEY, "pkeys", JSON.stringify(idx)]);
        await redis(c, ["HDEL", KEY, id]);
        return res.status(200).json({ ok: true });
      }
      // deleting/rejecting map content is an editor action
      if (!isEditor(who)) return res.status(403).json({ error: "editor access required" });
      await dailyBackup(c);
      await redis(c, ["HDEL", KEY, id]);
      return res.status(200).json({ ok: true });
    }
    res.setHeader("Allow", "GET, POST, PUT, DELETE");
    return res.status(405).json({ error: "method not allowed" });
  } catch (e) {
    return res.status(502).json({ error: "storage unavailable" });
  }
};
