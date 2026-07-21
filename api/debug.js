// Detection-issue reports from the Histatu Runner companion app.
//
// A user having trouble getting a chest detected can turn on a short (~3 min) capture; the app
// uploads a small bundle — app version, platform/resolution, the parse log, and a few JPEGs of
// the TOP-RIGHT game panel region (NOT full-screen) — for review. Screenshots are the user's own
// screen, uploaded only on their explicit action.
//
// Storage: the bundle is NOT kept on this site. It is forwarded to a PRIVATE GitHub gist (owned
// by the review token), and this endpoint only proxies. Reviewers list/fetch with the editor key;
// once reviewed, a report is fully DELETED (the gist is removed — no site copy, no git history).
//
//   POST   /api/debug            create a report  -> { ok, id }        (keyless, rate-limited, capped)
//   GET    /api/debug?list=1     list report metadata                  (editor key required)
//   GET    /api/debug?id=<id>    fetch one full report                 (editor key required)
//   DELETE /api/debug?id=<id>    permanently delete a reviewed report  (editor key required)
//
// Setup: set GITHUB_GIST_TOKEN to a token that may create/read/delete gists — a fine-grained PAT
// with the "Gists" account permission (Read and write), or a classic token with the `gist` scope.
// A dedicated minimal token is preferred so the download token can stay Contents:Read only.
// Falls back to GITHUB_TOKEN if GITHUB_GIST_TOKEN is unset.

const GIST_API = "https://api.github.com/gists";
const DESC_PREFIX = "histatu-debug";
const MAX_BYTES = 1_200_000;   // reject bundles larger than this (well under GitHub's gist limits)
const MAX_FRAMES = 12;
const LIST_MAX = 60;           // how many recent gists to scan when listing

