# Why profile rewrite — to-do

Session tracker for the Why pipeline rewrite. Update as items land.

**Status:** Phases 1–3 committed on branch `why-rewrite` (latest code: 8e1f377) and verified by a live run. Merged to `main` via PR #2. Tests: 102 passing.

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
- [x] Update `CLAUDE.md` (now `AGENTS.md`) and `README.md`
- [x] Live check: fetcher against real sites; 2 real Sonnet calls (no Slack post)
- [x] Commit Phase 1 (+ Phase 2 items 3–4) on branch `why-rewrite`
- [x] Manual run on `why-rewrite` ([run 42464578762](https://github.com/laurenyyg/tech-signals-bot/actions/runs/42464578762)): success
  - 9 in window, 2 skipped by ledger, 7 a16z summarised, 0 failures, 0 source errors; 3,779 tokens; ~35s of LLM time
  - 7 a16z announcements filtered; ledger commit pushed by the bot (57372a9), no tokens in file
- [x] Phase 2 + Phase 3 committed (8e1f377); manual run [42466438864](https://github.com/laurenyyg/tech-signals-bot/actions/runs/42466438864): success — DL.AI Nov 6 letter (61 → 2,751 chars) summarised and posted, 9 items skipped by ledger, bot ledger commit ef910f3 (no tokens)
- [x] Check the Slack post looks right (funnel line format, single DL.AI card) — funnel arithmetic checks out; zero-activity feeds are real (One Useful Thing last posted Oct 20)
- [x] Open PR `why-rewrite` → `main` and merge — [PR #2](https://github.com/laurenyyg/tech-signals-bot/pull/2), merged as 45d7f25
- [ ] Watch first live runs of the shared splitter: What (Sun Nov 8, 2:47 PM PST / 22:47 UTC), How synthesizer (Sat Nov 14), Why (Fri Nov 13)



## Phase 2 — Summary quality

- [x] 0. DeepLearning.AI parser fixed (titles moved to `h3`, date to `footer span`, tag badge is first link); untitled cards now raise instead of being skipped silently — it had been returning nothing for weeks
- [x] 1. Raise Why input truncation (1,500 → `WHY_BODY_MAX_CHARS` = 12,000)
- [x] 2. Fetch full bodies (`enrich_bodies`) for a16z, Ben Evans, DeepLearning.AI; never Stratechery (full text already in RSS; podcasts only have ~400-char descriptions) or One Useful Thing
- [x] 3. a16z filter: skip `Fundraise` tag, "Investing in / Incubating / Welcome / Meet the / Announcing / Introducing" titles, titles mentioning "a16z" (28/100 sampled posts dropped, all announcements)
- [x] 4. Seen-ledger `data/why_seen.json` (shared helpers moved to `seen_ledger.py`; How unchanged)
  - [x] Key = canonical RSS guid → link → `source:title`; strips `access_token`/tracking, keeps `?p=ID`
  - [x] Written only after a successful Slack post; summarised items only (failures retried)
  - [x] Workflow commits the ledger
  - [x] Seeded with the 2 Stratechery items posted on Nov 7 (no tokens in file)
- [x] 4b. Month-only dates ("Nov 2026") stay in window all month; shown as "Nov 2026"
- [x] 5. Full per-feed funnel with denominators: `N fetched › N in window › N new › N summarised (notes)`



## Phase 3 — Robustness ✅

- [x] ~~Split digests over ~22 items across multiple Slack messages (50-block limit)~~ — shared `split_into_messages()` / `post_payloads()` in `common.py`, used by What, Why, and How: ≤50 blocks / ~30k chars per message, cards never split, `(i/N)` headers, funnel + footer on the last message



## Open decisions

- [x] Stratechery links carry `?access_token=<JWT>` into Slack — **keep** (sole-member channel); ledger stays token-free
- [x] Fetch window stretches to last successful post − 1 day after a missed run (`fetch_cutoff`: floor 8, cap 30). Empty ledger still uses the 8-day floor
- [x] Fold zero-activity feeds into one funnel line (`📡 quiet: … (N fetched, 0 in window)`); failed fetches stay on their own line
- [ ] Parked polish: "Read letter" label for DeepLearning.AI
- [x] a16z "Charts of the Week" roundups — kept in the date window, not sent to the LLM; Slack shows title and date only



## Notes

- Pre-existing, out of scope: `models.py` `WhySignal` / `HowSignal` dataclasses are unused.
- Local venv is Python 3.9; CI runs 3.11 — avoid 3.10+ syntax.
- ~~a16z items from Nov 2–5 were never posted~~ — recovered by the Nov 8 manual run.
- ~~a16z inputs were only ~330 tokens each~~ → full bodies now fetched (a16z/Ben Evans pages ~11–20k chars, DL.AI letters ~2.7k).

