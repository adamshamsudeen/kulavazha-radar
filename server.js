// Local dev / self-host server. Serves /public and routes /api/<name> to api/<name>.js
// (same handlers Vercel runs). Zero dependencies.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));

// Minimal .env loader
try {
  for (const line of fs.readFileSync(path.join(root, ".env"), "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
} catch {}

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css", ".json": "application/json", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json" };

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (url.pathname.startsWith("/api/")) {
    const name = url.pathname.slice(5).replace(/[^a-z0-9_-]/gi, "");
    const file = path.join(root, "api", `${name}.js`);
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end("not found"); }
    const mod = await import(pathToFileURL(file).href);
    const handler = mod[req.method];
    if (!handler) { res.writeHead(405); return res.end(); }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const request = new Request(url, { method: req.method, headers: req.headers, body: ["GET", "HEAD"].includes(req.method) ? undefined : Buffer.concat(chunks) });
    try {
      const out = await handler(request);
      res.writeHead(out.status, Object.fromEntries(out.headers));
      res.end(Buffer.from(await out.arrayBuffer()));
    } catch (e) {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: String(e) }));
    }
    return;
  }
  let p = decodeURIComponent(url.pathname);
  if (p.endsWith("/")) p += "index.html";
  const file = path.join(root, "public", path.normalize(p));
  if (!file.startsWith(path.join(root, "public")) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end("not found"); }
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

const port = process.env.PORT || 3000;
server.listen(port, () => console.log(`Kulavazha Radar on http://localhost:${port}  (Sarvam ${process.env.SARVAM_API_KEY ? "LIVE" : "not configured, demo mode"})`));
