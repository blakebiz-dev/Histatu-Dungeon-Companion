// Public download endpoint for the Histatu Runner app.
//
// Looks up the latest GitHub release of this (public) repo and 302-redirects the
// caller to the asset — a stable download link for the site and the app's own
// update check, decoupled from GitHub UI/API details. The file itself never
// streams through here.
//
// Two editions ship per release (see the runner's EDITION flag):
//   full   — HistatuRunner.exe        (default: OCR + detection reports)
//   lite   — HistatuRunner-Lite.exe   (never captures/sends anything)
//
//   GET /api/download                    -> 302 to the Windows full build
//   GET /api/download?edition=lite       -> 302 to the Windows lite build
//   GET /api/download?os=linux           -> 302 to the Linux source .zip
//   GET /api/download?meta=1             -> {tag, full, lite, linux} sizes for the
//                                           get-app page and the app's own update check
//
// This repo is PUBLIC, so no token is required — GitHub's release API works
// unauthenticated. GITHUB_TOKEN is still honoured when set (higher API rate
// limits), and DOWNLOAD_REPO can point at a different owner/repo for forks.

const REPO = (process.env.DOWNLOAD_REPO || "blakebiz-dev/Histatu-Dungeon-Companion").trim();
const API = "https://api.github.com/repos/" + REPO + "/releases/latest";
let cache = { at: 0, rel: null }; // release memo per warm instance (saves API quota)

function ghHeaders(token) {
  const h = { Accept: "application/vnd.github+json", "User-Agent": "histatu-site" };
  if (token) h.Authorization = "Bearer " + token;
  return h;
}

async function latestRelease(token) {
  if (cache.rel && Date.now() - cache.at < 5 * 60 * 1000) return cache.rel;
  const r = await fetch(API, { headers: ghHeaders(token) });
  if (!r.ok) throw new Error("github " + r.status);
  const rel = await r.json();
  cache = { at: Date.now(), rel };
  return rel;
}

function pickAsset(rel, kind) {
  const assets = rel.assets || [];
  if (kind === "linux") {
    return assets.find(a => /linux|src|source/i.test(a.name) && /\.(zip|tar\.gz)$/i.test(a.name)) || null;
  }
  const exes = assets.filter(a => /\.exe$/i.test(a.name));
  if (kind === "lite") return exes.find(a => /lite/i.test(a.name)) || null;
  // full: an .exe that is not the lite build
  return exes.find(a => !/lite/i.test(a.name)) || null;
}

module.exports = async (req, res) => {
  const token = (process.env.GITHUB_TOKEN || "").trim(); // optional: raises API rate limits
  try {
    const rel = await latestRelease(token);
    const q = req.query || {};
    if (q.meta) {
      const f = pickAsset(rel, "full"), lite = pickAsset(rel, "lite");
      const lin = pickAsset(rel, "linux");
      const meta = (a) => a ? { name: a.name, size: a.size } : null;
      res.setHeader("Cache-Control", "public, max-age=300");
      return res.status(200).json({
        tag: rel.tag_name || null,
        full: meta(f),
        windows: meta(f),          // back-compat alias for older app builds
        lite: meta(lite),
        linux: meta(lin),
      });
    }
    const os = String(q.os || "").toLowerCase();
    const edition = String(q.edition || "").toLowerCase();
    let kind = "full";
    if (os === "linux") kind = "linux";
    else if (edition === "lite") kind = "lite";

    const asset = pickAsset(rel, kind);
    if (!asset) return res.status(404).json({ error: "no build for that platform/edition in the latest release yet" });
    // requesting the asset as octet-stream yields a 302 to a short-lived signed URL —
    // hand that straight to the caller
    const r2 = await fetch(asset.url, {
      redirect: "manual",
      headers: Object.assign(ghHeaders(token), { Accept: "application/octet-stream" }),
    });
    const loc = r2.headers.get("location");
    // public-repo assets may also expose a direct download URL instead of a 302
    if (!loc && asset.browser_download_url) { res.setHeader("Cache-Control", "no-store"); res.setHeader("Location", asset.browser_download_url); return res.status(302).end(); }
    if (!loc) return res.status(502).json({ error: "github did not return a download link" });
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Location", loc);
    return res.status(302).end();
  } catch (e) {
    return res.status(502).json({ error: "release lookup failed" });
  }
};
