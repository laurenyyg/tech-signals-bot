# CLAUDE.md — Project Conventions for Tech Signals Bot

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
- **Sources**: Techmeme, CoinDesk, TechCrunch, OpenAI News, and 机器之心 (Jiqizhixin) via RSS (`feedparser`); Anthropic news and research via SSR HTML scraping (Sanity CMS is no longer publicly accessible without auth).
- **Cross-feed dedupe**: Three passes in `rss_fetcher.py`: (1) normalised URL (strip `utm_*`, `fbclid`, `gclid`, etc.), (2) normalised title (strip `| TechCrunch` / `- CoinDesk` style suffixes), (3) semantic dedup via `claude-haiku-4-5-20251001` — groups titles covering the same event, catching cross-language duplicates (e.g. Chinese 机器之心 article vs English coverage). On conflict across all passes: higher keyword score wins; tie-break newer `published`; if still tied, keep first-seen.
- **Pre-filtering**: Keyword scoring against `KEYWORD_WHITELIST`. Drop zero-score items after dedupe, then keep the top 40 highest-scoring items. `source_counts["relevant"]` counts items with score > 0 per feed after dedup (before top-40 cut); `source_counts["sent_to_llm"]` reflects the final top-40 batch.
- **LLM input**: Title + stripped summary only. No URLs, no author, no content type. Items referenced by numeric ID.
- **LLM output**: Clustered signals with `source_ids` — resolved back to URLs from metadata after the API call.

### Why Profile
- **Sources**: Stratechery (articles + podcast via private RSS; deduplicated by title, articles win over podcasts), DeepLearning.AI (Andrew Ng letters), Ben-Evans (essays + presentations), a16z (newsletter essays; skips posts whose title starts with "Investing in"), One Useful Thing (Ethan Mollick; public RSS).
- **Pre-filtering**: 7-day date window only. No scoring, no top-N cap.
- **Deep-read flagging**: The LLM determines `is_deep_read` from content. The fetcher also computes a keyword scan against `DEEP_READ_KEYWORDS` in `config.py` (`needs_deep_read` field) but this is not currently passed to the LLM — the LLM judges independently.
- **LLM input**: Title, author, content type, summary (full article/transcript body, truncated to 1500 chars via `SUMMARY_TRUNCATE_CHARS` in `config.py`), and URL included. 1-to-1 mapping (no clustering).

### How Profile (Decoupled)
- **Daily Catcher** (`catch_how.py`): Fetches ChatPRD and SSPAI items published in the last `HOW_DAYS_FILTER` (3) days (`--days N` for a one-off backfill). Skips URLs already in the backlog or the seen ledger, downloads each new page and stores its extracted text (`fetch_body()` → `preprocess_article()`, truncated to `HOW_BODY_MAX_CHARS`) as `body`, and appends to `data/how_backlog.json`. No LLM calls. Each source runs in its own `try`; a failing source posts a 🚨 alert to `HOW_WEBHOOK_URL` and the other source still runs. Exits non-zero only if every source fails. Runs daily via GitHub Actions and commits the backlog and ledger.
- **ChatPRD source**: Parses `PodcastSeries.hasPart` (`PodcastEpisode`) JSON-LD on the How I AI page, with the legacy `blogPost` shape as fallback. Raises if neither is present.
- **SSPAI filter**: Author whitelist takes priority; otherwise, items must match `HOW_POSITIVE_KEYWORDS` and not match `HOW_NEGATIVE_KEYWORDS` (defined in `config.py`). ASCII keywords (`AI`, `API`, …) match as whole words, case-insensitive; CJK keywords match as substrings. Raises if the feed returns no entries.
- **Weekly Synthesizer** (`main.py --profile how` → `how_pipeline.run_how_pipeline`): Reads the backlog and runs a two-stage LLM cascade:
 - **Stage 1 — Haiku Bouncer** (`claude-haiku-4-5-20251001`, max 1024 tokens, one call per item): Input is title + `body` (re-fetched if empty, falling back to the feed summary). First output line is `PASS` or `NULL` (prefix match, so `NULL.` counts); `PASS` is followed by tool stack, steps, and outcome as plain text.
 - **Stage 2 — Sonnet Formatter** (`claude-sonnet-4-6`, max 16,000 tokens, with thinking): **One batched call** over all passing items, referenced by numeric ID. Returns `source_id` + `tech_stack`, `workflow_steps`, `roi_impact`; title, source, and URL are resolved from metadata.
 - **Failure policy**: If every Stage 1 call errors, or Stage 2 errors or returns invalid JSON, the run raises and posts a 🚨 alert. Individual Stage 1 errors are shown in the funnel line; those items stay in the backlog for retry (up to `MAX_ATTEMPTS` = 3 runs).
 - **State update**: **Only if** the Slack post succeeds, the backlog is rewritten to just the retry items and processed URLs are appended to `data/how_seen.json`. Failed runs leave both files untouched.
