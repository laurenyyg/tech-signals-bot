# Tech Signals Bot

A personal intelligence system that scrapes curated tech and AI sources, analyses them with Claude, and posts dense signal digests to Slack. Three pipelines answer three distinct questions: **What** happened, **Why** it matters, and **How** people are actually using AI tools.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    WHAT  (weekly)                        │
│  RSS feeds + Anthropic scrape                           │
│  → keyword scoring → 3-pass dedup → top 40             │
│  → Sonnet: cluster paradigm-shifting signals → Slack    │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                    WHY  (weekly)                         │
│  Stratechery / DL.AI / Ben-Evans / a16z / One Useful    │
│  → 7-day window → deep-read flag                        │
│  → Sonnet: dense bullet summaries → Slack               │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                    HOW  (decoupled)                      │
│                                                          │
│  Daily Catcher (catch_how.py)                           │
│    ChatPRD + SSPAI → dedupe vs backlog + seen ledger    │
│    → fetch full article body → data/how_backlog.json    │
│                         │                               │
│  Weekly Synthesizer (main.py --profile how)             │
│    Haiku bouncer on full body: PASS / NULL              │
│    → one batched Sonnet call → HowSignal JSON           │
│    → Slack → clear backlog, record URLs as seen         │
└─────────────────────────────────────────────────────────┘
```

---

## The Three Pipelines

### `--profile what` — What happened in tech?

**Sources**: Techmeme, TechCrunch, OpenAI News via RSS; Anthropic news and research via SSR HTML scraping.

**Pre-LLM pipeline**:
1. Fetch all feeds → raw items. A source that errors, returns no entries, whose entries all fail to parse, or whose Anthropic SSR page has no posts is reported as a ⚠️ line instead of being silently skipped.
2. Drop items outside the fetch window. The floor is 8 days (one day of slack for late scheduled runs). After a missed run the window stretches back to the last successful post minus one day, capped at 14 days. An empty ledger uses the 8-day floor. Items without a parsable date are skipped.
3. Three-pass cross-feed deduplication:
   - Pass 1: Normalised URL (strips `utm_*`, `fbclid`, `gclid` params)
   - Pass 2: Normalised title (strips ` | TechCrunch`, ` - Techmeme`-style suffixes)
   - Pass 3: Semantic dedup via `claude-haiku-4-5-20251001` — groups titles covering the same event (e.g. Techmeme vs TechCrunch on the same announcement). Invalid or partial index covers are ignored; the pre-semantic list is kept.
4. Keyword score against `KEYWORD_WHITELIST` (ASCII whole-word match). Drop zero-score items.
5. Drop URLs already posted (`data/what_seen.json`). Keep top 40 of the rest. An empty week still posts the digest (funnel + source errors, no Sonnet call). The ledger is updated only after a successful Slack post and committed by the workflow.

**LLM call**: Title + stripped summary only (no URLs). Claude clusters related items and extracts paradigm-shifting signals: macro event, core fact, ELI5 mechanism, and second-order infrastructure implications. Numeric `source_ids` are resolved to URLs after the call; unknown ids are dropped.

**Post-run**: After Slack succeeds, per-source sieve stats (keyword-relevant, fetched, ratio) are appended to a Google Spreadsheet. A Sheets failure is logged and does not undo the post. The digest funnel links each source to its homepage; the FinOps footer links the spreadsheet.

**Schedule**: Sundays at 22:47 UTC (2:47 PM PST / 3:47 PM PDT).

---

### `--profile why` — Why does it matter?

**Sources**: Stratechery (Ben Thompson) via private subscriber RSS (full-text articles; podcast entries carry only a short episode description, not a transcript), DeepLearning.AI (Andrew Ng letters), Ben-Evans (essays + presentations), a16z newsletter (announcements excluded: `Fundraise`-tagged posts, "Investing in / Welcome / Meet the / Announcing / Introducing …" titles, and titles mentioning a16z), One Useful Thing (Ethan Mollick).

**Pre-LLM pipeline**:
1. Fetch all sources → raw items. A source that errors, returns nothing, or whose entries all fail to parse is reported as a ⚠️ line in the digest instead of being silently skipped.
2. Drop items outside the fetch window. The floor is 8 days (one day of slack for late scheduled runs). After a missed run the window stretches back to the last successful post minus one day, capped at 30 days, so last week's items are recovered instead of ageing out. An empty ledger uses the 8-day floor. Items without a parsable date are skipped. Date-only values (e.g. DeepLearning.AI's `Nov 6, 2026`) count as the whole day, so items published on the cutoff day aren't dropped.
3. Stratechery deduplication: articles win over podcasts on same title
4. Drop items already posted in a previous week (`data/why_seen.json`). This lets the 8-day window overlap the weekly cadence, and lets month-only dates ("Nov 2026") stay in window all month, without repeats. The ledger is updated only after a successful Slack post and committed by the workflow; Stratechery access tokens are stripped from its keys.
5. Fetch full article text for sources whose feeds only give excerpts (a16z, Ben Evans, DeepLearning.AI). Stratechery and One Useful Thing already carry full text in RSS and are never fetched.

**LLM calls**: One Sonnet call per item (8,000 output tokens each) with title, author, content type, and text (truncated to 12,000 chars). Claude returns only the summary bullets and `is_deep_read`; metadata comes from the fetcher. An item that fails is listed as "Not summarised" and the rest of the digest still posts.

**Slack format**:

| Prefix | Meaning |
|---|---|
| ❗️ | Deep read (LLM-flagged) |
| 🧠 | Standard essay |
| 🗂 | Presentation |
| 🎙 | Podcast |

Each digest ends with ⚠️ lines for failed sources and unsummarised items, a funnel line per feed, and the FinOps footer:

```
📡 a16z   25 fetched › 7 in window › 2 new › 2 summarised  (5 announcements skipped)
📡 stratechery   50 fetched › 2 in window › 0 new › 0 summarised  (2 already posted)
📡 quiet: ben-evans-essays, ben-evans-presentations, one-useful-thing  (26 fetched, 0 in window)
```
Source names in the funnel are Slack links to the publication homepage.

**Schedule**: Fridays at 4:37 PM Vancouver time.

---

### `--profile how` — How are people using AI tools?

A decoupled two-stage pipeline designed around the **Daily Catcher / Weekly Synthesizer** pattern (see below).

**Sources**: ChatPRD "How I AI" — each new episode's linked workflow pages, parsed from their schema.org `HowTo` JSON-LD (one backlog item per workflow); SSPAI (RSS, filtered by author whitelist and keywords; Latin keywords like `AI` match whole words only). For each new item the catcher downloads the page and stores the extracted article text, so the LLM reads the full piece rather than the feed blurb.

**Schedule**: Catcher runs daily at 11:03 UTC. Synthesizer runs Saturdays at 9:27 AM PDT.

---

## Long digests (all profiles)

Slack caps a message at 50 blocks and each text block at 3,000 characters. Every profile builds its digest as a list of cards and hands it to `split_into_messages()` in `common.py`: a card is never split, but a long digest continues in further messages marked `(1/N)`, `(2/N)`, …, with the funnel and FinOps footer on the last one. If a later message fails to post, ledgers and backlogs are left unchanged, so the next run may re-post the earlier parts.

---

## FinOps & Cascade Pattern (How Pipeline)

The How pipeline uses a two-model cascade to minimise Sonnet token spend. Most articles in the backlog are lifestyle/opinion pieces that contain no replicable workflow — running them through Sonnet directly would be wasteful.

```
Backlog item (title + body extracted by the catcher via trafilatura)
    │
    ▼
