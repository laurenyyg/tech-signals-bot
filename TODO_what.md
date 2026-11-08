# What profile — to-do

Session tracker for tightening the What pipeline (fetch → score → dedupe → Sonnet → Slack). Update as items land.

**Status:** On branch `what-profile`. Phases 1–5 are done except the parked Phase 1 brand-keyword item. Crypto sources/keywords and 机器之心 are already gone (`5979541`, `d8d6f66`).

## Already done

- [x] Drop 机器之心 (`jiqizhixin`) from `FEEDS`
- [x] Drop CoinDesk from `FEEDS`; strip `coindesk` from the title-suffix regex
- [x] Remove the crypto keyword block and leftover `cftc` / `etf` scores
- [x] Rewrite `WHAT_PROFILE_PROMPT` for AI / compute (not crypto); drop “exchange listings”
- [x] Update `AGENTS.md` / `README.md` source lists and persona



## Phase 1 — Keyword sieve

ASCII keywords now use `keyword_pattern()` (whole-word match, shared with How). Remaining work: brand names at weight 5 (`google`, `facebook`, `meta`) still occupy the top 40.

- [x] Reuse How’s word-boundary matcher for ASCII keywords in `_score_item`
- [ ] Drop or replace bare brands that are not AI-specific: `google`, `facebook`, `meta` → phrases like `google deepmind`, `meta ai`
- [x] Downweight or drop generic tokens that false-hit: `chip`, `cloud`, `fed`, `agent`, `rag`, `sec`, `energy`
- [x] Add scorer unit tests (mocked titles only): true hits (`OpenAI releases GPT-5`) and false hits (`Apple storage security update`, `Chipotle earnings`, `Meta-analysis`)



## Phase 2 — Source health

Dead or empty sources currently log and continue. If every source is empty, `main.py` `sys.exit(0)` and Slack never hears. Why already posts the empty digest and lists ⚠️ failed sources.

- [x] Treat an empty RSS (or bozo with no entries) as a failed source, not a quiet week
- [x] Treat Anthropic SSR with zero parsed posts as a failed source (regex/layout change)
- [x] Skip items with no parsable date (today they pass the 7-day window)
- [x] Record per-source errors (mirror `WhyFetcher.source_errors`) and show ⚠️ lines in the digest
- [x] Empty week still posts (funnel + failures visible); never `sys.exit(0)` on “no items”
- [x] Funnel: fold zero-in-window feeds like Why (`📡 quiet: …`); failed fetches stay on their own line



## Phase 3 — Semantic dedupe

Haiku must return every index exactly once. A partial JSON array silently drops stories. A hard API error already falls back to “keep all,” which is the safe path.

- [x] Validate groups: every index `0..n-1` appears once, all in range
- [x] On invalid JSON, fences, or a partial cover → keep the pre-semantic list (same as API failure)
- [x] Unit test with a mocked group payload (omit an index, out-of-range index, markdown fence)



## Phase 4 — Memory / missed runs

Ledger + stretch (floor 8, cap 14), same shape as Why. `data/what_seen.json` is committed by the workflow after Slack succeeds.

- [x] Decide: stretch window after a missed run (Why’s `fetch_cutoff`, floor 7, cap ~30) vs a What seen ledger — **ledger + stretch, floor 8, cap 14**
- [x] If ledger: write only after a successful Slack post; workflow commits the file
- [x] Update funnel copy so “in last Nd” reflects the actual window



## Phase 5 — Tests and docs

`tests/test_rss_fetcher.py` covers URL/title dedupe, scoring, source health, semantic groups, the window/ledger, Anthropic SSR, and `source_ids` resolution.

- [x] Anthropic SSR parser: fixture HTML (no live HTTP)
- [x] Date window: in-window / too old / undated skipped
- [x] `source_ids` → `source_urls` resolution (unknown ids dropped)
- [x] Update `AGENTS.md` / `README.md` when sieve, health, or window behavior changes



## Parked

- [x] Sheets write currently runs *before* Slack; a failed post still appends a run. Move to after `post_payloads` succeeds
- [x] Sheets “Relevant (LLM) / Total (7d)” mixes source volume with Sonnet citation taste — not the same as keyword-relevant / fetched
- [x] Unused `NewsSignal` in `models.py`; unused `SUMMARY_TRUNCATE_CHARS` import in `rss_fetcher.py`
- [ ] Do **not** start with a How-style Haiku bouncer or fetching full TechCrunch bodies — the sieve is the bug; Sonnet is already the analyst



## Notes

- Local venv is Python 3.9; CI runs 3.11 — avoid 3.10+ syntax.
- `keyword_pattern()` in `common.py` is shared by How (default) and What (`hyphen_continues_word=True`).
- Anthropic pages are Next.js SSR; `_parse_anthropic_ssr` matches listing cards by `slug._type == slug` (extra CMS fields between `publishedOn` and `slug` are fine) and scans every Flight script chunk.

