"""
Configuration management for ok-ru-friends-pathfinder.
Loads settings safely from .env and environment variables with validation.
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional


def load_env(filepath: str = ".env") -> None:
    """Loads environment variables from a .env file with stdlib fallback."""
    try:
        import dotenv
        dotenv.load_dotenv(filepath)
        return
    except ImportError:
        pass

    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(key: str) -> List[str]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


load_env()


@dataclass
class Settings:
    """Typed application settings."""
    auth_state_file: str = field(default_factory=lambda: os.getenv("OK_AUTH_STATE_FILE", "ok_auth_state.json").strip() or "ok_auth_state.json")
    cookies_file: str = field(default_factory=lambda: os.getenv("OK_COOKIES_FILE", "ok_cookies.json").strip() or "ok_cookies.json")
    sessions_file: str = field(default_factory=lambda: os.getenv("OK_SESSIONS_FILE", "sessions.json").strip() or "sessions.json")
    db_file: str = field(default_factory=lambda: os.getenv("OK_DB_FILE", "ok_graph_cache.db").strip() or "ok_graph_cache.db")
    legacy_json_cache_file: str = field(default_factory=lambda: os.getenv("OK_CACHE_FILE", "ok_friends_cache.json").strip() or "ok_friends_cache.json")
    checkpoint_file: str = field(default_factory=lambda: os.getenv("OK_CHECKPOINT_FILE", "ok_search_checkpoint.json").strip() or "ok_search_checkpoint.json")
    report_file: str = field(default_factory=lambda: os.getenv("OK_REPORT_FILE", "ok_path_result.html").strip() or "ok_path_result.html")

    default_user_id: Optional[str] = field(default_factory=lambda: os.getenv("OK_USER_ID", "").strip() or None)
    excluded_ids: List[str] = field(default_factory=lambda: _env_list("OK_EXCLUDED_IDS"))
    proxy_url: Optional[str] = field(default_factory=lambda: os.getenv("OK_PROXY", "").strip() or None)

    max_depth: int = field(default_factory=lambda: _env_int("MAX_DEPTH", 5))
    max_workers: int = field(default_factory=lambda: _env_int("MAX_WORKERS", 2))
    max_friends_limit: int = field(default_factory=lambda: _env_int("MAX_FRIENDS_LIMIT", 400))
    safe_layer_cap: int = field(default_factory=lambda: _env_int("SAFE_LAYER_CAP", 150))
    cache_ttl_days: int = field(default_factory=lambda: _env_int("CACHE_TTL_DAYS", 14))

    request_delay_min: float = field(default_factory=lambda: _env_float("REQUEST_DELAY_MIN", 1.0))
    request_delay_max: float = field(default_factory=lambda: _env_float("REQUEST_DELAY_MAX", 1.5))
    cooldown_seconds: int = field(default_factory=lambda: _env_int("COOLDOWN_SECONDS", 30))
    max_pages_per_profile: int = field(default_factory=lambda: _env_int("MAX_PAGES_PER_PROFILE", 40))
    max_pages_endpoints: int = field(default_factory=lambda: _env_int("MAX_PAGES_ENDPOINTS", 200))
    deep_osint: bool = field(default_factory=lambda: _env_bool("DEEP_OSINT", False))


Config = Settings()

# Consistent modern mobile User-Agent used across all requests on m.ok.ru.
# Avoids desktop Accept negotiation (image/avif without Sec-CH-UA-Mobile) which causes OK.ru
# to 301-redirect m.ok.ru/profile/.../friends?st.page=N -> ok.ru/... (dropping ?st.page=N).
MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.6613.127 Mobile Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}
