# Manoj Tulsani — Directives Dashboard

Auto-refreshed daily from Gmail. Deployed on Vercel.

## How it works

1. **GitHub Actions** runs `scripts/refresh.py` every morning at 07:00 UAE time
2. The script reads emails from `manoj@raynatours.com` via IMAP, sends them to Claude for analysis, and updates `data.json`
3. **Vercel** detects the new commit and redeploys in ~30 seconds
4. The dashboard reads `data.json` on page load — hit the **↻ Refresh** button to see the latest without a full page reload

## Setup

### 1. GitHub Secrets (Settings → Secrets → Actions)

| Secret | Value |
|--------|-------|
| `GMAIL_USER` | `vaibhav@raynatours.com` |
| `GMAIL_PASS` | Gmail App Password |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

### 2. Vercel

- Import the GitHub repo into Vercel
- Framework preset: **Other** (static site)
- No build command needed
- Vercel auto-deploys on every push to `main`

### 3. Google Sheet link

Edit `data.json` and set `meta.googleSheetUrl` to your Google Sheet URL. The header will show a clickable link.

### Manual refresh

Trigger the workflow manually from GitHub → Actions → "Daily Dashboard Refresh" → Run workflow.

Or run locally:
```bash
pip install anthropic
python3 scripts/refresh.py
```
