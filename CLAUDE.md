# CLAUDE.md — Project Conventions for Tech Signals Bot

## Build & Run Commands
- Run main script: `venv/bin/python main.py`
- Install dependencies: `venv/bin/pip install -r requirements.txt`
- Run tests: `venv/bin/python -m pytest tests/`

## Python Style Guidelines
- **Type hints**: All function signatures must include strict type hints.
- **Data structures**: Use `dataclasses` for internal data modeling (e.g., `NewsSignal`).
- **Error handling**: Every fetcher and LLM call must implement a safe-fail mechanism using `try-except`. Do not silently fail; all critical exceptions must generate a formatted error string to be pushed to Slack.
- **Logging**: Use the `logging` module. Console output should be clean and structured.

## Project Architecture & Data Pipeline
- **Modular fetchers**: All scrapers (e.g., Techmeme, CoinDesk) must reside in `fetchers/` and output a standardized list of raw dictionaries.
- **Pre-filtering (Token Optimization)**: Never send raw firehoses to the LLM. Implement a Python-level Scoring System (Whitelist Keyword Matching) to rank RSS items. Keep only the top 40 highest-scoring items.
- **Environment**: Sensitive keys (`ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`) must only be read from `.env` via `os.getenv`.

## LLM Configuration & Prompting Strategy
- **Model selection**: Strictly use `claude-3-7-sonnet-20250219` to balance deep reasoning with token efficiency.
- **Thinking**: Enable extended thinking (`thinking={"type": "enabled", "budget_tokens": 2048}`) to ensure the model accurately clusters fragmented news and drops PR noise.
- **Persona & Business Logic**: The LLM must act as a ruthless intelligence analyst serving a Data Scientist in the Crypto industry.
  - *No PR Fluff*: Strip all marketing adjectives.
  - *ELI5 Mechanics*: Explain underlying technologies or business shifts as simply as possible.
  - *Boglehead Investment Lens*: When analyzing macro-events, prioritize long-term, second-order infrastructure implications (e.g., compute, energy, foundational protocols) over short-term price action or arbitrage.

## Slack Message Formatting
- Keep language sharp, professional, and dense. English only.
- Every Slack post must include a metadata footer in the format: `_Model: [model_id] | Tokens: [total_used]/[max_tokens] | Budget: [OK/Hit]_`
  - `total_used` = `input_tokens + output_tokens`; `max_tokens` is the cap set in the API call.
  - `Budget: Hit` when `stop_reason == 'max_tokens'`; otherwise `Budget: OK`.
  - If an error occurs before the API call completes, show the model ID but use `N/A` for token counts.
- Visual hierarchy using emojis:
  - 📰 **[Macro Event Name]**: `core_fact`
  - 🧠 **ELI5**: Simple mechanism breakdown.
  - 📈 **Second-Order Impact**: Infrastructure/long-term shift analysis.
- The digest must gracefully handle the `try-except` block: if the pipeline crashes, send a 🚨 Alert block to Slack with the exception details.

## Testing & Validation
- **Fetcher tests**: `tests/test_rss_fetcher.py` must use local, mocked XML strings to verify feed parsing and the Scoring System logic without making live HTTP requests.
- **Token cost awareness**: Log the input/output token usage for the Anthropic API call to `stdout` for monitoring.