- **Seen ledger** (`data/how_seen.json`): `{url, processed_at}` entries, pruned to `HOW_SEEN_RETENTION_DAYS` (60) on every write. Prevents items from re-entering the backlog after the weekly reset. Retention must stay well above `HOW_DAYS_FILTER`.

### Shared
- **Environment**: Sensitive keys read from `.env` via `os.getenv`. Required: `ANTHROPIC_API_KEY`, `WHAT_WEBHOOK_URL`, `WHY_WEBHOOK_URL`, `HOW_WEBHOOK_URL`, `STRATECHERY_RSS_URL`, `STRATECHERY_PODCAST_URL` (Why profile). Optional: `DATAIMPULSE_PROXY_URL`.
- **Proxy**: If `DATAIMPULSE_PROXY_URL` is set, HTTP requests use it. For `requests.get()` calls, pass a `proxies` dict. For `feedparser.parse()` calls, pass a `urllib.request.ProxyHandler` in the `handlers` list. Falls back to direct if absent.
- **HTML extraction**: `preprocess_article()` in `main.py` runs `trafilatura.extract()` first; falls back to `_strip_html()` (regex) if trafilatura returns nothing.

## LLM Configuration
- **Primary model**: `claude-sonnet-4-6` — used for What, Why, and How Stage 2.
- **Thinking**: `{"type": "adaptive"}` — enabled for What, Why, and How Stage 2 (Sonnet only; not supported by Haiku).
- **Max tokens**: 16,000 output token budget for Sonnet calls.
- **Haiku (What dedup)**: `claude-haiku-4-5-20251001` — semantic cross-feed deduplication only.
- **Haiku (How bouncer)**: `claude-haiku-4-5-20251001` — Stage 1 cognitive filter, max 1024 output tokens. (`claude-3-5-haiku-20241022` is retired and returns 404.)
- **Persona (What)**: Ruthless intelligence analyst — no PR fluff, ELI5 mechanics, Boglehead investment lens, cluster related items, drop non-structural noise. Focus areas: AI model releases/breakthroughs, real-world AI impact evidence, crypto protocol shifts, semiconductor/compute infra, structural regulation.
- **Persona (Why)**: Strategic intelligence analyst — dense bullet-point summaries extracting core thesis and second-order infrastructure/economic implications.
- **Persona (How)**: Ruthless reverse-engineer — extracts replicable SOPs, ignores personal stories and fluff. Three-step output: Trigger → Process → Output.

## Slack Message Formatting
- Every Slack post must include a FinOps footer: `_Model: [model_id] | Tokens: [total_used]/[max_tokens] | Budget: [OK/Hit]_`
  - `Budget: Hit` when `stop_reason == 'max_tokens'` (What/Why) or when any stage output tokens reach its limit (How cascade).
  - If the pipeline crashes before the API call completes, show the model ID but use `N/A` for token counts.
  - For the How cascade, `model_id` is `claude-haiku-4-5-20251001 + claude-sonnet-4-6`, `total_used` is the aggregate of all Haiku + Sonnet calls in that run, and `max_tokens` is the sum of the per-call output budgets for the calls made.
- If the pipeline crashes, send a 🚨 Alert block to Slack with the exception details.

### What Profile Visual Hierarchy
- 📰 **[Macro Event Name]**: `core_fact`
- 🧠 **ELI5**: Simple mechanism breakdown.
- 📈 **Second-Order Impact**: Infrastructure/long-term shift analysis.

### Why Profile Visual Hierarchy
- ❗️ Deep read (LLM-flagged) · 🧠 Standard essay · 🗂 Presentation · 🎙 Podcast
- Header line: `prefix *title* — _author_ · date`
- Summary as bullet points, source link as final bullet.
- Source counts per feed before FinOps footer.

### How Profile Visual Hierarchy
- 🛠️ **Tech Stack**: comma-separated tool list.
- 📝 **Workflow SOP**: 3 bullet points (Step 1: Trigger, Step 2: Process, Step 3: Output).
- 💰 **ROI/Impact**: one sentence with concrete pain point and measurable gain.
- Funnel line per source before FinOps footer: `N caught › N passed bouncer › N workflow(s)`, plus `(N errored)` when Stage 1 calls failed.
- Empty backlog still posts ("No new items were caught this week.") so the weekly run is always visible.

## Testing
- Tests must use local mocked XML strings — no live HTTP requests.
