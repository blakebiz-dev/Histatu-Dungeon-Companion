// Public download endpoint for the Histatu Runner app.
//
// Looks up the latest GitHub release of this (public) repo and 302-redirects the
// caller to the asset — a stable download link for the site and the app's own
// update check, decoupled from GitHub UI/API details. The file itself never
// streams through here.
//
// One Windows build ships per release: HistatuRunner.exe.
//
//   GET /api/download                    -> 302 to the Windows build
//   GET /api/download?os=linux           -> 302 to the Linux source .zip
//   GET /api/download?meta=1             -> {tag, full, windows, linux} sizes for the
//                                           get-app page and the app's own update check
//
// The legacy ?edition= param (and the `lite` meta field) are still accepted so older
// installed builds' update checks keep resolving to the single current exe.
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
  // one Windows exe now; any edition request (incl. legacy ?edition=lite from old installs)
  // resolves to it. Prefer a non-lite-named exe if an old release still has both.
  const exes = assets.filter(a => /\.exe$/i.test(a.name));
  return exes.find(a => !/lite/i.test(a.name)) || exes[0] || null;
}

module.exports = async (req, res) => {
  const token = (process.env.GITHUB_TOKEN || "").trim(); // optional: raises API rate limits
  try {
    const rel = await latestRelease(token);
    const q = req.query || {};
    if (q.meta) {
      const f = pickAsset(rel, "full"), lin = pickAsset(rel, "linux");
      const meta = (a) => a ? { name: a.name, size: a.size } : null;
      res.setHeader("Cache-Control", "public, max-age=300");
      return res.status(200).json({
        tag: rel.tag_name || null,
        full: meta(f),
        windows: meta(f),          // back-compat alias for older app builds
        lite: meta(f),             // legacy field: same single exe (older lite installs read this)
        linux: meta(lin),
      });
    }
    const os = String(q.os || "").toLowerCase();
    let kind = "full";             // ?edition= is legacy-accepted but always the single exe now
    if (os === "linux") kind = "linux";

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
