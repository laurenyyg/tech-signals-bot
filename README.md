# Tech Signals Bot

A personal intelligence bot that scrapes curated tech and crypto sources, analyses them with Claude, and posts dense signal digests to Slack.

---

## Profiles

### `--profile what`
**What happened in tech this week?**

Scrapes Techmeme, CoinDesk, TechCrunch, OpenAI News, and 机器之心 (Jiqizhixin) via RSS, plus Anthropic news and research by scraping their SSR page HTML (the Sanity CMS public API is no longer accessible without auth). Scores items against a crypto/AI/infrastructure keyword whitelist, **deduplicates** overlapping stories across feeds (normalized URL → normalized title → semantic dedup via Claude Haiku for cross-language duplicates), keeps the top 40, and asks Claude to extract paradigm-shifting signals. Posts a structured digest with macro event, ELI5 explanation, and second-order investment implications. After each run, per-source performance stats (articles in window, LLM-relevant count, ratio) are appended to a Google Spreadsheet.

**Schedule:** Sundays at 8:47 PM Vancouver time.

### `--profile why`
**Why does it matter?**

Scrapes long-form essays and presentations from:
- **Stratechery** (Ben Thompson) — private subscriber RSS feed (articles + podcast with transcripts, deduplicated by title)
- **DeepLearning.AI** (Andrew Ng) — Letters from Andrew Ng
- **Ben-Evans** (Benedict Evans) — Essays and bi-annual presentations
- **a16z** — Newsletter essays via Substack (investment announcements excluded)
- **One Useful Thing** (Ethan Mollick) — Public RSS feed

Filters to the last 7 days, flags deep reads against a keyword list, and asks Claude to summarise each piece as bullet points with core thesis and second-order implications.

**Schedule:** Fridays at 4:37 PM Vancouver time.

---

## Setup

**1. Clone and create a virtual environment**
```bash
git clone https://github.com/laurengyu/ly-tech-signals-bot.git
cd ly-tech-signals-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**2. Configure environment variables**

Create a `.env` file in the project root:
```
ANTHROPIC_API_KEY=your_key_here
WHAT_WEBHOOK_URL=your_slack_webhook_here
WHY_WEBHOOK_URL=your_slack_webhook_here
STRATECHERY_RSS_URL=your_private_article_rss_url_here
STRATECHERY_PODCAST_URL=your_private_podcast_rss_url_here
DATAIMPULSE_PROXY_URL=http://username:password@gw.dataimpulse.com:823
GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here
GOOGLE_SERVICE_ACCOUNT_PATH=information-source-digest-0a187c6dd85d.json
```

`STRATECHERY_RSS_URL` and `STRATECHERY_PODCAST_URL` are private subscriber URLs from Stratechery (both required for the Why profile). Find them in your Stratechery account settings.
`DATAIMPULSE_PROXY_URL` is optional. If omitted, all requests fall back to a direct connection.
`GOOGLE_SPREADSHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_PATH` are required for the What profile's performance tracking. Place the Google Cloud service account JSON in the project root and add it to `.gitignore`.

**3. Run locally**
```bash
venv/bin/python main.py --profile what
venv/bin/python main.py --profile why
```

---

## GitHub Actions

Both pipelines run automatically via GitHub Actions. Add the following secrets to your repository (`Settings → Secrets → Actions`):

| Secret | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `WHAT_WEBHOOK_URL` | Slack webhook for the What digest |
| `WHY_WEBHOOK_URL` | Slack webhook for the Why digest |
| `STRATECHERY_RSS_URL` | Private Stratechery article RSS URL |
| `STRATECHERY_PODCAST_URL` | Private Stratechery podcast RSS URL |
| `DATAIMPULSE_PROXY_URL` | Residential proxy URL (optional) |
| `GOOGLE_SPREADSHEET_ID` | Spreadsheet ID for What profile performance tracking |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the Google Cloud service account JSON file |

---

## Architecture

```
main.py                  Entry point, argument routing, Anthropic call, Slack post
├── fetchers/
│   ├── base_fetcher.py  Abstract base class
│   ├── rss_fetcher.py   What profile — RSS feeds + Anthropic SSR scraper, keyword scoring, cross-feed dedupe
│   └── why_fetcher.py   Why profile — Stratechery RSS, DL.AI + Ben-Evans HTML scrapers, a16z Substack API, One Useful Thing RSS
├── sheets_writer.py     What profile — appends per-source performance stats to Google Sheets after each run
├── prompts.py           System prompts for each profile
├── models.py            Dataclasses (NewsSignal, WhySignal)
└── config.py            DEEP_READ_KEYWORDS, SUMMARY_TRUNCATE_CHARS
```

### Pre-LLM pipeline (Why profile)

1. Scrape all 5 sources → raw item dicts
2. Drop items outside the 7-day window
3. Flag `needs_deep_read` via keyword scan
4. Serialise to numbered plaintext → user message
5. Send to Claude with `WHY_PROFILE_PROMPT`

### Slack format (Why profile)

| Prefix | Meaning |
|---|---|
| ❗️ | Deep read flagged |
| 🧠 | Standard essay |
| 🗂 | Presentation |
| 🎙 | Podcast episode |

---

## Tests

```bash
venv/bin/python -m pytest tests/
```
