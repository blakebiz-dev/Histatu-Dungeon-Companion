// Tests for api/debug.js — the detection-issue report endpoint (GitHub-gist backed).
// Run:  node api/__tests__/debug.test.js
const fs = require("fs");
const nodePath = require("path");
const src = fs.readFileSync(nodePath.join(__dirname, "..", "debug.js"), "utf8");

let fail = 0;
const t = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + " " + name); if (!cond) fail++; };

// combined fake: Upstash REST (rate-limit counter) + GitHub gists API
function makeWorld() {
  const kv = new Map();
  const gists = new Map();     // id -> { description, files }
  let seq = 0, deleted = [];
  async function fakeFetch(url, opts) {
    opts = opts || {};
    const method = opts.method || "GET";
    if (url.startsWith("https://fake")) { // Upstash
      const cmd = JSON.parse(opts.body), op = cmd[0];
      let result = null;
      if (op === "INCR") { const n = (kv.get(cmd[1]) || 0) + 1; kv.set(cmd[1], n); result = n; }
      else if (op === "EXPIRE") result = 1;
      else throw new Error("unmocked rl op " + op);
      return { ok: true, status: 200, json: async () => ({ result }) };
    }
    if (url.startsWith("https://api.github.com/gists")) {
      if (method === "POST") {
        const b = JSON.parse(opts.body);
        const id = "abc" + (seq++).toString(16).padStart(3, "0"); // hex-ish id
        gists.set(id, { id, description: b.description, created_at: "2026-07-16T00:00:00Z",
          files: { "report.json": { content: b.files["report.json"].content, truncated: false } } });
        return { ok: true, status: 201, json: async () => ({ id }) };
      }
      const m = url.match(/gists\/([0-9a-f]+)/i);
      if (m) {
        const id = m[1];
        if (method === "DELETE") { if (!gists.has(id)) return { ok: false, status: 404 }; gists.delete(id); deleted.push(id); return { ok: true, status: 204 }; }
        const g = gists.get(id);
        if (!g) return { ok: false, status: 404 };
        return { ok: true, status: 200, json: async () => g };
      }
      // list
      return { ok: true, status: 200, json: async () => Array.from(gists.values()) };
    }
    throw new Error("unmocked url " + url);
  }
  return { kv, gists, fakeFetch, deleted: () => deleted };
}
function loadHandler(env, world) {
  const mod = { exports: {} };
  const base = { GITHUB_GIST_TOKEN: "ghtok", DUNGEON_WRITE_KEY: "s3cret",
    KV_REST_API_URL: "https://fake", KV_REST_API_TOKEN: "tok" };
  const fakeProcess = { env: Object.assign(base, env) };
  new Function("module", "exports", "require", "process", "fetch", src)(mod, mod.exports, require, fakeProcess, world.fakeFetch);
  return mod.exports;
}
function mkRes() {
  const res = { statusCode: 0, body: null, headers: {} };
  res.setHeader = (k, v) => { res.headers[k] = v; };
  res.status = (code) => { res.statusCode = code; return { json: (o) => { res.body = o; return res; }, end: () => res }; };
  return res;
}
const call = (h, req) => { const res = mkRes(); return h(req, res).then(() => res); };
const IMG = "data:image/jpeg;base64," + "A".repeat(400);
const REP = () => ({ version: "1.0.11", platform: "win32", resolution: "2560x1369",
  note: "IGN=Rev", log: "log", frames: [{ note: "pos=None", jpg: IMG }] });

(async () => {
  {
    const w = makeWorld();
    const h = loadHandler({}, w);
    const r = await call(h, { method: "POST", headers: {}, body: REP() });
    t("keyless POST creates a private gist", r.statusCode === 200 && /^abc/.test(r.body.id));
    t("gist stored as private with histatu-debug description", (() => { const g = [...w.gists.values()][0]; return g && g.description.startsWith("histatu-debug"); })());
    const id = r.body.id;
    t("GET without editor key -> 403", (await call(h, { method: "GET", headers: {}, query: { id } })).statusCode === 403);
    const list = await call(h, { method: "GET", headers: { "x-write-key": "s3cret" }, query: { list: "1" } });
    t("editor list returns metadata (no images)", list.statusCode === 200 && list.body.reports.length === 1 && list.body.reports[0].jpg === undefined && /histatu-debug/.test(list.body.reports[0].description));
    const one = await call(h, { method: "GET", headers: { "x-write-key": "s3cret" }, query: { id } });
    t("editor fetch returns the full bundle with the frame", one.statusCode === 200 && one.body.frames[0].jpg === IMG && one.body.version === "1.0.11");
    // DELETE gated + actually removes the gist ("fully deleted after review")
    t("DELETE without key -> 403", (await call(h, { method: "DELETE", headers: {}, query: { id } })).statusCode === 403);
    const del = await call(h, { method: "DELETE", headers: { "x-write-key": "s3cret" }, query: { id } });
    t("editor DELETE removes the report", del.statusCode === 200 && w.deleted().includes(id) && !w.gists.has(id));
    t("fetch after delete -> 404", (await call(h, { method: "GET", headers: { "x-write-key": "s3cret" }, query: { id } })).statusCode === 404);
  }
  // frames capped + junk dropped
  {
    const w = makeWorld();
    const h = loadHandler({}, w);
    const frames = []; for (let i = 0; i < 20; i++) frames.push({ note: "f", jpg: IMG });
    frames.push({ note: "bad", jpg: "<script>x</script>" });
    const r = await call(h, { method: "POST", headers: {}, body: { version: "x", frames } });
    const one = await call(h, { method: "GET", headers: { "x-write-key": "s3cret" }, query: { id: r.body.id } });
    t("frames capped to 12, junk dropped", one.body.frames.length === 12 && one.body.frames.every(f => f.jpg === IMG));
  }
  // bad id rejected
  {
    const w = makeWorld();
    const h = loadHandler({}, w);
    t("bad gist id -> 400", (await call(h, { method: "GET", headers: { "x-write-key": "s3cret" }, query: { id: "../etc/passwd" } })).statusCode === 400);
  }
  // rate limit
  {
    const w = makeWorld();
    const h = loadHandler({}, w);
    let last; for (let i = 0; i < 8; i++) last = await call(h, { method: "POST", headers: { "x-real-ip": "9.9.9.9" }, body: REP() });
    t("rate limit after several reports", last.statusCode === 429);
  }
  // no token
  {
    const w = makeWorld();
    const h = loadHandler({ GITHUB_GIST_TOKEN: "", GITHUB_TOKEN: "" }, w);
    t("no gist token -> 503", (await call(h, { method: "POST", headers: {}, body: REP() })).statusCode === 503);
  }
  // review routes FAIL CLOSED with no write key configured (POST still works)
  {
    const w = makeWorld();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "" }, w);
    const r = await call(h, { method: "POST", headers: {}, body: REP() });
    t("keyless deploy: POST still accepted", r.statusCode === 200);
    t("keyless deploy: list fails closed", (await call(h, { method: "GET", headers: {}, query: { list: "1" } })).statusCode === 503);
    t("keyless deploy: fetch fails closed", (await call(h, { method: "GET", headers: {}, query: { id: r.body.id } })).statusCode === 503);
    t("keyless deploy: delete fails closed", (await call(h, { method: "DELETE", headers: {}, query: { id: r.body.id } })).statusCode === 503);
  }
  console.log("\n" + fail + " failed");
  process.exit(fail ? 1 : 0);
})();
