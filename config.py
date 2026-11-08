"""Shared configuration constants for the Tech Signals Bot."""
from typing import List

SUMMARY_TRUNCATE_CHARS: int = 1500

# Why profile: output budget per item (one Sonnet call per item, thinking included).
WHY_ITEM_MAX_TOKENS: int = 8000
# Floor window (DAYS_FILTER in why_fetcher.py). Stretches back to the last
# successful post minus WHY_DAYS_SLACK after a missed run, capped at WHY_DAYS_MAX.
WHY_DAYS_SLACK: int = 1
WHY_DAYS_MAX: int = 30
# Must exceed WHY_DAYS_MAX plus a full month (month-only dates stay in window all month).
WHY_SEEN_RETENTION_DAYS: int = 60
# Text sent to the LLM per item (What keeps SUMMARY_TRUNCATE_CHARS).
WHY_BODY_MAX_CHARS: int = 12000
# Sources whose feeds carry only an excerpt, so the article page is downloaded.
# Never add stratechery: pages are paywalled and its links carry the feed owner's token.
WHY_BODY_FETCH_SOURCES: List[str] = ["a16z", "ben-evans", "deeplearning-ai"]

# a16z posts that are announcements rather than essays. Prefixes are case-insensitive;
# any title mentioning "a16z" is the firm announcing itself (fellowships, funds, events).
A16Z_SKIP_TAGS: List[str] = ["Fundraise"]
A16Z_SKIP_TITLE_PREFIXES: List[str] = [
    "investing in ",
    "incubating ",
    "welcome ",
    "meet the ",
    "announcing ",
    "introducing ",
]
A16Z_SKIP_TITLE_SUBSTRINGS: List[str] = ["a16z"]

# What profile: floor window (one day of slack for a late Sunday run). After a
# missed week the window stretches to last ledger processed_at minus WHAT_DAYS_SLACK,
# capped at WHAT_DAYS_MAX (one missed Sunday). Empty ledger uses the floor.
WHAT_DAYS_FILTER: int = 8
WHAT_DAYS_SLACK: int = 1
WHAT_DAYS_MAX: int = 14
WHAT_SEEN_RETENTION_DAYS: int = 60  # Must stay well above WHAT_DAYS_MAX.

# How profile
HOW_DAYS_FILTER: int = 3  # Fetch window; must stay well below HOW_SEEN_RETENTION_DAYS.
HOW_SEEN_RETENTION_DAYS: int = 60
HOW_BODY_MAX_CHARS: int = 12000

SSPAI_WHITELIST_AUTHORS: List[str] = [
    "张立行",
    "LOSSES",
    "玉树芝兰",
    "TomBen",
]

HOW_POSITIVE_KEYWORDS: List[str] = [
    "Prompt",
    "自动化",
    "工作流",
    "API",
    "脚本",
    "插件",
    "Cursor",
    "Claude",
    "AI",
    "SOP",
    "Agent",
]

HOW_NEGATIVE_KEYWORDS: List[str] = [
    "旅游",
    "指南",
    "好物",
    "开箱",
    "评测",
    "摄影",
    "美食",
    "咖啡",
]
