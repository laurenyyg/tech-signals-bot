from dataclasses import dataclass, field
from typing import List


@dataclass
class NewsSignal:
    macro_event: str
    core_fact: str
    eli5: str
    investment_impact: str
    source_urls: List[str] = field(default_factory=list)


@dataclass
class WhySignal:
    title: str
    author: str
    summary: str
    is_deep_read: bool
    source_url: str


@dataclass
class HowSignal:
    title: str
    source: str
    tech_stack: List[str]
    workflow_steps: List[str]
    roi_impact: str
    url: str
