"""Shared configuration constants for the Tech Signals Bot."""
from typing import List

SUMMARY_TRUNCATE_CHARS: int = 1500

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
