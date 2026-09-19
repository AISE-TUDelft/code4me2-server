// Isolated browser-test host for the Code4Me research UI.
// Serves the built SPA from code4me2-server/src/website/build and proxies /api
// to the disposable e2e backend (localhost:28008), so the browser is strictly
// same-origin and no dev stack or repo file is touched.
const http = require("http");
const fs = require("fs");
const path = require("path");

const BUILD = path.resolve(process.env.CODE4ME_E2E_WEB_BUILD || path.join(__dirname, "../code4me2-server/src/website/build"));
const BACKEND = new URL(process.env.CODE4ME_E2E_BASE_URL || "http://127.0.0.1:28008");
const PORT = Number(process.env.CODE4ME_E2E_WEB_PORT || 3900);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".map": "application/json",
};

function serveFile(res, filePath) {
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("not found");
      return;
    }
    res.writeHead(200, { "Content-Type": TYPES[path.extname(filePath)] || "application/octet-stream" });
    res.end(data);
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  if (url.pathname.startsWith("/api/") || url.pathname === "/api") {
    const proxied = http.request(
      {
        host: BACKEND.hostname,
        port: BACKEND.port,
        method: req.method,
        path: req.url,
        headers: { ...req.headers, host: BACKEND.host },
      },
      (upstream) => {
        res.writeHead(upstream.statusCode || 502, upstream.headers);
        upstream.pipe(res);
      }
    );
    proxied.on("error", () => {
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end('{"detail":"proxy upstream unavailable"}');
    });
    req.pipe(proxied);
    return;
  }

  const candidate = path.join(BUILD, url.pathname);
  if (url.pathname !== "/" && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
    serveFile(res, candidate);
    return;
  }
  // SPA fallback for client-side routes such as /research/join.
  serveFile(res, path.join(BUILD, "index.html"));
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`research-ui-test-host listening on http://127.0.0.1:${PORT} -> backend ${BACKEND.origin}`);
});
