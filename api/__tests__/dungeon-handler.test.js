// Integration tests: drive the REAL api/dungeon.js handler against an in-memory fake Redis,
// to verify the security fixes at the request boundary (not just validEntry).
// Run:  node api/__tests__/dungeon-handler.test.js
const fs = require("fs");
const nodePath = require("path");
const src = fs.readFileSync(nodePath.join(__dirname, "..", "dungeon.js"), "utf8");

let fail = 0;
const t = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + " " + name); if (!cond) fail++; };

// ---- in-memory fake Redis over the Upstash REST command protocol, plus fake Hytale services ----
function makeStore() {
  const hashes = new Map();   // hashKey -> Map(field -> jsonString)
  const kv = new Map();       // simple keys (rate limit counters, auth state, stamp)
  const h = (k) => { if (!hashes.has(k)) hashes.set(k, new Map()); return hashes.get(k); };
  // programmable stand-in for Hytale's OAuth/account services, routed by URL in fakeFetch
  const hytale = { approved: false, denied: false, deviceFails: false, profileFails: false,
                   profiles: [{ uuid: "11111111-2222-3333-4444-555555555555", username: "BlakeBiz" }] };
  async function fakeFetch(url, opts) {
    if (typeof url === "string" && !url.startsWith("https://fake")) {
      if (url.includes("/oauth2/device/auth")) {
        if (hytale.deviceFails) return { ok: false, status: 500, json: async () => ({}) };
        return { ok: true, status: 200, json: async () => ({ device_code: "dc-1", user_code: "ABCD-1234",
          verification_uri: "https://accounts.hytale.com/device",
          verification_uri_complete: "https://accounts.hytale.com/device?user_code=ABCD-1234",
          expires_in: 600, interval: 0 }) };
      }
      if (url.includes("/oauth2/token")) {
        if (hytale.denied) return { ok: false, status: 400, json: async () => ({ error: "access_denied" }) };
        if (!hytale.approved) return { ok: false, status: 400, json: async () => ({ error: "authorization_pending" }) };
        return { ok: true, status: 200, json: async () => ({ access_token: "at-1", token_type: "Bearer", expires_in: 3600 }) };
      }
      if (url.includes("/my-account/get-profiles")) {
        if (hytale.profileFails) return { ok: false, status: 500, json: async () => ({}) };
        return { ok: true, status: 200, json: async () => ({ owner: "acct-1", profiles: hytale.profiles }) };
      }
      throw new Error("unmocked external fetch: " + url);
    }
    const cmd = JSON.parse(opts.body);
    const op = cmd[0];
    let result = null;
    if (op === "INCR") { const n = (kv.get(cmd[1]) || 0) + 1; kv.set(cmd[1], n); result = n; }
    else if (op === "EXPIRE") { result = 1; }
    else if (op === "GET") { result = kv.has(cmd[1]) ? kv.get(cmd[1]) : null; }
    else if (op === "DEL") { result = kv.delete(cmd[1]) ? 1 : 0; }
    else if (op === "HGETALL") { const m = hashes.get(cmd[1]); result = []; if (m) for (const [f, val] of m) { result.push(f, val); } }
    else if (op === "HGET") { const m = hashes.get(cmd[1]); result = (m && m.has(cmd[2])) ? m.get(cmd[2]) : null; }
    else if (op === "HMGET") { const m = hashes.get(cmd[1]); result = cmd.slice(2).map((f) => (m && m.has(f)) ? m.get(f) : null); }
    else if (op === "HLEN") { const m = hashes.get(cmd[1]); result = m ? m.size : 0; }
    else if (op === "HSET") { const m = h(cmd[1]); let added = 0; for (let i = 2; i + 1 < cmd.length; i += 2) { if (!m.has(cmd[i])) added++; m.set(cmd[i], cmd[i + 1]); } result = added; }
    else if (op === "HDEL") { const m = hashes.get(cmd[1]); result = (m && m.delete(cmd[2])) ? 1 : 0; }
    else if (op === "SET") { if (cmd.includes("NX") && kv.has(cmd[1])) result = null; else { kv.set(cmd[1], cmd[2]); result = "OK"; } }
    else if (op === "COPY") { hashes.set(cmd[2], new Map(h(cmd[1]))); result = 1; }
    else if (op === "EVAL" && String(cmd[1]).includes("local fresh")) {
      // BULK_LUA: ARGV[1] = hard cap, then (id, json) pairs; all-or-nothing over the cap
      const hashKey = cmd[3], hardCap = Number(cmd[4]);
      const m = h(hashKey);
      let fresh = 0;
      for (let i = 5; i + 1 < cmd.length; i += 2) if (!m.has(cmd[i])) fresh++;
      if (fresh > 0 && m.size + fresh > hardCap) result = -1;
      else { for (let i = 5; i + 1 < cmd.length; i += 2) m.set(cmd[i], cmd[i + 1]); result = (cmd.length - 5) / 2; }
    }
    else if (op === "EVAL") {
      // UPSERT_LUA: for a NEW id, reject if total >= hardCap, or (keyless) if keyless-field count >= softCap
      const hashKey = cmd[3], id = cmd[4], json = cmd[5];
      const hardCap = Number(cmd[6]), isKeyless = cmd[7], softCap = Number(cmd[8]);
      const m = h(hashKey);
      let reject = false;
      if (!m.has(id)) {
        if (m.size >= hardCap) reject = true;
        else if (isKeyless === "1") {
          let n = 0; for (const f of m.keys()) if (f.startsWith("opens-") || f.startsWith("pend-") || f.startsWith("rate-")) n++;
          if (n >= softCap) reject = true;
        }
      }
      if (reject) result = -1; else { m.set(id, json); result = 1; }
    } else { throw new Error("unmocked redis op: " + op); }
    return { ok: true, json: async () => ({ result }) };
  }
  return { hashes, kv, fakeFetch, h, hytale };
}

