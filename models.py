from dataclasses import dataclass
from typing import List


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
