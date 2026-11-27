"""
Data models for ok-ru-friends-pathfinder.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

CrawlStatus = Literal[
    "ok",
    "private",
    "deleted",
    "hub",
    "rate_limited",
    "captcha",
    "auth_expired",
    "network_error",
]


@dataclass
class FriendRef:
    """Lightweight representation of a friend edge parsed from HTML."""
    id: str
    name: str
    url: str
    relation_type: str = "friend"  # "friend" or "implicit_tie"

    def to_dict(self) -> Dict[str, str]:
        d = {"id": self.id, "name": self.name, "url": self.url}
        if self.relation_type != "friend":
            d["relation_type"] = self.relation_type
        return d


@dataclass
class ProfileResult:
    """Result of fetching/loading a single OK.ru profile."""
    uid: str
    name: str
    friends: List[Dict[str, str]] = field(default_factory=list)
    status: CrawlStatus = "ok"
    is_private: bool = False
    is_deleted: bool = False
    is_hub: bool = False
    total_friends_count: Optional[int] = None
    geo_tokens: List[str] = field(default_factory=list)
    from_cache: bool = False
    partial: bool = False


@dataclass
class AccountSlot:
    """Represents one session + optional proxy slot in the session pool."""
    name: str
    cookies: Dict[str, str]
    proxy: Optional[str] = None
    user_id: Optional[str] = None
    cooldown_until: float = 0.0
    dead: bool = False
    requests_made: int = 0
