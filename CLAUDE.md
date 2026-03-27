# CLAUDE.md — Project Conventions for Tech Signals Bot

## Build & Run Commands
- Run What pipeline: `venv/bin/python main.py --profile what`
- Run Why pipeline: `venv/bin/python main.py --profile why`
- Install dependencies: `venv/bin/pip install -r requirements.txt`
- Run tests: `venv/bin/python -m pytest tests/`

## Python Style Guidelines
- **Type hints**: All function signatures must include strict type hints.
- **Error handling**: Every fetcher and LLM call must use `try-except`. Do not silently fail — all critical exceptions must generate a formatted error string pushed to Slack.

## Data Pipeline

### What Profile
- **Sources**: Techmeme, CoinDesk, and TechCrunch via RSS (`feedparser`).
- **Cross-feed dedupe**: After the 7-day window, merge duplicates in `rss_fetcher.py`: first by normalized URL (strip `utm_*`, `fbclid`, `gclid`, etc.), then by normalized title (strip `| TechCrunch` / `- CoinDesk` style suffixes). On conflict: higher keyword score wins; tie-break newer `published`; if still tied, keep the first-seen row in dedupe order (roughly `FEEDS` order for URL pass).
- **Pre-filtering**: Keyword scoring against `KEYWORD_WHITELIST`. Drop zero-score items after dedupe, then keep the top 40 highest-scoring items. `source_counts["relevant"]` counts unique surviving rows per feed (not raw RSS rows).
- **LLM input**: Title + stripped summary only. No URLs, no author, no content type. Items referenced by numeric ID.
- **LLM output**: Clustered signals with `source_ids` — resolved back to URLs from metadata after the API call.

### Why Profile
- **Sources**: Stratechery (articles + podcast via private RSS; deduplicated by title, articles win), DeepLearning.AI (Andrew Ng letters), Ben-Evans (essays + presentations), a16z (newsletter essays; skips "Investing in" posts), One Useful Thing (Ethan Mollick; public RSS).
- **Pre-filtering**: 7-day date window only. No scoring, no top-N cap.
- **Deep-read flagging**: Case-insensitive keyword scan against `DEEP_READ_KEYWORDS` in `config.py`.
- **LLM input**: Title, author, content type, summary (full article/transcript body, truncated to 1500 chars via `SUMMARY_TRUNCATE_CHARS` in `config.py`), and URL included. 1-to-1 mapping (no clustering).

### Shared
- **Environment**: Sensitive keys read from `.env` via `os.getenv`. Required: `ANTHROPIC_API_KEY`, `WHAT_WEBHOOK_URL`, `WHY_WEBHOOK_URL`, `STRATECHERY_RSS_URL`, `STRATECHERY_PODCAST_URL` (Why profile). Optional: `DATAIMPULSE_PROXY_URL`.
- **Proxy**: If `DATAIMPULSE_PROXY_URL` is set, all `requests.get()` and `feedparser` calls use it. Falls back to direct if absent.

## LLM Configuration
- **Model**: `claude-sonnet-4-6`
- **Thinking**: `{"type": "adaptive"}`
- **Max tokens**: 16,000 output token budget.
- **Persona (What)**: Ruthless intelligence analyst — no PR fluff, ELI5 mechanics, Boglehead investment lens, cluster related items, drop non-structural noise.
- **Persona (Why)**: Strategic intelligence analyst — dense bullet-point summaries extracting core thesis and second-order infrastructure/economic implications.

## Slack Message Formatting
- Every Slack post must include a FinOps footer: `_Model: [model_id] | Tokens: [total_used]/[max_tokens] | Budget: [OK/Hit]_`
  - `Budget: Hit` when `stop_reason == 'max_tokens'`; otherwise `Budget: OK`.
  - If the pipeline crashes before the API call completes, show the model ID but use `N/A` for token counts.
- If the pipeline crashes, send a 🚨 Alert block to Slack with the exception details.

### What Profile Visual Hierarchy
- 📰 **[Macro Event Name]**: `core_fact`
- 🧠 **ELI5**: Simple mechanism breakdown.
- 📈 **Second-Order Impact**: Infrastructure/long-term shift analysis.

### Why Profile Visual Hierarchy
- ❗️ Deep read flagged (keyword match) · 🧠 Standard essay · 🗂 Presentation · 🎙 Podcast
- Header line: `prefix *title* — _author_ · date`
- Summary as bullet points, source link as final bullet.
- Source counts per feed before FinOps footer.

## Testing
- Tests must use local mocked XML strings — no live HTTP requests.
