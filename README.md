# VideoWatch

A self-hosted web application that monitors websites for new videos and shows them in a live dashboard.

## Features

- **Real web scraping** — fetches pages server-side, bypassing CORS entirely
- Detects YouTube, Vimeo, Twitch, Dailymotion embeds and direct `.mp4`/`.webm` links
- Scans `<video>`, `<iframe>`, `<a>`, `data-*` attributes, and inline `<script>` tags
- "New" badge on freshly discovered videos
- Auto-scans every 5 minutes
- Persistent SQLite database — no setup required
- Clean Bootstrap 5 dashboard with in-app notification alerts

## Requirements

- Python 3.10+

## Setup

```bash
# 1. Clone / copy the project folder
cd videowatch

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Start the backend
python server.py
# or
uvicorn server:app --reload --port 8000

# Alternative startup scripts
On Unix/macOS:
```bash
./run.sh
```
On Windows PowerShell:
```powershell
.\run.ps1
```
```

Note: Playwright requires separate browser binaries. After installing Python
dependencies run:

```bash
python -m playwright install
```

Also consider running the server from the project root; static files are
served from the `static/` folder to avoid exposing repository files.

## Docker

A Docker environment is included for easy deployment.

Build and start the app with:

```bash
docker build -t videowatch .
docker run --rm -p 8000:8000 -v "$PWD/cookies:/app/cookies" \
  -v "$PWD/thumbcache:/app/thumbcache" \
  -v "$PWD/videowatch.db:/app/videowatch.db" \
  -e HOST=0.0.0.0 -e PORT=8000 videowatch
```

Or use Docker Compose:

```bash
docker compose up --build
```

## Tests

Run the basic API test suite with:

```bash
python -m pytest -q
```

The tests validate the root endpoint and the new `/api/health` route.

## Open the app

Visit **http://localhost:8000** in your browser.

The frontend (`static/index.html`) is served directly by the backend.

## How it works

```
Browser → FastAPI (port 8000)
              ├── GET  /              → serves index.html
              ├── POST /api/sites     → add a website to monitor
              ├── GET  /api/videos    → list all found videos
              ├── POST /api/scan      → trigger a scan of all sites
              └── GET  /api/stats     → dashboard stats
```

When a scan runs:
1. The backend fetches the page HTML using a real browser User-Agent (bypasses CORS / same-origin policy)
2. BeautifulSoup parses `<video>`, `<iframe>`, `<a>`, `data-*` attrs, and `<script>` bodies
3. Detected URLs are matched against YouTube, Vimeo, Twitch, Dailymotion patterns, or direct video file extensions
4. New videos are stored in `videowatch.db` (SQLite) with `is_new = 1`
5. The frontend polls the API and highlights new items

## Project structure

```
videowatch/
├── server.py            ← FastAPI backend + scraper
├── static/
│   └── index.html       ← Frontend dashboard
├── requirements.txt
└── videowatch.db        ← Created automatically on first run
```

## Limitations & tips

- Some sites use JavaScript to load videos dynamically. For those, consider adding [Playwright](https://playwright.dev/python/) to render JS before scraping:
  ```python
  from playwright.async_api import async_playwright
  async with async_playwright() as p:
      browser = await p.chromium.launch()
      page = await browser.new_page()
      await page.goto(url)
      html = await page.content()
  ```
- Sites behind login walls require session cookies — add them to `BROWSER_HEADERS` in `server.py`.
- To run on a server and access from other devices, set `HOST=0.0.0.0` (for example via an environment variable) before starting `server.py`.
