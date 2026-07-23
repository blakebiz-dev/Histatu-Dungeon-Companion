// Tests for api/download.js — the release proxy that lets users download the app
// while the source repo stays private.  Run:  node api/__tests__/download.test.js
const fs = require("fs");
const nodePath = require("path");
const src = fs.readFileSync(nodePath.join(__dirname, "..", "download.js"), "utf8");

let fail = 0;
const t = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + " " + name); if (!cond) fail++; };

const RELEASE = {
  tag_name: "v1.0.4",
  assets: [
    { name: "HistatuRunner.exe", size: 21700000, url: "https://api.github.com/assets/1" },
    { name: "HistatuRunner-linux-src.zip", size: 90000, url: "https://api.github.com/assets/2" },
  ],
};

function makeFetch(release, opts) {
  opts = opts || {};
  const calls = [];
  async function fakeFetch(url, init) {
    calls.push({ url, init });
    if (url.includes("/releases/latest")) {
      if (opts.apiFails) return { ok: false, status: 500 };
      return { ok: true, json: async () => release };
    }
    // asset fetch -> signed redirect
    return { headers: { get: (k) => (k.toLowerCase() === "location" ? "https://signed.example/" + url.split("/").pop() : null) } };
  }
  fakeFetch.calls = calls;
  return fakeFetch;
}

function loadHandler(env, fetchImpl) {
  const mod = { exports: {} };
  const fakeProcess = { env: env };
  new Function("module", "exports", "require", "process", "fetch", src)(mod, mod.exports, require, fakeProcess, fetchImpl);
  return mod.exports;
}

function mkRes() {
  const res = { statusCode: 0, body: null, headers: {}, ended: false };
  res.setHeader = (k, v) => { res.headers[k] = v; };
  res.status = (code) => {
    res.statusCode = code;
    return { json: (obj) => { res.body = obj; return res; }, end: () => { res.ended = true; return res; } };
  };
  return res;
}

(async () => {
  // public repo: works with NO token — unauthenticated GitHub API, no Authorization header sent
  {
    const f = makeFetch(RELEASE);
    const h = loadHandler({}, f);
    const res = mkRes();
    await h({ query: {} }, res);
    t("no token -> download still redirects (public repo)", res.statusCode === 302 && res.headers.Location === "https://signed.example/1");
    t("no token -> no Authorization header sent", f.calls.every((c) => !(c.init && c.init.headers && c.init.headers.Authorization)));
  }
  // DOWNLOAD_REPO env overrides the release source (fork support)
  {
    const f = makeFetch(RELEASE);
    const h = loadHandler({ DOWNLOAD_REPO: "someone/their-fork" }, f);
    await h({ query: { meta: "1" } }, mkRes());
    t("DOWNLOAD_REPO overrides the repo slug", f.calls[0].url.includes("/repos/someone/their-fork/"));
  }
  // token (when set) is attached — higher rate limits
  {
    const f = makeFetch(RELEASE);
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, f);
    await h({ query: { meta: "1" } }, mkRes());
    t("token (when set) is sent to GitHub", f.calls[0].init.headers.Authorization === "Bearer tok");
  }
  // meta returns tag + the single exe (full/windows/lite all alias it) + linux
  {
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, makeFetch(RELEASE));
    const res = mkRes();
    await h({ query: { meta: "1" }, headers: {} }, res);
    t("meta: tag + single-exe name/size + linux", res.statusCode === 200 && res.body.tag === "v1.0.4"
      && res.body.full.name === "HistatuRunner.exe" && res.body.linux.size === 90000);
    t("meta: windows + legacy lite fields both alias the single exe",
      res.body.windows.name === "HistatuRunner.exe" && res.body.lite.name === "HistatuRunner.exe");
    t("meta: no editor key in the payload", !("editor" in res.body));
  }
  // full (default) -> 302 to the signed exe URL, NOT lite
  {
    const f = makeFetch(RELEASE);
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, f);
    const res = mkRes();
    await h({ query: {}, headers: {} }, res);
    t("default -> 302 signed FULL exe", res.statusCode === 302 && res.headers.Location === "https://signed.example/1" && res.ended);
    const assetCall = f.calls.find(c => c.url.endsWith("/assets/1"));
    t("asset fetched as octet-stream, no auto-follow", assetCall
      && assetCall.init.redirect === "manual" && assetCall.init.headers.Accept === "application/octet-stream");
  }
  // legacy ?edition=lite (old installs' update check) -> the single current exe
  {
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, makeFetch(RELEASE));
    const res = mkRes();
    await h({ query: { edition: "lite" }, headers: {} }, res);
    t("legacy edition=lite -> 302 signed single exe", res.statusCode === 302 && res.headers.Location === "https://signed.example/1");
  }
  // legacy ?edition=editor now falls through to the FULL build (seamless migration for old Editor.exe users)
  {
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, makeFetch(RELEASE));
    const res = mkRes();
    await h({ query: { edition: "editor" }, headers: {} }, res);
    t("legacy edition=editor -> 302 signed FULL exe (no gate)", res.statusCode === 302 && res.headers.Location === "https://signed.example/1");
  }
  // linux -> the zip asset
  {
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, makeFetch(RELEASE));
    const res = mkRes();
    await h({ query: { os: "linux" } }, res);
    t("os=linux -> 302 signed zip", res.statusCode === 302 && res.headers.Location === "https://signed.example/2");
  }
  // release without a linux asset -> 404 for linux, meta reports null
  {
    const rel = { tag_name: "v1.0.4", assets: [RELEASE.assets[0]] };
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, makeFetch(rel));
    const res = mkRes();
    await h({ query: { os: "linux" } }, res);
    t("missing linux asset -> 404", res.statusCode === 404);
    const res2 = mkRes();
    await h({ query: { meta: "1" }, headers: {} }, res2);
    t("meta reports missing linux as null", res2.body.linux === null && res2.body.full !== null);
  }
  // GitHub API failure -> 502, token never surfaces
  {
    const h = loadHandler({ GITHUB_TOKEN: "tok" }, makeFetch(RELEASE, { apiFails: true }));
    const res = mkRes();
    await h({ query: {}, headers: {} }, res);
    t("github failure -> 502 without detail", res.statusCode === 502 && !JSON.stringify(res.body).includes("tok"));
  }

  console.log("\n" + fail + " failed");
  process.exit(fail ? 1 : 0);
})();