// load the handler with a given env + fake fetch
function loadHandler(env, store) {
  const mod = { exports: {} };
  const fakeProcess = { env: Object.assign({ KV_REST_API_URL: "https://fake", KV_REST_API_TOKEN: "tok" }, env) };
  new Function("module", "exports", "require", "process", "fetch", src)(mod, mod.exports, require, fakeProcess, store.fakeFetch);
  return mod.exports;
}

function mkRes() {
  const res = { statusCode: 0, body: null, headers: {} };
  res.setHeader = (k, v) => { res.headers[k] = v; };
  res.status = (code) => { res.statusCode = code; return { json: (obj) => { res.body = obj; return res; } }; };
  return res;
}
async function call(handler, { method = "GET", headers = {}, body = null, query = {} } = {}) {
  const res = mkRes();
  await handler({ method, headers, body, query }, res);
  return res;
}
// run the whole device-flow sign-in against the fake Hytale for `username`; returns {playerKey, ign, role}
async function signIn(h, store, username) {
  store.hytale.profiles = [{ uuid: uuidFor(username), username }];
  store.hytale.approved = true;
  const s = await call(h, { method: "POST", body: { type: "authstart" } });
  if (s.statusCode !== 200) throw new Error("authstart " + s.statusCode + " " + JSON.stringify(s.body));
  const p = await call(h, { method: "POST", body: { type: "authpoll", handle: s.body.handle } });
  if (!p.body || !p.body.done) throw new Error("authpoll " + p.statusCode + " " + JSON.stringify(p.body));
  return p.body;
}
// deterministic fake profile uuid per username so re-sign-ins bind the same account
function uuidFor(name) {
  const hex = require("crypto").createHash("sha256").update(name).digest("hex");
  return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20, 32);
}

