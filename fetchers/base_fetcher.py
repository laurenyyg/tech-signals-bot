from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseFetcher(ABC):
    @abstractmethod
    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch and return a list of raw news item dicts."""
