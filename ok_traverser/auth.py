"""
Authentication, cookie management, proxy/account pool, and token-bucket rate limiting.
"""

import json
import os
import re
import threading
import time
from typing import Dict, List, Optional

import requests

from .config import Config, MOBILE_HEADERS
from .models import AccountSlot

try:
    from curl_cffi import requests as curl_requests  # type: ignore
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False


def build_http_session(cookies: Dict[str, str], proxy: Optional[str] = None):
    """
    Creates an HTTP session with connection pooling and consistent mobile headers.
    Uses curl_cffi (Chrome TLS impersonation) if installed, falling back to requests.Session.
    """
    if HAS_CURL_CFFI and curl_requests is not None:
        session = curl_requests.Session(impersonate="chrome124")
    else:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=1)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    session.headers.update(MOBILE_HEADERS)
    if cookies:
        session.cookies.update(cookies)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


class TokenBucketRateLimiter:
    """Thread-safe token bucket limiter enforcing max sustained RPS per account slot."""

    def __init__(self, rate_per_sec: float = 0.85, capacity: float = 2.0):
        self.rate = max(rate_per_sec, 0.1)
        self.capacity = max(capacity, 1.0)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_time = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait_time, 1.0))


class OKBrowserSession:
    """Manages Playwright interactive login and session file loading."""

    @staticmethod
    def is_logged_in() -> bool:
        return (
            os.path.exists(Config.auth_state_file)
            or os.path.exists(Config.cookies_file)
            or os.path.exists(Config.sessions_file)
        )

    DESKTOP_HINT_COOKIES = {
        "viewport", "_hover", "_breakpoint", "_touch",
        "mdd", "ok_w", "ok_h", "ok_dpi", "__pwa",
    }

    @classmethod
    def load_cookies(cls, cookies_path: str = Config.cookies_file, auth_state_path: str = Config.auth_state_file) -> Dict[str, str]:
        """
        Loads .ok.ru cookie jar while omitting desktop screen/viewport hints
        so m.ok.ru does not 301-redirect to desktop ok.ru.
        """
        cookies: Dict[str, str] = {}
        if os.path.exists(cookies_path):
            try:
                with open(cookies_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    cookies = {
                        str(k): str(v)
                        for k, v in raw.items()
                        if str(k) not in cls.DESKTOP_HINT_COOKIES
                    }
            except Exception:
                pass
        if not cookies and os.path.exists(auth_state_path):
            try:
                with open(auth_state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for c in data.get("cookies", []):
                    domain = c.get("domain", "")
                    name = str(c["name"])
                    if (not domain or "ok.ru" in domain) and name not in cls.DESKTOP_HINT_COOKIES:
                        cookies[name] = str(c["value"])
            except Exception:
                pass
        return cookies

    @staticmethod
    def get_current_user_id(session=None) -> Optional[str]:
        """Dynamically resolves current logged-in user ID from config, storage state, or userMain."""
        if Config.default_user_id and Config.default_user_id.isdigit():
            return Config.default_user_id

        if os.path.exists(Config.auth_state_file):
            try:
                with open(Config.auth_state_file, "r", encoding="utf-8") as f:
                    text_data = f.read()
                m = re.search(r'["\']uid["\']\s*:\s*["\']?(\d+)', text_data)
                if m:
                    return m.group(1)
            except Exception:
                pass

        if session is not None:
            try:
                r = session.get("https://m.ok.ru/dk?st.cmd=userMain", timeout=10, allow_redirects=True)
                m = re.search(r'(?:data-current-user-id|currentuserid|userid)=["\'](\d+)', r.text)
                if not m:
                    m = re.search(r'href=["\'](?:https://m\.ok\.ru)?/profile/(\d+)/friends', r.text)
                if m:
                    return m.group(1)
            except Exception:
                pass

        return None

    @staticmethod
    def interactive_login(headless: bool = False) -> bool:
        """Opens browser window for interactive login and writes cookies + storage_state."""
        print("\n" + "=" * 65)
        print("    АВТОРИЗАЦИЯ В ОДНОКЛАССНИКАХ (ЧЕРЕЗ БРАУЗЕР)")
        print("=" * 65)
        print("1. В открывшемся окне войдите в аккаунт OK.ru (логин/пароль/QR).")
        print("2. Дождитесь загрузки вашей ленты и вернитесь в консоль.")
        print("3. Нажмите ENTER (или закройте браузер вручную после входа).")
        print("=" * 65)

        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context()
                page = context.new_page()
                page.goto("https://ok.ru")

                try:
                    input("\nНажмите ENTER здесь после успешного входа в аккаунт... ")
                    context.storage_state(path=Config.auth_state_file)
                    cookies = context.cookies()
                    cookie_dict = {
                        c["name"]: c["value"]
                        for c in cookies
                        if not c.get("domain") or "ok.ru" in c.get("domain", "")
                    }
                    with open(Config.cookies_file, "w", encoding="utf-8") as f:
                        json.dump(cookie_dict, f, ensure_ascii=False, indent=2)
                    print(f"\n[+] Сессия сохранена ({len(cookie_dict)} cookies)!\n")
                    return True
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[!] Ошибка при запуске/закрытии браузера: {e}")
            return False


class SessionHandle:
    """Runtime wrapper binding an AccountSlot, HTTP session, and rate limiter."""

    def __init__(self, slot: AccountSlot):
        self.slot = slot
        self.http = build_http_session(slot.cookies, proxy=slot.proxy)
        rps = 1.0 / max((Config.request_delay_min + Config.request_delay_max) / 2.0, 0.5)
        self.limiter = TokenBucketRateLimiter(rate_per_sec=rps, capacity=2.0)

    def reset_cookies(self, new_cookies: Dict[str, str]) -> None:
        self.slot.cookies = dict(new_cookies)
        self.http.cookies.clear()
        self.http.cookies.update(self.slot.cookies)
        self.slot.dead = False
        self.slot.cooldown_until = 0.0


class SessionPool:
    """
    Manages one or multiple account+proxy sessions (`sessions.json` or default cookie files).
    Supports automatic rotation when a slot hits a rate-limit cooldown.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.handles: List[SessionHandle] = []
        self._rr_index = 0
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self.handles.clear()
            loaded_slots: List[AccountSlot] = []

            if os.path.exists(Config.sessions_file):
                try:
                    with open(Config.sessions_file, "r", encoding="utf-8") as f:
                        entries = json.load(f)
                    if isinstance(entries, list):
                        for idx, item in enumerate(entries, 1):
                            cookies = item.get("cookies") or {}
                            if isinstance(cookies, str) and os.path.exists(cookies):
                                cookies = OKBrowserSession.load_cookies(cookies_path=cookies, auth_state_path="")
                            if cookies:
                                loaded_slots.append(
                                    AccountSlot(
                                        name=item.get("name", f"account-{idx}"),
                                        cookies=cookies,
                                        proxy=item.get("proxy") or Config.proxy_url,
                                        user_id=item.get("user_id"),
                                    )
                                )
                except Exception:
                    pass

            if not loaded_slots:
                default_cookies = OKBrowserSession.load_cookies()
                if default_cookies:
                    loaded_slots.append(
                        AccountSlot(
                            name="primary",
                            cookies=default_cookies,
                            proxy=Config.proxy_url,
                            user_id=OKBrowserSession.get_current_user_id(),
                        )
                    )

            self.handles = [SessionHandle(s) for s in loaded_slots]

    def has_sessions(self) -> bool:
        with self._lock:
            return len(self.handles) > 0

    def acquire_handle(self) -> Optional[SessionHandle]:
        """Selects the next healthy session not in cooldown using round-robin."""
        with self._lock:
            if not self.handles:
                return None
            now = time.monotonic()
            n = len(self.handles)
            for offset in range(n):
                idx = (self._rr_index + offset) % n
                h = self.handles[idx]
                if not h.slot.dead and h.slot.cooldown_until <= now:
                    self._rr_index = (idx + 1) % n
                    return h
            return None

    def all_in_cooldown_or_dead(self) -> bool:
        with self._lock:
            if not self.handles:
                return True
            now = time.monotonic()
            return all(h.slot.dead or h.slot.cooldown_until > now for h in self.handles)

    def min_cooldown_remaining(self) -> float:
        with self._lock:
            now = time.monotonic()
            active_cd = [h.slot.cooldown_until - now for h in self.handles if not h.slot.dead and h.slot.cooldown_until > now]
            return max(0.0, min(active_cd)) if active_cd else 0.0

    def mark_cooldown(self, handle: SessionHandle, seconds: float) -> None:
        with self._lock:
            handle.slot.cooldown_until = time.monotonic() + seconds

    def mark_dead(self, handle: SessionHandle) -> None:
        with self._lock:
            handle.slot.dead = True

    def clear_all_cooldowns(self) -> None:
        with self._lock:
            for h in self.handles:
                h.slot.cooldown_until = 0.0