(async () => {
  const KEY = "histatu:dungeon", OKEY = "histatu:dungeon:open";

  // ===== identity: Hytale sign-in, roles, ownership of personal data =====
  {
    const store = makeStore();
    const h = loadHandler({ OWNER_IGN: "BlakeBiz" }, store);
    // owner signs in -> owner role, key issued, binding stored without leaking the hash
    const own = await signIn(h, store, "BlakeBiz");
    t("identity: OWNER_IGN account binds as owner", own.role === "owner" && own.ign === "BlakeBiz" && /^hd_/.test(own.playerKey));
    const g = await call(h, { method: "GET", headers: { "x-player-key": own.playerKey } });
    t("identity: GET me reflects the signed-in owner", g.body.me && g.body.me.ign === "BlakeBiz" && g.body.me.role === "owner");
    t("identity: GET never serves pkeys or key hashes",
      !("pkeys" in g.body.entries) && Object.values(g.body.entries).every((e) => !e.keyHash));
    // a regular player signs in -> role player
    const al = await signIn(h, store, "Alice");
    t("identity: a fresh account binds as player", al.role === "player" && al.ign === "Alice");
    // pending sign-in: not approved yet -> pending:true, no key
    store.hytale.approved = false;
    const s2 = await call(h, { method: "POST", body: { type: "authstart" } });
    const p2 = await call(h, { method: "POST", body: { type: "authpoll", handle: s2.body.handle } });
    t("identity: unapproved poll -> pending, no key", p2.statusCode === 200 && p2.body.pending === true && !p2.body.playerKey);
    // denied -> clear error, state cleared
    store.hytale.denied = true;
    const p3 = await call(h, { method: "POST", body: { type: "authpoll", handle: s2.body.handle } });
    t("identity: denied sign-in -> 400 and the handle dies", p3.statusCode === 400
      && (await call(h, { method: "POST", body: { type: "authpoll", handle: s2.body.handle } })).statusCode === 400);
    store.hytale.denied = false;

    // ---- personal-data ownership ----
    const mkOpens = (slug, ign) => ({ id: "opens-" + slug, type: "opens", ign, opens: { "1,64,1": { t: 5 } } });
    const ok1 = await call(h, { method: "POST", headers: { "x-player-key": al.playerKey }, body: mkOpens("alice", "Alice") });
    t("opens: a player writes their OWN log", ok1.statusCode === 200);
    const forge = await call(h, { method: "POST", headers: { "x-player-key": al.playerKey }, body: mkOpens("blakebiz", "BlakeBiz") });
    t("opens: writing someone ELSE's log -> 403", forge.statusCode === 403);
    const anon = await call(h, { method: "POST", headers: {}, body: mkOpens("alice", "Alice") });
    t("opens: no key at all -> 403 (every write is signed-in now)", anon.statusCode === 403);
    const runForge = await call(h, { method: "POST", headers: { "x-player-key": al.playerKey },
      body: { id: "runs", type: "runs", best: { "r1|blakebiz": { ign: "BlakeBiz", t: 1, c: 9, at: 1 } }, recent: [] } });
    t("runs: forging a record under another name -> 403", runForge.statusCode === 403);
    const runOwn = await call(h, { method: "POST", headers: { "x-player-key": al.playerKey },
      body: { id: "runs", type: "runs", best: { "r1|alice": { ign: "SpoofName", t: 120, c: 9, at: 1 } }, recent: [{ r: "r1", ign: "Alice", t: 120, c: 9, at: 1 }] } });
    t("runs: your own record is accepted, and best.ign is forced to your real name", runOwn.statusCode === 200
      && JSON.parse(store.h(KEY).get("runs")).best["r1|alice"].t === 120
      && JSON.parse(store.h(KEY).get("runs")).best["r1|alice"].ign === "Alice");
    // map structure: players can't, editors can
    const mk = { id: "mX", type: "marker", kind: "chest", x: 0.5, y: 0.5 };
    t("markers: player role -> 403", (await call(h, { method: "POST", headers: { "x-player-key": al.playerKey }, body: mk })).statusCode === 403);

    // ---- roles: owner grants editor by IGN/uuid; revoke works; non-owner cannot ----
    const grant = await call(h, { method: "POST", headers: { "x-player-key": own.playerKey },
      body: { type: "role", uuid: uuidFor("Alice"), role: "editor" } });
    t("roles: owner grants editor", grant.statusCode === 200 && grant.body.role === "editor");
    t("markers: newly-granted editor -> 200", (await call(h, { method: "POST", headers: { "x-player-key": al.playerKey }, body: mk })).statusCode === 200);
    const selfGrant = await call(h, { method: "POST", headers: { "x-player-key": al.playerKey },
      body: { type: "role", uuid: uuidFor("Alice"), role: "editor" } });
    t("roles: an editor cannot grant roles (owner only)", selfGrant.statusCode === 403);
    await call(h, { method: "POST", headers: { "x-player-key": own.playerKey }, body: { type: "role", uuid: uuidFor("Alice"), role: "player" } });
    t("roles: revoke works", (await call(h, { method: "POST", headers: { "x-player-key": al.playerKey }, body: { id: "mY", type: "marker", kind: "chest", x: 0.5, y: 0.5 } })).statusCode === 403);

    // ---- key reset: signing in again rotates the key; the old one dies instantly ----
    const al2 = await signIn(h, store, "Alice");
    t("reset: fresh sign-in issues a NEW key and keeps identity", al2.playerKey !== al.playerKey && al2.ign === "Alice");
    const oldKey = await call(h, { method: "GET", headers: { "x-player-key": al.playerKey } });
    t("reset: the old key is dead immediately", oldKey.body.me === null);
    const newKey = await call(h, { method: "GET", headers: { "x-player-key": al2.playerKey } });
    t("reset: the new key works", newKey.body.me && newKey.body.me.ign === "Alice");

    // ---- name conflicts: a DIFFERENT account with the same name is refused, never merged ----
    store.hytale.profiles = [{ uuid: "99999999-8888-7777-6666-555555555555", username: "Alice" }];
    store.hytale.approved = true;
    const sC = await call(h, { method: "POST", body: { type: "authstart" } });
    const pC = await call(h, { method: "POST", body: { type: "authpoll", handle: sC.body.handle } });
    t("conflict: same name from another account -> 409, no silent takeover", pC.statusCode === 409);

    // ---- owner releases a binding (frees the name, kills the key) ----
    const rel = await call(h, { method: "DELETE", headers: { "x-player-key": own.playerKey }, query: { id: "player-" + uuidFor("Alice") } });
    t("release: owner deletes a binding", rel.statusCode === 200);
    t("release: the released key is dead", (await call(h, { method: "GET", headers: { "x-player-key": al2.playerKey } })).body.me === null);
    const relByPlayer = await call(h, { method: "DELETE", headers: { "x-player-key": own.playerKey.replace("hd_", "hd_x") }, query: { id: "player-" + uuidFor("BlakeBiz") } });
    t("release: a bad key cannot release bindings", relByPlayer.statusCode !== 200);

    // ---- multi-profile accounts: chooseFrom then authfinish ----
    store.hytale.profiles = [{ uuid: uuidFor("AltOne"), username: "AltOne" }, { uuid: uuidFor("AltTwo"), username: "AltTwo" }];
    store.hytale.approved = true;
    const sM = await call(h, { method: "POST", body: { type: "authstart" } });
    const pM = await call(h, { method: "POST", body: { type: "authpoll", handle: sM.body.handle } });
    t("multi-profile: poll offers a choice", pM.statusCode === 200 && Array.isArray(pM.body.chooseFrom) && pM.body.chooseFrom.length === 2);
    const fM = await call(h, { method: "POST", body: { type: "authfinish", handle: sM.body.handle, uuid: uuidFor("AltTwo") } });
    t("multi-profile: finish binds the chosen profile", fM.statusCode === 200 && fM.body.ign === "AltTwo");

    // ---- master key stays a break-glass owner override ----
    const h2 = loadHandler({ OWNER_IGN: "BlakeBiz", DUNGEON_WRITE_KEY: "master" }, store);
    t("master key: still acts as owner", (await call(h2, { method: "POST", headers: { "x-write-key": "master" }, body: { id: "mZ", type: "marker", kind: "chest", x: 0.5, y: 0.5 } })).statusCode === 200);
    // ---- brute force: invalid player keys throttle ----
    let lastBf = null;
    for (let i = 0; i < 16; i++) lastBf = await call(h, { method: "GET", headers: { "x-player-key": "hd_" + "A".repeat(30) + i } });
    t("brute force: guessed player keys throttle", lastBf.statusCode === 429);
  }

  // ===== auth gate still holds =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret" }, store);
    const r1 = await call(h, { method: "POST", headers: {}, body: { id: "c1", type: "marker", kind: "chest", x: 0.5, y: 0.5 } });
    t("locked map: keyless marker POST -> 403", r1.statusCode === 403);
    const r2 = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "c1", type: "marker", kind: "chest", x: 0.5, y: 0.5 } });
    t("locked map: editor marker POST -> 200 + stored in main hash", r2.statusCode === 200 && store.h(KEY).has("c1"));
    const g = await call(h, { method: "GET" });
    t("GET advertises authRequired=true when key set", g.statusCode === 200 && g.body.authRequired === true);
    t("GET keyValid=false with no key", g.body.keyValid === false);
    const gk = await call(h, { method: "GET", headers: { "x-write-key": "s3cret" } });
    t("GET keyValid=true with the right key", gk.body.keyValid === true);
    const gw = await call(h, { method: "GET", headers: { "x-write-key": "wrong" } });
    t("GET keyValid=false with a wrong key", gw.body.keyValid === false);
  }

  // ===== player runs merge (can't wipe); editor can wholesale-replace =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    // seed a real leaderboard as an editor
    await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "runs", type: "runs", best: { "r|b": { ign: "B", t: 100, c: 5, at: 1 } }, recent: [{ r: "r", ign: "B", t: 100, c: 5, at: 1 }] } });
    // a signed-in PLAYER posting an empty runs object merges — it cannot wipe anyone
    const mal = await signIn(h, store, "Mallory");
    const wipe = await call(h, { method: "POST", headers: { "x-player-key": mal.playerKey }, body: { id: "runs", type: "runs", best: {}, recent: [] } });
    const stored = JSON.parse(store.h(KEY).get("runs"));
    t("player empty-runs POST is MERGED, not wiped", wipe.statusCode === 200 && stored.best["r|b"].t === 100 && stored.recent.length === 1);
    // forging under a victim's name is now flatly refused (identity-bound writes)
    const forge = await call(h, { method: "POST", headers: { "x-player-key": mal.playerKey }, body: { id: "runs", type: "runs", best: { "r2|victim": { ign: "Victim", t: 1, c: 1, at: 2 } }, recent: [] } });
    t("forging a record under a victim IGN -> 403 (was a known keyless limitation, now closed)", forge.statusCode === 403);
    // editor CAN wholesale-replace (cleanup / restore)
    await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "runs", type: "runs", best: {}, recent: [] } });
    const s3 = JSON.parse(store.h(KEY).get("runs"));
    t("editor CAN wholesale-replace runs (cleanup path)", Object.keys(s3.best).length === 0);
  }

  // ===== per-route LIFETIME run stats: kept so nothing is lost, idempotent, editor-safe =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const pa = await signIn(h, store, "A"), pb = await signIn(h, store, "B"), pc = await signIn(h, store, "C");
    const post = (body, hdr) => call(h, { method: "POST", headers: hdr || {}, body });
    await post({ id: "runs", type: "runs", best: { "loop|a": { ign: "A", t: 300, c: 10, at: 1 } }, recent: [{ r: "loop", ign: "A", t: 300, c: 10, at: 1 }] }, { "x-player-key": pa.playerKey });
    await post({ id: "runs", type: "runs", best: { "loop|b": { ign: "B", t: 320, c: 10, at: 2 } }, recent: [{ r: "loop", ign: "B", t: 320, c: 10, at: 2 }] }, { "x-player-key": pb.playerKey });
    let s = JSON.parse(store.h(KEY).get("runs"));
    t("run stats: each fresh completion increments the route aggregate",
      s.stats.loop.n === 2 && s.stats.loop.sum === 620 && s.stats.loop.min === 300 && s.stats.loop.max === 320);
    // a re-POST of an already-seen run must NOT double-count (idempotent, tied to the recent dedup)
    await post({ id: "runs", type: "runs", best: {}, recent: [{ r: "loop", ign: "A", t: 300, c: 10, at: 1 }] }, { "x-player-key": pa.playerKey });
    s = JSON.parse(store.h(KEY).get("runs"));
    t("run stats: a re-posted (already-seen) run does not double-count", s.stats.loop.n === 2 && s.stats.loop.sum === 620);
    // a TIE (same time, different player/at) still counts -> the data is kept, not lost
    await post({ id: "runs", type: "runs", best: {}, recent: [{ r: "loop", ign: "C", t: 300, c: 10, at: 3 }] }, { "x-player-key": pc.playerKey });
    s = JSON.parse(store.h(KEY).get("runs"));
    t("run stats: a tie (same time, new run) still increments -> no data lost", s.stats.loop.n === 3 && s.stats.loop.min === 300);
    // an editor wholesale overwrite that OMITS stats preserves the lifetime aggregate (like legs)
    await post({ id: "runs", type: "runs", best: {}, recent: [] }, { "x-write-key": "s3cret" });
    s = JSON.parse(store.h(KEY).get("runs"));
    t("run stats: editor overwrite WITHOUT stats preserves the aggregate",
      s.stats && s.stats.loop && s.stats.loop.n === 3 && Object.keys(s.best).length === 0);
    // an editor that EXPLICITLY sends stats overwrites wholesale (cleanup / restore path)
    await post({ id: "runs", type: "runs", best: {}, recent: [], stats: {} }, { "x-write-key": "s3cret" });
    s = JSON.parse(store.h(KEY).get("runs"));
    t("run stats: editor explicit stats:{} clears the aggregate (cleanup path)", s.stats && Object.keys(s.stats).length === 0);
  }

  // ===== write-key brute-force guard: failed attempts throttle, valid/keyless traffic never =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret" }, store);
    let last = null;
    for (let i = 0; i < 16; i++) last = await call(h, { method: "GET", headers: { "x-write-key": "guess" + i } });
    t("bad-key GETs throttle after the failure window fills", last.statusCode === 429);
    const good = await call(h, { method: "GET", headers: { "x-write-key": "s3cret" } });
    t("the RIGHT key is never throttled by its own traffic", good.statusCode === 200 && good.body.keyValid === true);
    const anon = await call(h, { method: "GET", headers: {} });
    t("keyless reads unaffected by the guard", anon.statusCode === 200 && anon.body.keyValid === false);
  }

  // ===== Lua lint: these scripts are single-line strings — one "--" comments out the WHOLE
  // script, Redis runs nothing and returns nil, and the handler reports success without storing
  // (exactly the bug that made re-aligned pins silently revert). Guard both scripts forever. =====
  {
    const luaDefs = src.match(/const (?:UPSERT_LUA|BULK_LUA) =[^;]+;/g) || [];
    t("lua lint: both scripts found in source", luaDefs.length === 2);
    for (const def of luaDefs) {
      const name = def.match(/const (\w+)/)[1];
      const lua = (def.match(/"([^"]*)"/g) || []).map((s) => s.slice(1, -1)).join("");
      t("lua lint: " + name + " has no '--' comment (would no-op the one-line script)", !lua.includes("--"));
      t("lua lint: " + name + " actually writes (HSET present)", /HSET/.test(lua));
    }
  }

  // ===== BULK map-structure write (re-align at scale): editor-gated, validated, capped =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret" }, store);
    const mkm = (id, x) => ({ id, type: "marker", kind: "chest", x, y: 0.5, gx: 1, gy: 64, gz: 1 });
    // keyless caller can't touch bulk at all
    const noKey = await call(h, { method: "POST", headers: {}, body: { type: "bulk", entries: [mkm("b1", 0.1)] } });
    t("bulk: keyless is rejected 403", noKey.statusCode === 403 && !store.h(KEY).has("b1"));
    // editor bulk stores markers AND areas in one request
    const okRes = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: [
      mkm("b1", 0.1), mkm("b2", 0.2),
      { id: "area-thornvale", type: "area", name: "Thornvale", color: "#7c6df2", points: [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]] },
    ] } });
    t("bulk: editor stores markers + area in one request",
      okRes.statusCode === 200 && okRes.body.count === 3 && store.h(KEY).has("b1") && store.h(KEY).has("b2") && store.h(KEY).has("area-thornvale"));
    // merge-typed aggregates are NOT allowed through bulk (would bypass add-only merge semantics)
    const runsTry = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: [{ id: "runs", type: "runs", best: {}, recent: [] }] } });
    t("bulk: merge-typed entries (runs) rejected", runsTry.statusCode === 400);
    // one invalid item rejects the whole batch — nothing is half-written
    const badBatch = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: [mkm("b9", 0.3), { id: "bad", type: "marker", x: "nope", y: 0.5 }] } });
    t("bulk: one invalid entry rejects the whole batch", badBatch.statusCode === 400 && !store.h(KEY).has("b9"));
    // size limits
    const many = []; for (let i = 0; i < 101; i++) many.push(mkm("m" + i, 0.4));
    t("bulk: >100 entries rejected", (await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: many } })).statusCode === 400);
    t("bulk: empty entries rejected", (await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: [] } })).statusCode === 400);
    // duplicate ids in one request collapse to one (last wins)
    await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: [mkm("dup", 0.11), mkm("dup", 0.99)] } });
    t("bulk: duplicate ids collapse, last wins", JSON.parse(store.h(KEY).get("dup")).x === 0.99);
    // the total-entry ceiling still applies to NEW ids…
    const full = makeStore();
    const hf = loadHandler({ DUNGEON_WRITE_KEY: "s3cret" }, full);
    for (let i = 0; i < 3000; i++) full.h(KEY).set("seed" + i, "{}");
    const capHit = await call(hf, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: [mkm("newid", 0.5)] } });
    t("bulk: entry cap blocks NEW ids", capHit.statusCode === 400 && !full.h(KEY).has("newid"));
    // …but pure updates of EXISTING ids still work at the cap (that's what re-align is)
    full.h(KEY).set("exists1", JSON.stringify(mkm("exists1", 0.1)));
    const updOk = await call(hf, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { type: "bulk", entries: [mkm("exists1", 0.77)] } });
    t("bulk: existing-id updates pass at the cap", updOk.statusCode === 200 && JSON.parse(full.h(KEY).get("exists1")).x === 0.77);
  }

  // ===== player travel merges (can't wipe) =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const pl = await signIn(h, store, "Runner1");
    await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 40 } } });
    await call(h, { method: "POST", headers: { "x-player-key": pl.playerKey }, body: { id: "traveltimes", type: "travel", pairs: {} } });
    const st = JSON.parse(store.h(KEY).get("traveltimes"));
    t("player empty-travel POST does not wipe learned times", st.pairs["1,2,3|4,5,6"] === 40);
    t("anonymous travel POST -> 403", (await call(h, { method: "POST", headers: {}, body: { id: "traveltimes", type: "travel", pairs: {} } })).statusCode === 403);
  }

  // ===== a legs-UNAWARE editor build cannot wipe the shared directed legs =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const pl = await signIn(h, store, "Runner1");
    const pk = { "x-player-key": pl.playerKey };
    // a signed-in runner accumulates directed legs
    await call(h, { method: "POST", headers: pk, body: { id: "traveltimes", type: "travel",
      pairs: { "1,2,3|4,5,6": 40 }, legs: { "1,2,3>4,5,6": { t: 40, n: 3, at: 100 } } } });
    // an OLD editor build (master key, no legs field at all) flushes its travel snapshot
    await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 38 } } });
    let st = JSON.parse(store.h(KEY).get("traveltimes"));
    t("old-editor travel POST (no legs field) preserves stored directed legs",
      st.legs && st.legs["1,2,3>4,5,6"] && st.legs["1,2,3>4,5,6"].t === 40 && st.pairs["1,2,3|4,5,6"] === 38);
    // a legs-AWARE editor that explicitly sends legs still overwrites wholesale (cleanup path)
    await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "traveltimes", type: "travel", pairs: { "1,2,3|4,5,6": 38 }, legs: {} } });
    st = JSON.parse(store.h(KEY).get("traveltimes"));
    t("legs-aware editor explicit legs:{} still overwrites wholesale", st.legs && Object.keys(st.legs).length === 0);
    // and a player re-POST of the same cumulative snapshot doesn't inflate n (idempotent at the boundary)
    const snap = { id: "traveltimes", type: "travel", pairs: {}, legs: { "9,9,9>8,8,8": { t: 20, n: 4, at: 500 } } };
    await call(h, { method: "POST", headers: pk, body: snap });
    await call(h, { method: "POST", headers: pk, body: snap });
    await call(h, { method: "POST", headers: pk, body: snap });
    st = JSON.parse(store.h(KEY).get("traveltimes"));
    t("re-POSTed cumulative snapshot keeps n stable (no double-count)", st.legs["9,9,9>8,8,8"].n === 4);
  }

  // ===== opens: identity-bound; even the owner of the log can't self-wipe (merge) =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const vic = await signIn(h, store, "Victim");
    const vk = { "x-player-key": vic.playerKey };
    await call(h, { method: "POST", headers: vk, body: { id: "opens-victim", type: "opens", ign: "Victim", opens: { "1,2,3": { t: 500 }, "4,5,6": { t: 500 } } } });
    // a buggy/empty snapshot from the player's own client merges — never erases their cooldowns
    await call(h, { method: "POST", headers: vk, body: { id: "opens-victim", type: "opens", ign: "Victim", opens: {} } });
    const so = JSON.parse(store.h(KEY).get("opens-victim"));
    t("own empty opens POST cannot erase own cooldowns (merge)", so.opens["1,2,3"].t === 500 && so.opens["4,5,6"].t === 500);
    // a flood can't evict the real keys either (bounded adds per request)
    const flood = {}; for (let i = 0; i < 700; i++) flood["9," + i + ",9"] = { t: 4e12 };
    await call(h, { method: "POST", headers: vk, body: { id: "opens-victim", type: "opens", ign: "Victim", opens: flood } });
    const so2 = JSON.parse(store.h(KEY).get("opens-victim"));
    t("fresh-key flood cannot evict the real opens", so2.opens["1,2,3"] && so2.opens["4,5,6"] && Object.keys(so2.opens).length <= 2 + 32);
    // and a DIFFERENT player simply can't touch this log at all
    const mal = await signIn(h, store, "Mallory");
    t("another player writing this log -> 403",
      (await call(h, { method: "POST", headers: { "x-player-key": mal.playerKey }, body: { id: "opens-victim", type: "opens", ign: "Victim", opens: { "7,7,7": { t: 4e12 } } } })).statusCode === 403);
  }

  // ===== pending isolated + coord-deduped + server-attributed + reachable via GET =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const pa = await signIn(h, store, "Ann"), pb = await signIn(h, store, "Bob"), pc = await signIn(h, store, "Cid");
    await call(h, { method: "POST", headers: { "x-player-key": pa.playerKey }, body: { id: "junkA", type: "pending", gx: 57.2, gy: 76, gz: -65, x: 0.4, y: 0.3, by: "A" } });
    await call(h, { method: "POST", headers: { "x-player-key": pb.playerKey }, body: { id: "junkB", type: "pending", gx: 56.8, gy: 76, gz: -65, x: 0.9, y: 0.9, by: "B" } });
    const pendCount = [...store.h(KEY).keys()].filter((k) => k.startsWith("pend-")).length;
    t("pending same spot deduped to one entry", pendCount === 1 && store.h(KEY).has("pend-57_76_-65"));
    t("pending attribution is server-authoritative (claimed 'by' overridden with the signed-in ign)",
      JSON.parse(store.h(KEY).get("pend-57_76_-65")).by === "Bob");
    const g = await call(h, { method: "GET" });
    t("GET returns the deduped pending entry", !!g.body.entries["pend-57_76_-65"]);
    // removal reports: distinct id namespace, kind preserved through storage
    const rr = await call(h, { method: "POST", headers: { "x-player-key": pc.playerKey }, body: { id: "j", type: "pending", kind: "remove", gx: 57, gy: 76, gz: -65, x: 0.4, y: 0.3, by: "C" } });
    t("player removal report accepted alongside the proposal", rr.statusCode === 200 && store.h(KEY).has("pend-rm-57_76_-65"));
    const g2 = await call(h, { method: "GET" });
    t("removal report kind survives round-trip", g2.body.entries["pend-rm-57_76_-65"].kind === "remove");
  }

  // ===== areas + observed totals are editor-gated map data =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret" }, store);
    const poly = { id: "x", type: "area", name: "Thornvale", points: [[0.1, 0.1], [0.5, 0.1], [0.3, 0.6]] };
    const k1 = await call(h, { method: "POST", headers: {}, body: poly });
    t("keyless area polygon -> 403", k1.statusCode === 403 && !store.h(KEY).has("area-thornvale"));
    const e1 = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: poly });
    t("editor area polygon accepted", e1.statusCode === 200 && store.h(KEY).has("area-thornvale"));
    const at = { id: "areatotals", type: "areatotals", areas: { thornvale: { name: "Thornvale", total: 61 } } };
    const k2 = await call(h, { method: "POST", headers: {}, body: at });
    t("keyless areatotals -> 403", k2.statusCode === 403);
    const e2 = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: at });
    t("editor areatotals accepted", e2.statusCode === 200 && store.h(KEY).has("areatotals"));
  }

  // ===== contrib tally is editor-gated: crediting is a confirmation-time (editor) action =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret" }, store);
    const body = { id: "contrib", type: "contrib", by: { blakebiz: { ign: "BlakeBiz", found: 1, removed: 0 } } };
    const keyless = await call(h, { method: "POST", headers: {}, body });
    t("keyless contrib write -> 403 (no self-inflation)", keyless.statusCode === 403 && !store.h(KEY).has("contrib"));
    const editor = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body });
    t("editor contrib write accepted", editor.statusCode === 200 && store.h(KEY).has("contrib"));
  }

  // ===== keyless cap is by KEYLESS count only — editor growth never starves keyless features =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const nb = await signIn(h, store, "Newbie");
    // 2000 EDITOR markers (non-keyless) — a big map
    const km = store.h(KEY);
    for (let i = 0; i < 2000; i++) km.set("m" + i, "{}");
    // a signed-in player can STILL create their opens log (keyless count is 0, only editor content grew)
    const ro = await call(h, { method: "POST", headers: { "x-player-key": nb.playerKey }, body: { id: "opens-newbie", type: "opens", ign: "Newbie", opens: { "1,2,3": { t: 1 } } } });
    t("editor growth to 2000 does NOT block new player opens (cap counts keyless-namespaced only)", ro.statusCode === 200 && store.h(KEY).has("opens-newbie"));
  }
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const p = await signIn(h, store, "Sub");
    // 2000 keyless-namespaced entries (pend-*) — a deliberate flood
    const km = store.h(KEY);
    for (let i = 0; i < 2000; i++) km.set("pend-" + i + "_0_0", "{}");
    // editors are unaffected (total < hard cap 3000)
    const r = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "m1", type: "marker", kind: "chest", x: 0.5, y: 0.5 } });
    t("flood does NOT block editor markers (hard-cap headroom)", r.statusCode === 200 && store.h(KEY).has("m1"));
    // but a new player pending is refused — keyless-namespaced field count hit KEYLESS_SOFT
    const rp = await call(h, { method: "POST", headers: { "x-player-key": p.playerKey }, body: { id: "x", type: "pending", gx: 88888, gy: 0, gz: 0, x: 0.5, y: 0.5 } });
    t("player pending refused once keyless-namespaced count hits the soft cap", rp.statusCode === 400);
  }

  // ===== rate limit keys on a trusted IP, not the spoofable leftmost XFF =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const pa = await signIn(h, store, "A"), pb = await signIn(h, store, "B");
    let last;
    for (let i = 0; i < 31; i++) {
      // attacker rotates the LEFTMOST xff each request; the real edge IP (rightmost) is constant
      last = await call(h, { method: "POST", headers: { "x-player-key": pa.playerKey, "x-forwarded-for": (100 + i) + ".0.0.1, 8.8.8.8" }, body: { id: "opens-a", type: "opens", ign: "A", opens: { "1,2,3": { t: i + 1 } } } });
    }
    t("spoofing leftmost XFF does NOT dodge the 30/min limit (31st -> 429)", last.statusCode === 429);
    // a genuinely different client (different real IP) is not throttled
    const other = await call(h, { method: "POST", headers: { "x-player-key": pb.playerKey, "x-forwarded-for": "1.1.1.1, 9.9.9.9" }, body: { id: "opens-b", type: "opens", ign: "B", opens: { "1,2,3": { t: 1 } } } });
    t("a different real client gets its own bucket", other.statusCode === 200);
    // x-real-ip wins over xff
    const store2 = makeStore();
    const h2 = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store2);
    const pa2 = await signIn(h2, store2, "A");
    let l2;
    for (let i = 0; i < 31; i++) l2 = await call(h2, { method: "POST", headers: { "x-player-key": pa2.playerKey, "x-real-ip": "5.5.5.5", "x-forwarded-for": i + ".0.0.1" }, body: { id: "opens-a", type: "opens", ign: "A", opens: { "1,2,3": { t: i + 1 } } } });
    t("x-real-ip is the trusted bucket even if xff rotates (31st -> 429)", l2.statusCode === 429);
  }

  // ===== route ratings: identity-bound, merge-protected, keyless-capped =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const alice = await signIn(h, store, "Alice"), bob = await signIn(h, store, "Bob");
    const ra = await call(h, { method: "POST", headers: { "x-player-key": alice.playerKey }, body: { id: "rate-r1", type: "rating", route: "r1", by: { "alice": { stars: 5, comment: "great", ign: "Alice", at: 10 } } } });
    t("player rating accepted + id derived from route", ra.statusCode === 200 && store.h(KEY).has("rate-r1"));
    // you can only rate AS yourself
    t("rating under someone else's slug -> 403",
      (await call(h, { method: "POST", headers: { "x-player-key": alice.playerKey }, body: { id: "rate-r1", type: "rating", route: "r1", by: { "bob": { stars: 1, at: 1 } } } })).statusCode === 403);
    // a forged DISPLAY name in the comment is overwritten with the signed-in ign (no impersonation)
    await call(h, { method: "POST", headers: { "x-player-key": alice.playerKey }, body: { id: "rate-r1", type: "rating", route: "r1", by: { "alice": { stars: 1, comment: "scam", ign: "BlakeBiz", at: 30 } } } });
    t("rating: forged display ign is overwritten with the real signed-in name", JSON.parse(store.h(KEY).get("rate-r1")).by["alice"].ign === "Alice");
    // Bob rates the same route — must NOT wipe Alice
    await call(h, { method: "POST", headers: { "x-player-key": bob.playerKey }, body: { id: "rate-r1", type: "rating", route: "r1", by: { "bob": { stars: 3, comment: "ok", ign: "Bob", at: 20 } } } });
    const stored = JSON.parse(store.h(KEY).get("rate-r1"));
    t("second rater merges, does not erase the first", stored.by["alice"].stars === 1 && stored.by["bob"].stars === 3);
    // ratings count toward the keyless-namespaced soft cap
    const km = store.h(KEY);
    for (let i = 0; i < 2000; i++) km.set("rate-x" + i, "{}");
    const zed = await signIn(h, store, "Zed");
    const rr = await call(h, { method: "POST", headers: { "x-player-key": zed.playerKey }, body: { id: "rate-new", type: "rating", route: "new", by: { "zed": { stars: 2, at: 1 } } } });
    t("player rating refused once keyless-namespaced soft cap is hit", rr.statusCode === 400);
    // an editor can still add markers (hard-cap headroom)
    const rm = await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "m1", type: "marker", kind: "chest", x: 0.5, y: 0.5 } });
    t("editor markers unaffected by rating flood", rm.statusCode === 200);
  }

  // ===== DELETE routes to the correct hash and stays editor-gated =====
  {
    const store = makeStore();
    const h = loadHandler({ DUNGEON_WRITE_KEY: "s3cret", OWNER_IGN: "BlakeBiz" }, store);
    const p = await signIn(h, store, "Finder");
    await call(h, { method: "POST", headers: { "x-write-key": "s3cret" }, body: { id: "c1", type: "marker", kind: "chest", x: 0.5, y: 0.5 } });
    await call(h, { method: "POST", headers: { "x-player-key": p.playerKey }, body: { id: "j", type: "pending", gx: 1, gy: 2, gz: 3, x: 0.5, y: 0.5 } });
    const nokey = await call(h, { method: "DELETE", query: { id: "pend-1_2_3" }, headers: {} });
    t("DELETE without editor -> 403 (even for pending)", nokey.statusCode === 403 && store.h(KEY).has("pend-1_2_3"));
    const asPlayer = await call(h, { method: "DELETE", query: { id: "pend-1_2_3" }, headers: { "x-player-key": p.playerKey } });
    t("DELETE as a plain player -> 403", asPlayer.statusCode === 403 && store.h(KEY).has("pend-1_2_3"));
    await call(h, { method: "DELETE", query: { id: "pend-1_2_3" }, headers: { "x-write-key": "s3cret" } });
    t("editor DELETE removes the pending entry", !store.h(KEY).has("pend-1_2_3"));
    await call(h, { method: "DELETE", query: { id: "c1" }, headers: { "x-write-key": "s3cret" } });
    t("editor DELETE removes the marker", !store.h(KEY).has("c1"));
  }

  // ===== identity model FAILS CLOSED — no anonymous writes, ever, even with nothing configured =====
  {
    const store = makeStore();
    const h = loadHandler({}, store); // no DUNGEON_WRITE_KEY, no OWNER_IGN
    const r = await call(h, { method: "POST", headers: {}, body: { id: "c1", type: "marker", kind: "chest", x: 0.5, y: 0.5 } });
    t("no config: anonymous marker POST -> 403 (fail closed, never an open map)", r.statusCode === 403 && !store.h(KEY).has("c1"));
    const ro = await call(h, { method: "POST", headers: {}, body: { id: "opens-x", type: "opens", ign: "X", opens: { "1,2,3": { t: 1 } } } });
    t("no config: anonymous personal write -> 403", ro.statusCode === 403);
    const g = await call(h, { method: "GET" });
    t("GET always authRequired=true (identity always on) and me=null when unsigned", g.body.authRequired === true && g.body.me === null);
    // sign-in still works with no OWNER_IGN — the account just isn't the owner
    const p = await signIn(h, store, "Someone");
    t("sign-in works even with no OWNER_IGN configured (role player)", p.role === "player");
    t("a signed-in player can then write their own opens", (await call(h, { method: "POST", headers: { "x-player-key": p.playerKey }, body: { id: "opens-someone", type: "opens", ign: "Someone", opens: { "1,2,3": { t: 1 } } } })).statusCode === 200);
  }

  console.log("\n" + fail + " failed");
  process.exit(fail ? 1 : 0);
})();
