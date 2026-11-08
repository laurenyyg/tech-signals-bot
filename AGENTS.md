# AGENTS.md — Project Conventions for Tech Signals Bot

## Build & Run Commands
- Run What pipeline: `venv/bin/python main.py --profile what`
- Run Why pipeline: `venv/bin/python main.py --profile why`
- Run How synthesizer: `venv/bin/python main.py --profile how`
- Run How catcher (no LLM): `venv/bin/python catch_how.py` (backfill: `--days 14`)
- Install dependencies: `venv/bin/pip install -r requirements.txt`
- Run tests: `venv/bin/python -m pytest tests/`

## Python Style Guidelines
- **Type hints**: All function signatures must include strict type hints.
- **Error handling**: Every fetcher and LLM call must use `try-except`. Do not silently fail — all critical exceptions must generate a formatted error string pushed to Slack.

## Data Pipeline

### What Profile
- **Sources**: Techmeme, TechCrunch, and OpenAI News via RSS (`feedparser`); Anthropic news and research via SSR HTML scraping (Sanity CMS is no longer publicly accessible without auth).
- **Cross-feed dedupe**: Three passes in `rss_fetcher.py`: (1) normalised URL (strip `utm_*`, `fbclid`, `gclid`, etc.), (2) normalised title (strip `| TechCrunch` / `- Techmeme` style suffixes), (3) semantic dedup via `claude-haiku-4-5-20251001` — groups titles covering the same event (e.g. Techmeme vs TechCrunch on the same announcement). On conflict across all passes: higher keyword score wins; tie-break newer `published`; if still tied, keep first-seen.
- **Pre-filtering**: Keyword scoring against `KEYWORD_WHITELIST`. Drop zero-score items after dedupe, then keep the top 40 highest-scoring items. `source_counts["relevant"]` counts items with score > 0 per feed after dedup (before top-40 cut); `source_counts["sent_to_llm"]` reflects the final top-40 batch.
- **LLM input**: Title + stripped summary only. No URLs, no author, no content type. Items referenced by numeric ID.
- **LLM output**: Clustered signals with `source_ids` — resolved back to URLs from metadata after the API call.