function gistToken() {
  return (process.env.GITHUB_GIST_TOKEN || process.env.GITHUB_TOKEN || "").trim();
}
function storeCfg() {
  const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  return (url && token) ? { url, token } : null;
}
async function redis(c, cmd) {
  const r = await fetch(c.url, { method: "POST",
    headers: { Authorization: "Bearer " + c.token, "Content-Type": "application/json" },
    body: JSON.stringify(cmd) });
  if (!r.ok) throw new Error("rl http " + r.status);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j.result;
}
async function github(method, url, token, body) {
  return fetch(url, {
    method,
    headers: {
      Authorization: "Bearer " + token,
      Accept: "application/vnd.github+json",
      "User-Agent": "histatu-debug",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

function writeKey() { const k = (process.env.DUNGEON_WRITE_KEY || "").trim(); return k || null; }
function presentedKey(req) {
  const h = (req.headers && (req.headers["x-write-key"] || req.headers["authorization"])) || "";
  return String(h).replace(/^Bearer\s+/i, "").trim();
}
function authed(req) {
  const key = writeKey();
  if (!key) return true; // no key configured -> open (matches the map's model)
  const given = presentedKey(req);
  if (!given) return false;
  // hash both sides before the constant-time compare — neither key length nor content can leak
  // through timing (this source is public; assume attackers read it)
  const crypto = require("crypto");
  const a = crypto.createHash("sha256").update(given).digest();
  const b = crypto.createHash("sha256").update(key).digest();
  return crypto.timingSafeEqual(a, b);
}
function clientIp(req) {
  const h = req.headers || {};
  const real = String(h["x-real-ip"] || "").trim();
  if (real) return real;
  const xff = String(h["x-forwarded-for"] || "").split(",").map(s => s.trim()).filter(Boolean);
  return xff.length ? xff[xff.length - 1] : "unknown";
}
function str(v, max) { return typeof v === "string" ? v.slice(0, max) : ""; }
function isGistId(v) { return typeof v === "string" && /^[0-9a-f]{6,64}$/i.test(v); }

// keep only expected fields, hard-cap each, and accept only base64 (optionally data-URI) images.
function sanitize(b) {
  if (!b || typeof b !== "object") return null;
  const out = {
    at: Date.now(),
    version: str(b.version, 20),
    platform: str(b.platform, 60),
    resolution: str(b.resolution, 20),
    note: str(b.note, 500),
    log: str(b.log, 60000),
    frames: [],
  };
  if (Array.isArray(b.frames)) {
    for (const f of b.frames.slice(0, MAX_FRAMES)) {
      if (!f || typeof f !== "object") continue;
      const img = typeof f.jpg === "string" ? f.jpg : "";
      if (!/^(data:image\/jpe?g;base64,)?[A-Za-z0-9+/=]+$/.test(img) || img.length > 300000) continue;
      out.frames.push({ note: str(f.note, 300), jpg: img });
    }
  }
  return out;
}

async function readGist(id, token) {
  const r = await github("GET", GIST_API + "/" + id, token);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error("gist get " + r.status);
  const g = await r.json();
  const file = g.files && g.files["report.json"];
  if (!file) return null;
  let content = file.content;
  if (file.truncated && file.raw_url) {  // large files come back truncated; fetch the raw blob
    const raw = await fetch(file.raw_url, { headers: { Authorization: "Bearer " + token, "User-Agent": "histatu-debug" } });
    content = await raw.text();
  }
  try { return JSON.parse(content); } catch (e) { return null; }
}

module.exports = async (req, res) => {
  const token = gistToken();
  if (!token) return res.status(503).json({ error: "reports not configured (set GITHUB_GIST_TOKEN)" });
  try {
    if (req.method === "POST") {
      // rate limit by trusted IP (the only thing this endpoint keeps on the site)
      const c = storeCfg();
      if (c) {
        const rlKey = "histatu:rl:debug:" + clientIp(req);
        const n = await redis(c, ["INCR", rlKey]);
        if (n === 1) await redis(c, ["EXPIRE", rlKey, 600]);
        // NX: repair a missing TTL without re-arming the window on every rejected request
        // (an unconditional EXPIRE here livelocks any client that retries faster than the window)
        else if (n > 6) {
          try { await redis(c, ["EXPIRE", rlKey, 600, "NX"]); } catch (e) { /* store without EXPIRE-NX: skip the TTL repair, never turn a 429 into a 5xx */ }
          return res.status(429).json({ error: "too many reports — try again later" });
        }
      }
      let body;
      try { body = req.body; } catch (e) { return res.status(400).json({ error: "invalid JSON" }); }
      const report = sanitize(body);
      if (!report) return res.status(400).json({ error: "invalid report" });
      const payload = JSON.stringify(report);
      if (payload.length > MAX_BYTES) return res.status(413).json({ error: "report too large" });

      const desc = [DESC_PREFIX, report.version, report.platform, report.resolution,
        new Date(report.at).toISOString()].join(" ");
      const r = await github("POST", GIST_API, token,
        { description: desc, public: false, files: { "report.json": { content: payload } } });
      if (!r.ok) return res.status(502).json({ error: "could not file the report" });
      const g = await r.json();
      return res.status(200).json({ ok: true, id: g.id });
    }

    // everything below is review-only — and unlike the open map, it FAILS CLOSED: reports are
    // users' own-screen captures, so with no editor key configured there is no review access.
    if (!writeKey()) return res.status(503).json({ error: "review not configured (set DUNGEON_WRITE_KEY)" });
    if (!authed(req)) {
      // brute-force guard: failed key attempts count against a per-IP window (public source
      // means the 403-vs-200 contrast is a known oracle; only failures are ever throttled)
      const c2 = storeCfg();
      if (c2) {
        const afKey = "histatu:af:debug:" + clientIp(req);
        const fails = await redis(c2, ["INCR", afKey]);
        if (fails === 1) await redis(c2, ["EXPIRE", afKey, 600]);
        if (fails > 15) return res.status(429).json({ error: "too many bad key attempts — wait a few minutes" });
      }
      return res.status(403).json({ error: "editor key required to review reports" });
    }
    res.setHeader("Cache-Control", "no-store");

    if (req.method === "GET") {
      const q = req.query || {};
      if (q.id) {
        if (!isGistId(String(q.id))) return res.status(400).json({ error: "bad id" });
        const rep = await readGist(String(q.id), token);
        if (!rep) return res.status(404).json({ error: "not found (may have been deleted)" });
        return res.status(200).json(rep);
      }
      const r = await github("GET", GIST_API + "?per_page=" + LIST_MAX, token);
      if (!r.ok) return res.status(502).json({ error: "could not list reports" });
      const gists = await r.json();
      const reports = (Array.isArray(gists) ? gists : [])
        .filter(g => typeof g.description === "string" && g.description.startsWith(DESC_PREFIX))
        .map(g => ({ id: g.id, at: g.created_at, description: g.description }));
      return res.status(200).json({ reports });
    }

    if (req.method === "DELETE") {
      const q = req.query || {};
      if (!isGistId(String(q.id || ""))) return res.status(400).json({ error: "bad id" });
      const r = await github("DELETE", GIST_API + "/" + String(q.id), token);
      if (r.status === 404) return res.status(404).json({ error: "already gone" });
      if (!(r.status === 204 || r.ok)) return res.status(502).json({ error: "could not delete" });
      return res.status(200).json({ ok: true, deleted: String(q.id) });
    }

    res.setHeader("Allow", "GET, POST, DELETE");
    return res.status(405).json({ error: "method not allowed" });
  } catch (e) {
    return res.status(502).json({ error: "review service unavailable" });
  }
};
