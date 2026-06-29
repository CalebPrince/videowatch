const http = require('http');
const { URL } = require('url');

// Simple proxy that performs a HEAD request to the target video URL
// and returns a JSON payload indicating success.
// Run with: node proxy/index.js (requires Node 18+ for built‑in fetch)

const server = http.createServer(async (req, res) => {
  // Enable CORS for all origins (required for browser fetches)
  res.setHeader('Access-Control-Allow-Origin', '*');
  // Handle preflight OPTIONS request
  if (req.method === 'OPTIONS') {
    res.setHeader('Access-Control-Allow-Methods', 'GET,HEAD,POST,OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    res.writeHead(204);
    res.end();
    return;
  }

  const reqUrl = new URL(req.url, `http://${req.headers.host}`);
  // Route handling
  if (reqUrl.pathname === '/check') {
    const target = reqUrl.searchParams.get('url');
    if (!target) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Missing url query parameter' }));
      return;
    }
    try {
      // Perform a HEAD request to the video URL.
      const response = await fetch(target, { method: 'HEAD' });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: response.ok, status: response.status }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  if (reqUrl.pathname === '/thumb') {
    const imgUrl = reqUrl.searchParams.get('url');
    if (!imgUrl) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Missing url query parameter' }));
      return;
    }
    try {
      const imgResp = await fetch(imgUrl);
      if (!imgResp.ok) {
        res.writeHead(imgResp.status, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Failed to fetch image' }));
        return;
      }
      const contentType = imgResp.headers.get('content-type') || 'application/octet-stream';
      const buffer = await imgResp.arrayBuffer();
      res.writeHead(200, { 'Content-Type': contentType });
      res.end(Buffer.from(buffer));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  // Fallback for unknown routes
  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('Not found');
});

const PORT = 3000;
server.listen(PORT, () => {
  console.log(`Video‑check proxy listening on http://localhost:${PORT}`);
});
