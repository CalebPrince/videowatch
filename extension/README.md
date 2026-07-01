# VideoWatch Browser Extension

One-click add any site you're browsing to your VideoWatch monitor.

## Install (Developer mode)

1. Open Chrome/Edge and go to `chrome://extensions`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked** and select this `extension/` folder
4. The VideoWatch icon will appear in your toolbar

## Setup

1. Click the extension icon on any page
2. Enter your **VideoWatch Server URL** (e.g. `https://videowatch.duckdns.org`)
3. Paste your **API Token** — generate one in VideoWatch › Settings › My Account › API Tokens
4. Click **Save credentials**

## Usage

Navigate to any site you want to monitor, click the extension icon, optionally set a name and scan interval, then click **Add to VideoWatch**. The site will appear in your dashboard immediately and be scanned on the next scheduler tick.

## Icons

Place 16×16, 48×48, and 128×128 PNG icons named `icon16.png`, `icon48.png`, `icon128.png` in the `icons/` subfolder. The extension will work without them but will show a default puzzle-piece icon in the toolbar.
