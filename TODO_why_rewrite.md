# Why profile rewrite — to-do

Session tracker for the Why pipeline rewrite. Update as items land.

**Status:** Phase 1 + Phase 2 items 3–4 committed on branch `why-rewrite` (087a7fe) and verified by a live run. Not merged to `main`. Tests: 81 passing.

## Phase 1 — Reliability ✅

- [x] a16z: parse the new `{"posts": [...], "nextCursor": ...}` response; keep bare-list fallback
- [x] a16z: prefer `truncated_body_text` over `description`
- [x] Ben Evans: parse day-first dates (`3 September 2026`) and `Sept 2026`
- [x] Drop items with no parsable date instead of letting them pass the window
- [x] Per-source failures recorded in `WhyFetcher.source_errors` and shown as ⚠️ lines in Slack
- [x] One Sonnet call per item (`WHY_ITEM_MAX_TOKENS` = 8000); metadata resolved from fetcher, not echoed by LLM
- [x] Failed items listed as "⚠️ Not summarised"; 🚨 alert only if every item fails
- [x] Empty week still posts the digest (so source errors stay visible)
- [x] Rewrite `WHY_PROFILE_PROMPT` for single items (bullets, podcasts, deep-read criteria)
- [x] Move Why code from `main.py` into `why_pipeline.py`; remove `include_metadata` path
- [x] Remove unused `needs_deep_read` / `DEEP_READ_KEYWORDS`
- [x] Pure, testable parsers + `tests/test_why.py` (mocked input only)
- [x] Update `CLAUDE.md` and `README.md`
- [x] Live check: fetcher against real sites; 2 real Sonnet calls (no Slack post)
- [x] Commit Phase 1 (+ Phase 2 items 3–4) on branch `why-rewrite`
- [x] Manual run on `why-rewrite` ([run 42464578762](https://github.com/laurenyyg/tech-signals-bot/actions/runs/42464578762)): success
  - 9 in window, 2 skipped by ledger, 7 a16z summarised, 0 failures, 0 source errors; 3,779 tokens; ~35s of LLM time
  - 7 a16z announcements filtered; ledger commit pushed by the bot (57372a9), no tokens in file
- [x] Phase 2 + Phase 3 committed (8e1f377); manual run [42466438864](https://github.com/laurenyyg/tech-signals-bot/actions/runs/42466438864): success — DL.AI Nov 6 letter (61 → 2,751 chars) summarised and posted, 9 items skipped by ledger, bot ledger commit ef910f3 (no tokens)
- [ ] Check the Slack post looks right (funnel line format, single DL.AI card)
- [ ] Open PR `why-rewrite` → `main` and merge (Friday's scheduled run uses `main`)



## Phase 2 — Summary quality

- [ ] 1. Raise Why input truncation (1,500 chars → ~8–12k); Stratechery bodies are ~13k+
- [ ] 2. Fetch full bodies (`fetch_body()`) for DeepLearning.AI, Ben Evans, and short a16z items
- [x] 3. a16z filter: skip `Fundraise` tag, "Investing in / Incubating / Welcome / Meet the / Announcing / Introducing" titles, titles mentioning "a16z" (28/100 sampled posts dropped, all announcements)
- [x] 4. Seen-ledger `data/why_seen.json` (shared helpers moved to `seen_ledger.py`; How unchanged)
  - [x] Key = canonical RSS guid → link → `source:title`; strips `access_token`/tracking, keeps `?p=ID`
  - [x] Written only after a successful Slack post; summarised items only (failures retried)
  - [x] Workflow commits the ledger
  - [x] Seeded with the 2 Stratechery items posted on Nov 7 (no tokens in file)
- [x] 4b. Month-only dates ("Nov 2026") stay in window all month; shown as "Nov 2026"



## Phase 3 — Robustness

- [ ] Split digests over ~22 items across multiple Slack messages (50-block limit)



## Open decisions

- [ ] Stratechery links carry `?access_token=<JWT>` into Slack — strip it? (link would then need a normal login)
- [ ] With the ledger, widen `DAYS_FILTER` (8 → ~14) so a failed week's items are recovered next run?
- [x] a16z "Charts of the Week" roundups — keep or skip?



## Notes

- Pre-existing, out of scope: unused `SUMMARY_TRUNCATE_CHARS` import in `fetchers/rss_fetcher.py`; `models.py` dataclasses are unused.
- Local venv is Python 3.9; CI runs 3.11 — avoid 3.10+ syntax.
- ~~a16z items from Nov 2–5 were never posted~~ — recovered by the Nov 8 manual run.
- a16z inputs were only ~330 tokens each in the live run → Phase 2 item 2 (full bodies) is the next quality win.

