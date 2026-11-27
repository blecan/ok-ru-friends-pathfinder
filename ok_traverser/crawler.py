"""
Resilient mobile OK.ru network crawler with strict HTTP/anti-bot detection,
partial-pagination protection, quick hub probing, and optional implicit ties fallback.
"""

import html as html_lib
import random
import re
import time
from typing import Dict, List, Optional, Tuple

from .auth import OKBrowserSession, SessionHandle, SessionPool
from .config import Config
from .heuristics import build_public_profile_url, extract_geo_edu_tokens, extract_total_friends_count
from .models import ProfileResult
from .storage import SQLiteGraphCache


TAG_FRIEND_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']*st\.cmd=friendMain[^"\']*)["\'][^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

TAG_ANY_PROFILE_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']*(?:/profile/|st\.friendId=)(\d+)[^"\']*)["\'][^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

DELETED_MARKERS = (
    "страница удалена",
    "профиль удален",
    "профиль удалён",
    "профиль заблокирован",
    "пользователь не найден",
    "страница заблокирована",
)

CAPTCHA_MARKERS = (
    "showcaptcha",
    "g-recaptcha",
    "smartcaptcha",
    "st.cmd=captcha",
    "введите символы с картинки",
)


class OKCrawler:
    """
    Network crawler backed by SessionPool (account + proxy rotation)
    and SQLiteGraphCache (O(1) WAL persistence).
    """

    def __init__(self, session_pool: SessionPool, cache: SQLiteGraphCache):
        self.pool = session_pool
        self.cache = cache

    @staticmethod
    def _is_login_or_anon(url: str, body: str, headers: dict) -> bool:
        u = url.lower()
        return (
            "st.cmd=login" in u
            or "st.cmd=anonym" in u
            or "/cdk/st.cmd/main" in u
            or u.rstrip("/") in ("https://m.ok.ru", "http://m.ok.ru")
            or "/login" in u
            or "anonymregistration" in body.lower()
            or 'name="st.password"' in body
            or 'action="/dk?st.cmd=login"' in body
            or "x-sign-in-layer" in {k.lower(): v for k, v in headers.items()}
        )

    @staticmethod
    def _is_captcha(url: str, body: str) -> bool:
        u = url.lower()
        b = body.lower()
        return any(m in u or m in b for m in CAPTCHA_MARKERS)

    @staticmethod
    def _is_deleted(status_code: int, body: str) -> bool:
        if status_code in (404, 410):
            return True
        b = body.lower()
        return any(m in b for m in DELETED_MARKERS)

    @staticmethod
    def _is_private(url: str, body: str, parsed_friend_links_count: int) -> bool:
        u = url.lower()
        if "friendrestricted" in u or "st.cmd=friendrestricted" in u:
            return True
        # Only inspect privacy banners when NO real friend links were found,
        # avoiding false positives from friend names containing similar substrings.
        if parsed_friend_links_count == 0:
            b = body.lower()
            if (
                "информация недоступна" in b
                or "профиль пользователя закрыт" in b
                or "доступ к профилю ограничен" in b
                or re.search(r'<div[^>]*class=["\'][^"\']*restricted[^"\']*["\']', b)
            ):
                return True
        return False

    def check_session(self, handle: Optional[SessionHandle] = None) -> bool:
        """
        Strictly verifies authenticated session by querying userMain / feed.
        NEVER defaults to True on ambiguous responses, timeouts, or 429s.
        """
        target = handle or self.pool.acquire_handle()
        if target is None:
            return False

        try:
            target.limiter.acquire()
            r = target.http.get("https://m.ok.ru/dk?st.cmd=userMain", timeout=12, allow_redirects=True)
            if r.status_code != 200:
                return False
            if self._is_login_or_anon(r.url, r.text, r.headers) or self._is_captcha(r.url, r.text):
                return False

            my_id = target.slot.user_id or OKBrowserSession.get_current_user_id()
            if not my_id:
                m_uid = re.search(r'(?:data-current-user-id|currentuserid|userid)=["\'](\d+)', r.text)
                if m_uid:
                    my_id = m_uid.group(1)
            if my_id and my_id in r.text:
                if not target.slot.user_id:
                    target.slot.user_id = my_id
                return True

            if (
                "data-current-user-id" in r.text
                or "currentuserid=" in r.text
                or "st.cmd=userMain" in r.text
            ) and ("st.cmd=logout" in r.text.lower() or "выйти" in r.text.lower() or 'class="mntl"' in r.text):
                return True

            return False
        except Exception:
            return False

    def _build_friends_url(self, uid: str) -> str:
        return f"{build_public_profile_url(uid, mobile=True)}/friends"

    def _build_profile_url(self, uid: str) -> str:
        return build_public_profile_url(uid, mobile=True)

    def _fetch_profile_name_and_geo(self, handle: SessionHandle, uid: str) -> Tuple[str, Optional[int], List[str]]:
        """Fetches mobile profile page using pooled session (same UA/cookies) to get name, count, and geo/edu tokens."""
        name = f"ID {uid}"
        count = None
        geo_tokens: List[str] = []
        try:
            handle.limiter.acquire()
            r = handle.http.get(self._build_profile_url(uid), timeout=10, allow_redirects=True)
            if r.status_code == 200 and not self._is_login_or_anon(r.url, r.text, r.headers):
                m = re.search(r"<title>(.*?)</title>", r.text, re.DOTALL | re.IGNORECASE)
                if m:
                    raw_t = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
                    clean_t = re.sub(r"\s*[\|\-—]\s*Одноклассники.*$", "", raw_t, flags=re.IGNORECASE).strip()
                    if clean_t and not any(bad in clean_t.lower() for bad in ("одноклассники", "вход", "регистрация")):
                        name = clean_t
                count = extract_total_friends_count(r.text)
                geo_tokens = sorted(extract_geo_edu_tokens(r.text))
        except Exception:
            pass
        return name, count, geo_tokens

    def _fetch_implicit_ties(self, handle: SessionHandle, uid: str) -> List[Dict[str, str]]:
        """
        Optional --deep-osint fallback for private friends lists:
        extracts publicly visible commenters/reactors on public profile wall/photos,
        strictly filtering out the authenticated user's own navigation/header links.
        """
        ties: Dict[str, Dict[str, str]] = {}
        self_uid = handle.slot.user_id or OKBrowserSession.get_current_user_id(handle.http)
        if self_uid and not handle.slot.user_id:
            handle.slot.user_id = self_uid
        try:
            handle.limiter.acquire()
            r = handle.http.get(self._build_profile_url(uid), timeout=10, allow_redirects=True)
            if r.status_code != 200 or self._is_login_or_anon(r.url, r.text, r.headers):
                return []
            # Also detect logged-in user ID directly from page telemetry if slot.user_id was unknown
            if not self_uid:
                m_self = re.search(r'(?:data-current-user-id|currentuserid|userid)=["\'](\d+)', r.text)
                if m_self:
                    self_uid = m_self.group(1)
                    handle.slot.user_id = self_uid

            for m in TAG_ANY_PROFILE_RE.finditer(r.text):
                href, fid, raw_inner = m.group(1), m.group(2), m.group(3)
                if fid == uid or (self_uid and fid == self_uid):
                    continue
                hl = href.lower()
                il = raw_inner.lower()
                if any(nav in hl for nav in ("st.cmd=userpage", "navsidemenu", "toolbar", "/friends", "st.cmd=friendfriends", "_aid=oldmenu")):
                    continue
                if any(nav in il for nav in ("перейти в свой профиль", "toolbar_accounts", "adaptive_menu")):
                    continue

                raw_name = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_inner)).strip()
                if len(raw_name) > 2:
                    rl = raw_name.lower()
                    if rl not in {"класс", "комментировать", "поделиться", "ответить", "друзья", "гости", "группы", "фото"} and not any(
                        bad in rl for bad in ("подарок", "написать сообщение", "добавить в друзья")
                    ):
                        if fid not in ties:
                            ties[fid] = {
                                "id": fid,
                                "name": raw_name,
                                "url": build_public_profile_url(fid),
                                "relation_type": "implicit_tie",
                            }
        except Exception:
            pass
        return list(ties.values())

    def get_user_data(
        self,
        uid: str,
        max_pages: int = Config.max_pages_per_profile,
        max_friends: int = Config.max_friends_limit,
        known_name: Optional[str] = None,
        hub_probe_only: bool = False,
        deep_osint: bool = False,
    ) -> ProfileResult:
        """
        Fetches profile friends and metadata from SQLite cache or mobile OK.ru.
        Guarantees partial pagination failures NEVER poison the persistent cache.
        """
        uid = str(uid)
        cached = self.cache.get_profile(uid, max_friends=max_friends)
        if cached is not None:
            if known_name and (cached.name.startswith("ID ") or "одноклассники" in cached.name.lower()):
                cached.name = known_name
                self.cache.update_profile_name(uid, known_name)
            return cached

        handle = self.pool.acquire_handle()
        if handle is None:
            return ProfileResult(uid=uid, name=known_name or f"ID {uid}", status="rate_limited", partial=True)

        geo_tokens: List[str] = []
        total_cnt: Optional[int] = None
        if known_name and not known_name.startswith("ID ") and not any(bad in known_name.lower() for bad in ("одноклассники", "выйти", "вход")):
            user_name = known_name
        else:
            user_name, total_cnt, geo_tokens = self._fetch_profile_name_and_geo(handle, uid)

        if total_cnt and total_cnt > max_friends:
            res = ProfileResult(
                uid=uid,
                name=user_name,
                friends=[],
                status="hub",
                is_hub=True,
                total_friends_count=total_cnt,
                geo_tokens=geo_tokens,
            )
            self.cache.save_profile(res)
            return res

        friends: Dict[str, Dict[str, str]] = {}
        is_private = False
        is_deleted = False
        is_hub = False
        aborted_status = None
        early_probe_exit = False
        page = 1
        effective_max_pages = max(max_pages, (max_friends // 18) + 2) if hub_probe_only else max_pages
        current_url = self._build_friends_url(uid)

        while current_url and page <= effective_max_pages:
            try:
                if page > 1:
                    time.sleep(random.uniform(Config.request_delay_min, Config.request_delay_max))
                handle.limiter.acquire()
                handle.slot.requests_made += 1
                resp = handle.http.get(current_url, timeout=12, allow_redirects=True)
            except Exception:
                aborted_status = "network_error"
                break

            if resp.status_code == 429:
                self.pool.mark_cooldown(handle, Config.cooldown_seconds)
                aborted_status = "rate_limited"
                break

            if self._is_deleted(resp.status_code, resp.text):
                is_deleted = True
                break

            if resp.status_code >= 400:
                aborted_status = "network_error"
                break

            if self._is_captcha(resp.url, resp.text):
                self.pool.mark_cooldown(handle, Config.cooldown_seconds * 2)
                aborted_status = "captcha"
                break

            if self._is_login_or_anon(resp.url, resp.text, resp.headers):
                self.pool.mark_cooldown(handle, Config.cooldown_seconds)
                aborted_status = "auth_expired"
                break

            content_text = resp.text

            # Extract friend links first so privacy detection doesn't misfire on names
            page_new_count = 0
            for m in TAG_FRIEND_RE.finditer(content_text):
                href, raw_text = m.group(1), m.group(2)
                clean_text = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_text)).strip()
                id_m = re.search(r"st\.friendId=(\d+)", href)
                if id_m and len(clean_text) > 1:
                    fid = id_m.group(1)
                    if fid != uid and not any(bad in clean_text.lower() for bad in ("подарок", "сообщение", "добавить", "отозвать")):
                        if fid not in friends:
                            friends[fid] = {
                                "id": fid,
                                "name": clean_text,
                                "url": f"https://ok.ru/profile/{fid}",
                            }
                            page_new_count += 1

            if page == 1:
                if not geo_tokens:
                    geo_tokens = sorted(extract_geo_edu_tokens(content_text))
                cnt = extract_total_friends_count(content_text)
                if cnt is not None and cnt >= len(friends):
                    total_cnt = cnt
                    if cnt > max_friends:
                        is_hub = True
                        break

                if self._is_private(resp.url, content_text, len(friends)):
                    is_private = True
                    break

                # Fallback name extraction from title if name wasn't known
                if user_name.startswith("ID "):
                    title_m = re.search(r"<title>(.*?)</title>", content_text, re.DOTALL | re.IGNORECASE)
                    if title_m:
                        t_clean = html_lib.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
                        t_clean = re.sub(r"\s*[-—|]\s*Друзья.*$", "", t_clean, flags=re.IGNORECASE).strip()
                        if t_clean and not any(bad in t_clean.lower() for bad in ("друзья", "одноклассники", "вход")):
                            user_name = t_clean

            if len(friends) >= max_friends:
                is_hub = True
                break

            if hub_probe_only and (total_cnt is not None or page_new_count < 18):
                if total_cnt is not None and len(friends) < total_cnt:
                    early_probe_exit = True
                break

            # Next pagination page (supports both classic static links and unified infinite-scroll ?st.page=N)
            text_lower = content_text.lower()
            page_matches = re.findall(
                r'href=["\']([^"\']*st\.page=' + str(page + 1) + r'[^"\']*)["\']',
                content_text,
                re.IGNORECASE,
            )
            if page_matches and page_new_count > 0:
                next_href = page_matches[0].replace("&amp;", "&")
                current_url = next_href if next_href.startswith("http") else f"https://m.ok.ru{next_href}"
                page += 1
            elif page_new_count >= 18 and (
                "friendspagemrb" in text_lower
                or "loader-container" in text_lower
                or "friendfriend" in text_lower
            ):
                base_friends_url = self._build_friends_url(uid)
                sep = "&" if "?" in base_friends_url else "?"
                current_url = f"{base_friends_url}{sep}st.page={page + 1}"
                page += 1
            else:
                break

        if total_cnt is not None and total_cnt < len(friends):
            total_cnt = None

        if aborted_status is not None:
            return ProfileResult(
                uid=uid,
                name=user_name,
                friends=list(friends.values()),
                status=aborted_status,
                total_friends_count=total_cnt,
                geo_tokens=geo_tokens,
                partial=True,
            )

        friends_list = list(friends.values())

        # Optional Deep OSINT fallback for private profiles
        if is_private and deep_osint and not hub_probe_only:
            implicit = self._fetch_implicit_ties(handle, uid)
            if implicit:
                friends_list = implicit

        status = "deleted" if is_deleted else ("private" if is_private else ("hub" if is_hub else "ok"))
        res = ProfileResult(
            uid=uid,
            name=user_name,
            friends=friends_list,
            status=status,
            is_private=is_private,
            is_deleted=is_deleted,
            is_hub=is_hub,
            total_friends_count=total_cnt,
            geo_tokens=geo_tokens,
            partial=False,
        )

        # Persist complete results (avoid writing truncated probes when early_probe_exit skipped pages)
        if not early_probe_exit or is_hub or is_private or is_deleted:
            self.cache.save_profile(res)

        return res