Stage 1 — Haiku Bouncer       ← claude-haiku-4-5-20251001, max 1024 tokens, one call per item
  First line: PASS or NULL.
  PASS → tool stack, steps, outcome as plain text.
    │
    ├─ NULL ──► discard
    │
    └─ PASS ──► collect
                    ▲
ChatPRD HowTo items ┘  ← already structured workflows; skip the bouncer
          │
          ▼
Stage 2 — Sonnet Formatter    ← claude-sonnet-4-6, max 16,000 tokens, ONE batched call
  Items referenced by numeric ID; returns
  source_id, tech_stack, workflow_steps, roi_impact.
  Title / source / URL resolved from metadata.
          │
          ▼
       Slack post
```

**Failures are loud.** If every Stage 1 call errors (e.g. a retired model ID), or Stage 2 fails, the run posts a 🚨 alert instead of an empty digest. Individual Stage 1 errors appear in the funnel line, and those items stay in the backlog for up to 3 runs.

**Every Slack message ends with a per-source funnel and a FinOps footer** aggregating all Haiku + Sonnet calls in the run (budget = sum of per-call output limits):

```
📡 ChatPRD   4 caught  ›  1 passed bouncer  ›  1 workflow(s)
_Model: claude-haiku-4-5-20251001 + claude-sonnet-4-6 | Tokens: 16934/21120 | Budget: OK_
```

The Haiku stage acts as a cheap cognitive filter — only articles that survive it consume Sonnet tokens. The footer lists only the models actually called.

---

## Decoupled Data Engineering (How Pipeline)

The How pipeline separates **data collection** from **data processing** to avoid two failure modes:

**Problem 1 — Data decay**: AI workflow articles are time-sensitive. A weekly fetch-and-synthesise approach would miss articles that fall outside a narrow scraping window on synthesis day.

**Problem 2 — LLM cost on empty runs**: Running the Sonnet formatter daily when most days have zero new workflow articles wastes money.

**Solution — Daily Catcher / Weekly Synthesizer**:

| Component | File | Runs | Does |
|---|---|---|---|
| Daily Catcher | `catch_how.py` | Daily (GitHub Actions) | Fetches the last 3 days of ChatPRD + SSPAI, skips URLs in the backlog or seen ledger, stores each new article's extracted body in `data/how_backlog.json`, commits to repo. No LLM calls. A failing source posts a 🚨 alert. |
| Weekly Synthesizer | `main.py --profile how` | Saturdays | Reads the accumulated backlog, runs the Haiku→Sonnet cascade, posts to Slack, and **only if the Slack post succeeds**, clears the backlog and records processed URLs in `data/how_seen.json`. |

The backlog is a plain JSON file committed to the repo by the catcher workflow. If the synthesizer fails (API error, Slack outage), the backlog is left untouched — all articles are retried next Saturday. No data is lost.

The **seen ledger** (`data/how_seen.json`) stops articles from re-entering the backlog after the weekly reset (the 3-day fetch window overlaps the reset). Entries older than 60 days are pruned on every write, so the file stays small.

For a one-off backfill (e.g. after an outage), run `venv/bin/python catch_how.py --days 14`, or trigger the **How Pipeline - Daily Catcher** workflow manually with `days` set to `14`.

---

## File Structure

```
main.py                   Entry point — --profile what|why|how
common.py                 Shared helpers — Anthropic call, Slack post/footer, HTML extraction
why_pipeline.py           Why per-item summariser + Slack digest
how_pipeline.py           How weekly synthesizer + backlog state
seen_ledger.py            JSON state helpers + seen-URL ledger (How and Why), URL canonicalisation
catch_how.py              How daily catcher (no LLM)
config.py                 Shared constants (keywords, author lists, truncation)
models.py                 Dataclasses: WhySignal, HowSignal
prompts.py                LLM system prompts for all three profiles
sheets_writer.py          What profile — Google Sheets source tracking

