# CLAUDE.md — Project Conventions for Tech Signals Bot

## Build & Run Commands
- Run What pipeline: `venv/bin/python main.py --profile what`
- Run Why pipeline: `venv/bin/python main.py --profile why`
- Install dependencies: `venv/bin/pip install -r requirements.txt`
- Run tests: `venv/bin/python -m pytest tests/`

## Python Style Guidelines
- **Type hints**: All function signatures must include strict type hints.
- **Data structures**: Use `dataclasses` for internal data modeling (e.g., `NewsSignal`, `WhySignal`).
- **Error handling**: Every fetcher and LLM call must implement a safe-fail mechanism using `try-except`. Do not silently fail; all critical exceptions must generate a formatted error string to be pushed to Slack.
- **Logging**: Use the `logging` module. Console output should be clean and structured.

## Project Architecture & Data Pipeline

### What Profile
- **Sources**: Techmeme and CoinDesk via RSS (`feedparser`).
- **Pre-filtering**: Python-level keyword scoring (`KEYWORD_WHITELIST`). Drop zero-score items first, then keep the top 40 highest-scoring items.
- **LLM input**: Title + stripped summary only. No URLs, no author, no content type. Items referenced by numeric ID.
- **LLM output**: Clustered signals with `source_ids` (resolved back to URLs from metadata after the API call).

### Why Profile
- **Sources**: Stratechery, DeepLearning.AI (Andrew Ng letters), Ben-Evans (essays + presentations) — all HTML scrapers using `BeautifulSoup` with `SoupStrainer`.
- **Pre-filtering**: 7-day date window only. No scoring, no top-N cap.
- **Deep-read flagging**: Case-insensitive keyword scan against `DEEP_READ_KEYWORDS` in `config.py`.
- **LLM input**: Title, author, content type, summary excerpt, and URL included. 1-to-1 mapping (no clustering).
- **LLM output**: One signal per essay with bullet-point summary and second-order implications.

### Shared Conventions
- **Modular fetchers**: All scrapers reside in `fetchers/` and output a standardised list of raw dicts.
- **Environment**: Sensitive keys must only be read from `.env` via `os.getenv`. Required vars: `ANTHROPIC_API_KEY`, `WHAT_WEBHOOK_URL`, `WHY_WEBHOOK_URL`. Optional: `DATAIMPULSE_PROXY_URL`.
- **Proxy**: If `DATAIMPULSE_PROXY_URL` is set, all `requests.get()` calls and `feedparser` use it. Falls back to direct connection if absent.

## LLM Configuration
- **Model**: `claude-sonnet-4-6`
- **Thinking**: `{"type": "adaptive"}` — lets the model decide when extended thinking is warranted.
- **Max tokens**: 16,000 output token budget.
- **Persona (What)**: Ruthless intelligence analyst. No PR fluff, ELI5 mechanics, Boglehead investment lens, cluster related items, drop non-structural noise.
- **Persona (Why)**: Strategic intelligence analyst. Dense bullet-point summaries extracting core thesis and second-order infrastructure/economic implications.

## Slack Message Formatting
- Keep language sharp, professional, and dense. English only.
- Every Slack post must include a FinOps metadata footer: `_Model: [model_id] | Tokens: [total_used]/[max_tokens] | Budget: [OK/Hit]_`
  - `total_used` = `input_tokens + output_tokens`; `max_tokens` is the cap set in the API call.
  - `Budget: Hit` when `stop_reason == 'max_tokens'`; otherwise `Budget: OK`.
  - If an error occurs before the API call completes, show the model ID but use `N/A` for token counts.
- The digest must gracefully handle the `try-except` block: if the pipeline crashes, send a 🚨 Alert block to Slack with the exception details.

### What Profile Visual Hierarchy
- 📰 **[Macro Event Name]**: `core_fact`
- 🧠 **ELI5**: Simple mechanism breakdown.
- 📈 **Second-Order Impact**: Infrastructure/long-term shift analysis.

### Why Profile Visual Hierarchy
- ❗️ Deep read flagged (keyword match)
- 🧠 Standard essay
- 🗂 Presentation
- Header line: `prefix *title* — _author_ · date`
- Summary as bullet points, source link as final bullet.
- Source counts per feed before FinOps footer.

## Testing & Validation
- **Fetcher tests**: `tests/test_rss_fetcher.py` must use local, mocked XML strings to verify feed parsing and the scoring system logic without making live HTTP requests.
- **Token cost awareness**: Log the input/output token usage for the Anthropic API call to `stdout` for monitoring.