### Why Profile
- **Sources**: Stratechery (articles + podcast via private RSS; deduplicated by title, articles win over podcasts; articles carry full text in RSS, podcast entries carry only a ~400-char episode description — no transcript), DeepLearning.AI (Andrew Ng letters, scraped from `/the-batch/tag/letters/`), Ben-Evans (essays + presentations), a16z (newsletter essays; announcements skipped via `a16z_skip_reason()`: posts tagged `A16Z_SKIP_TAGS` (`Fundraise`), titles starting with `A16Z_SKIP_TITLE_PREFIXES` ("Investing in", "Welcome", "Meet the", "Announcing", "Introducing", …), or mentioning "a16z"; lists in `config.py`), One Useful Thing (Ethan Mollick; public RSS).
- **Pre-filtering**: Date window from `fetch_cutoff()`: floor `DAYS_FILTER` = 8 (one day of slack because scheduled runs start hours late). After a missed run the window stretches back to the last ledger `processed_at` minus `WHY_DAYS_SLACK` (1), capped at `WHY_DAYS_MAX` (30). An empty ledger uses the 8-day floor. No scoring, no top-N cap. Items with no parsable date are skipped as parse errors, never passed through. Human dates are ranges (`parse_human_date_range`): a day-precision date is in window if that day ends after the cutoff; a month-only date ("Nov 2026", Ben Evans presentations) stays in window for the whole month and is shown as `Nov 2026` (`date_precision: "month"`).
- **Seen ledger** (`data/why_seen.json`): `{url, processed_at}` entries, pruned to `WHY_SEEN_RETENTION_DAYS` (60) on every write; must exceed the window plus a full month. Items already in the ledger are dropped before the LLM (counted as "N already posted" in the funnel). Key is `ledger_key()`: canonical RSS `guid` (WordPress-permanent), else canonical link, else `source:title`. `canonical_url()` (in `seen_ledger.py`, shared with How) drops `access_token` and tracking params but keeps identifying ones like `?p=20955` — **Stratechery tokens must never reach the committed ledger**. Updated **only after** a successful Slack post, with summarised items only (failed items are retried next run while still in window). The workflow commits the file.
- **Source health**: Each source runs in its own guard. A source fails (recorded in `WhyFetcher.source_errors`) when the request or parse raises, the feed/page has no entries or cards, or every entry fails to parse (e.g. a date-format or API-shape change). Failed sources are listed as ⚠️ lines in the digest; they do not abort the run.
- **Parsers**: Pure functions (`parse_rss_feed`, `parse_deeplearning_ai_page`, `parse_ben_evans_page`, `parse_a16z_archive`, `parse_human_date`) so they can be tested on mocked strings. A card or post with no title raises (counted toward the all-entries-failed check) rather than being skipped silently. DeepLearning.AI cards: title is the first `h2`/`h3`, date is `<time datetime>` or else the first `footer span` (`Nov 6, 2026`), link is the first `a[href]` that isn't a `/tag/` badge. Ben Evans dates are day-first (`3 September 2026`, `Sept 2026`); the a16z archive returns `{"posts": [...], "nextCursor": ...}` and `truncated_body_text` is preferred over `description`.
- **LLM calls** (`why_pipeline.py`): **One Sonnet call per item** (max `WHY_ITEM_MAX_TOKENS` = 8000 output tokens each, with thinking). Input is title, author, content type, and text (HTML-stripped, truncated to `WHY_BODY_MAX_CHARS` = 12000). Output is a JSON object with only `summary` (bullets) and `is_deep_read`; title, author, date, type, and URL come from fetcher metadata.
- **Body fetching** (`enrich_bodies`, after the ledger check): sources in `WHY_BODY_FETCH_SOURCES` (a16z, Ben Evans, DeepLearning.AI — feeds that only give excerpts) get their page downloaded via `fetch_body()`; the page text replaces `summary` only if longer, otherwise the item is marked `excerpt_only`. Stratechery and One Useful Thing already carry full text in RSS and are **never** fetched (Stratechery page fetches would send the private token to the site with no benefit).
- **Funnel** (`build_funnel` / `funnel_line` / `funnel_lines`): one line per active feed in `FEEDS` order — `📡 *<homepage|feed>*   N fetched › N in window › N new › N summarised`, with notes for announcements skipped (a16z), podcasts deduped against an article ("same as an article"), already posted, failed, and excerpt only. Source names are Slack links (`FEED_HOME_URLS`). A source whose fetch failed shows `fetch failed` and is never folded. Feeds with `in_window == 0` collapse to one line: `📡 quiet: *<url|feed>*, …  (N fetched, 0 in window)`. `fetched` is raw entries/cards before any filtering; `in window` includes the skipped announcements and deduped podcasts, so in window − skipped − already posted = new.
- **Stratechery links**: Slack links keep the `access_token` (the channel has one member); only the committed ledger is token-free.
- **Failure policy**: An item whose call errors, hits its token budget, or returns invalid JSON is dropped and listed as "⚠️ Not summarised" in the digest. The run raises (🚨 alert) only if every item fails. A week with no items still posts the empty digest so source errors stay visible.