fetchers/
  base_fetcher.py         Abstract base class
  rss_fetcher.py          What sources + 3-pass dedup + keyword scoring
  why_fetcher.py          Why sources — Stratechery, DL.AI, Ben-Evans, a16z, OUT
  chatprd_fetcher.py      How source — ChatPRD JSON-LD scraper
  sspai_fetcher.py        How source — SSPAI RSS with author/keyword filter

data/
  how_backlog.json        Persistent How backlog (committed by catcher CI)
  how_seen.json           How seen-URL ledger, pruned to 60 days
  why_seen.json           Why posted-item ledger (committed by Why CI), pruned to 60 days

.github/workflows/
  what_pipeline.yml       Weekly What pipeline
  why_pipeline.yml        Weekly Why pipeline
  how_catcher.yml         Daily How catcher + backlog commit
  how_synthesizer.yml     Weekly How synthesizer + backlog reset

tests/
  test_rss_fetcher.py     What dedupe unit tests (mocked XML, no live HTTP)
  test_how.py             How parsers, filters, ledger, Slack payload (mocked, no live HTTP)
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/laurengyu/ly-tech-signals-bot.git
cd ly-tech-signals-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. Configure `.env`

Create a `.env` file in the project root:

```env
# Required — all profiles
ANTHROPIC_API_KEY=sk-ant-...

# Required — Slack webhooks per profile
WHAT_WEBHOOK_URL=https://hooks.slack.com/...
WHY_WEBHOOK_URL=https://hooks.slack.com/...
HOW_WEBHOOK_URL=https://hooks.slack.com/...

# Required — Why profile (private Stratechery subscriber URLs)
STRATECHERY_RSS_URL=https://stratechery.com/feed/...
STRATECHERY_PODCAST_URL=https://stratechery.com/feed/...

# Required — What profile (Google Sheets source tracking)
GOOGLE_SPREADSHEET_ID=1abc...
GOOGLE_SERVICE_ACCOUNT_PATH=information-source-digest-0a187c6dd85d.json

# Optional — residential proxy (falls back to direct if unset)
DATAIMPULSE_PROXY_URL=http://username:password@gw.dataimpulse.com:823
```

Place the Google Cloud service account JSON file in the project root and add it to `.gitignore`.

### 3. Run locally

```bash
venv/bin/python main.py --profile what
venv/bin/python main.py --profile why
venv/bin/python main.py --profile how

# Run the How catcher manually (no LLM, just fetch + append to backlog)
venv/bin/python catch_how.py
```

### 4. GitHub Secrets

Add the following secrets to your repository under `Settings → Secrets and variables → Actions`:

| Secret | Required for | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | All | Anthropic API key |
| `WHAT_WEBHOOK_URL` | What | Slack incoming webhook |
| `WHY_WEBHOOK_URL` | Why | Slack incoming webhook |
| `HOW_WEBHOOK_URL` | How | Slack incoming webhook (used by both the synthesizer and the catcher's failure alerts) |
| `STRATECHERY_RSS_URL` | Why | Private Stratechery article RSS URL |
| `STRATECHERY_PODCAST_URL` | Why | Private Stratechery podcast RSS URL |
| `GOOGLE_SPREADSHEET_ID` | What | Spreadsheet ID for source performance tracking |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | What | Full contents of the Google Cloud service account JSON |
| `DATAIMPULSE_PROXY_URL` | Optional | Residential proxy URL |

---

## Tests

```bash
venv/bin/python -m pytest tests/
```

Tests use local mocked XML — no live HTTP requests.
