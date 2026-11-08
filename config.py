"""Shared configuration constants for the Tech Signals Bot."""
from typing import List

SUMMARY_TRUNCATE_CHARS: int = 1500

# How profile
HOW_DAYS_FILTER: int = 3  # Fetch window; must stay well below HOW_SEEN_RETENTION_DAYS.
HOW_SEEN_RETENTION_DAYS: int = 60
HOW_BODY_MAX_CHARS: int = 12000

DEEP_READ_KEYWORDS: List[str] = [
    "artificial intelligence",
    "moat",
    "paradigm shift",
    "ai productivity",
    "compute",
    "capital expenditure",
    "inference cost",
    "scaling laws",
    "agentic workflows",
    "open weights",
    "decentralization",
    "regulatory capture",
]

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