### How Profile (Decoupled)
- **Daily Catcher** (`catch_how.py`): Fetches ChatPRD and SSPAI items published in the last `HOW_DAYS_FILTER` (3) days (`--days N` for a one-off backfill). Skips URLs already in the backlog or the seen ledger, downloads each new page and stores its extracted text (`fetch_body()` → `preprocess_article()`, truncated to `HOW_BODY_MAX_CHARS`) as `body`, and appends to `data/how_backlog.json`. No LLM calls. Each source runs in its own `try`; a failing source posts a 🚨 alert to `HOW_WEBHOOK_URL` and the other source still runs. Exits non-zero only if every source fails. Runs daily via GitHub Actions and commits the backlog and ledger.
- **ChatPRD source**: Parses `PodcastSeries.hasPart` (`PodcastEpisode`) JSON-LD on the How I AI page (legacy `blogPost` shape as fallback; raises if neither is present). For each in-window episode, follows its `/how-i-ai/workflows/<slug>` links and emits **one backlog item per workflow page**, with `body` rendered from the page's schema.org `HowTo` JSON-LD (tools, inputs, numbered steps, outcome) and `episode` set to the parent episode URL. Episodes with no workflow links (or whose workflow pages all fail) fall back to a single episode item.
- **SSPAI filter**: Author whitelist takes priority; otherwise, items must match `HOW_POSITIVE_KEYWORDS` and not match `HOW_NEGATIVE_KEYWORDS` (defined in `config.py`). ASCII keywords (`AI`, `API`, …) match as whole words, case-insensitive; CJK keywords match as substrings. Raises if the feed returns no entries.
- **Weekly Synthesizer** (`main.py --profile how` → `how_pipeline.run_how_pipeline`): Reads the backlog and runs a two-stage LLM cascade:
 - **Stage 1 — Haiku Bouncer** (`claude-haiku-4-5-20251001`, max 1024 tokens, one call per item): Input is title + `body` (re-fetched if empty, falling back to the feed summary). First output line is `PASS` or `NULL` (prefix match, so `NULL.` counts); `PASS` is followed by tool stack, steps, and outcome as plain text. **Skipped** for pre-structured items (`kind == "howto"`, i.e. ChatPRD workflow pages), whose `body` goes straight to Stage 2.
 - **Stage 2 — Sonnet Formatter** (`claude-sonnet-4-6`, max 16,000 tokens, with thinking): **One batched call** over all passing items, referenced by numeric ID. Returns `source_id` + `tech_stack`, `workflow_steps`, `roi_impact`; title, source, and URL are resolved from metadata. `tech_stack` is named products only (integrations folded into their product, no generic categories); `dedupe_tools()` also drops case-insensitive duplicates and `X MCP` when `X` is listed. `roi_impact` must be grounded in the source's stated outcome/results — no "eliminates"/"replaces" claims or figures the source doesn't state.
 - **Failure policy**: If Stage 2 errors or returns invalid JSON, or every Stage 1 call errors and nothing else passed, the run raises and posts a 🚨 alert. Individual Stage 1 errors are shown in the funnel line; those items stay in the backlog for retry (up to `MAX_ATTEMPTS` = 3 runs).
 - **State update**: **Only if** the Slack post succeeds, the backlog is rewritten to just the retry items and processed URLs are appended to `data/how_seen.json`. Failed runs leave both files untouched.
- **Seen ledger** (`data/how_seen.json`): `{url, processed_at}` entries, pruned to `HOW_SEEN_RETENTION_DAYS` (60) on every write. Prevents items from re-entering the backlog after the weekly reset. Retention must stay well above `HOW_DAYS_FILTER`.

### Shared
- **Environment**: Sensitive keys read from `.env` via `os.getenv`. Required: `ANTHROPIC_API_KEY`, `WHAT_WEBHOOK_URL`, `WHY_WEBHOOK_URL`, `HOW_WEBHOOK_URL`, `STRATECHERY_RSS_URL`, `STRATECHERY_PODCAST_URL` (Why profile). Optional: `DATAIMPULSE_PROXY_URL`.
- **Proxy**: If `DATAIMPULSE_PROXY_URL` is set, HTTP requests use it. For `requests.get()` calls, pass a `proxies` dict. For `feedparser.parse()` calls, pass a `urllib.request.ProxyHandler` in the `handlers` list. Falls back to direct if absent.
- **HTML extraction**: `preprocess_article()` in `main.py` runs `trafilatura.extract()` first; falls back to `_strip_html()` (regex) if trafilatura returns nothing.

## LLM Configuration
- **Primary model**: `claude-sonnet-4-6` — used for What, Why, and How Stage 2.
- **Thinking**: `{"type": "adaptive"}` — enabled for What, Why (per item), and How Stage 2 (Sonnet only; not supported by Haiku).
- **Max tokens**: 16,000 output token budget for Sonnet calls.
- **Haiku (What dedup)**: `claude-haiku-4-5-20251001` — semantic cross-feed deduplication only.
- **Haiku (How bouncer)**: `claude-haiku-4-5-20251001` — Stage 1 cognitive filter, max 1024 output tokens. (`claude-3-5-haiku-20241022` is retired and returns 404.)
- **Persona (What)**: Ruthless intelligence analyst — no PR fluff, ELI5 mechanics, Boglehead investment lens, cluster related items, drop non-structural noise. Focus areas: AI model releases/breakthroughs, real-world AI impact evidence, semiconductor/compute infra, structural regulation.
- **Persona (Why)**: Strategic intelligence analyst — dense bullet-point summaries extracting core thesis and second-order infrastructure/economic implications.
- **Persona (How)**: Ruthless reverse-engineer — extracts replicable SOPs, ignores personal stories and fluff. Three-step output: Trigger → Process → Output.

## Slack Message Formatting
- Every Slack post must include a FinOps footer: `_Model: [model_id] | Tokens: [total_used]/[max_tokens] | Budget: [OK/Hit]_`
  - `Budget: Hit` when `stop_reason == 'max_tokens'` (What), when any per-item call hits it (Why), or when any stage output tokens reach its limit (How cascade).
 - For Why, `total_used` is the aggregate over all per-item calls and `max_tokens` is `WHY_ITEM_MAX_TOKENS` × calls made.
  - If the pipeline crashes before the API call completes, show the model ID but use `N/A` for token counts.
  - For the How cascade, `model_id` lists the models actually called in the run (e.g. `claude-haiku-4-5-20251001 + claude-sonnet-4-6`, or just `claude-sonnet-4-6` when every item was pre-structured), `total_used` is the aggregate of all Haiku + Sonnet calls in that run, and `max_tokens` is the sum of the per-call output budgets for the calls made.
- If the pipeline crashes, send a 🚨 Alert block to Slack with the exception details.
- **Message splitting (all profiles)**: every digest is built as card groups plus a tail (warnings, funnel, footer) and passed to `split_into_messages()` in `common.py`. A message holds at most 50 blocks and ~30,000 chars; section and context text is capped at 3000 chars (`fit_text`), context blocks at 10 elements (`context_blocks`). A card is never split across messages, but a source's cards may be. Multi-message digests get `(i/N)` in each header; the tail goes on the last message (or its own if it doesn't fit). `post_payloads()` posts in order with a 1 s pause and raises on the first failure — ledgers/backlogs are updated only after every message posts, so a partial failure may re-post earlier messages on retry. Empty digests are a single message.

### What Profile Visual Hierarchy
- 📰 **[Macro Event Name]**: `core_fact`
- 🧠 **ELI5**: Simple mechanism breakdown.
- 📈 **Second-Order Impact**: Infrastructure/long-term shift analysis.

### Why Profile Visual Hierarchy
- ❗️ Deep read (LLM-flagged) · 🧠 Standard essay · 🗂 Presentation · 🎙 Podcast
- Header line: `prefix *title* — _publication_ · date` — always the source (`SOURCE_LABELS`: Stratechery, DeepLearning.AI, Benedict Evans, a16z, One Useful Thing), never the individual writer. The author is still sent to the LLM.
- Summary as bullet points, source link as final bullet.
- ⚠️ lines for failed sources and unsummarised items, then the per-feed funnel, before FinOps footer.

### How Profile Visual Hierarchy
- 🛠️ **Tech Stack**: comma-separated tool list.
- 📝 **Workflow SOP**: 3 bullet points (Step 1: Trigger, Step 2: Process, Step 3: Output).
- 💰 **ROI/Impact**: one sentence with concrete pain point and measurable gain.
- Funnel line per source before FinOps footer: `N caught › N passed bouncer › N workflow(s)`, plus `(N pre-structured, not bounced)` for HowTo items and `(N errored)` when Stage 1 calls failed.
- Empty backlog still posts ("No new items were caught this week.") so the weekly run is always visible.

## Testing
- Tests must use local mocked XML strings — no live HTTP requests.
