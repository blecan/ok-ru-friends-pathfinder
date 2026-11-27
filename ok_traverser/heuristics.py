"""
Input normalization, commercial account pruning, Anti-Hub friend count
extraction, and A* heuristic scoring (surnames, maiden names, geo/education).
"""

import html as html_lib
import re
from typing import Optional, Set

COMMERCIAL_STOPWORDS = {
    "потолок", "потолки", "ремонт", "ногти", "маникюр", "педикюр", "авто", "магазин",
    "одежда", "тур", "туры", "работа", "фотограф", "скидки", "ноутбук",
    "ноутбуки", "светофор", "мебель", "окна", "двери", "ресницы", "брови",
    "тату", "цветы", "доставка", "недвижимость", "риэлтор", "грузоперевозки",
    "кухни", "торты", "массаж", "косметика", "запчасти", "автосервис",
    "шина", "шины", "квартира", "квартиры", "букет", "букеты", "шары",
    "кератин", "визажист", "парикмахер", "пошив", "шторы", "обувь", "баня",
    "сауна", "такси", "грузчики", "диваны", "пицца", "суши", "кафе", "банкет",
    "вещи", "шоурум", "косметолог", "юрист", "адвокат", "стоматолог",
}

COMMERCIAL_ROOTS = (
    "натяжн", "потолк", "ноутбук", "светофор",
    "маникюр", "педикюр", "автозапчаст", "грузоперевоз",
)

BAD_NAME_TOKENS = {
    "одноклассники", "пользователь", "главная", "друзья", "профиль", "закрытый",
}

GEO_EDU_STOPWORDS = {
    "россия", "украина", "беларусь", "казахстан", "город", "село",
    "школа", "лицей", "гимназия", "колледж", "университет", "институт",
    "родился", "родилась", "живет", "живёт", "учился", "училась", "окончил", "окончила",
}


def clean_user_input(input_val: str) -> str:
    """
    Extracts a profile numerical ID or vanity username from an OK.ru URL or raw string.

    Handles:
      - Raw numeric IDs: '1234567890'
      - Mobile query links: 'https://m.ok.ru/dk?st.cmd=friendMain&st.friendId=123456'
      - Profile URLs: 'https://ok.ru/profile/1234567890/friends'
      - Vanity URLs: 'https://ok.ru/john.doe/friends' -> 'john.doe'
    """
    s = str(input_val).strip()
    if not s:
        return ""
    if s.isdigit():
        return s

    # Priority 1: st.friendId=12345 in query parameters
    q_match = re.search(r"st\.friendId=(\d+)", s)
    if q_match:
        return q_match.group(1)

    # Priority 2: /profile/<id_or_slug>
    p_match = re.search(r"ok\.ru/profile/([a-zA-Z0-9._-]+)", s)
    if p_match:
        return p_match.group(1).rstrip("/")

    # Priority 3: ok.ru/<slug> excluding /dk
    v_match = re.search(r"ok\.ru/([a-zA-Z0-9._-]+)", s)
    if v_match:
        slug = v_match.group(1).rstrip("/")
        if slug.lower() not in {"dk", "feed", "profile", "friends", "messages"}:
            return slug

    return s


def build_public_profile_url(uid: str, mobile: bool = False) -> str:
    """Builds canonical profile URL for either numeric IDs or vanity slugs."""
    host = "m.ok.ru" if mobile else "ok.ru"
    u = str(uid).strip()
    if u.isdigit():
        return f"https://{host}/profile/{u}"
    return f"https://{host}/{u}"


def is_commercial_account(name: str) -> bool:
    """Returns True if the profile name indicates a commercial service or shop."""
    if not name:
        return False
    clean = name.lower()
    words = re.findall(r"[а-яёa-z]+", clean)
    for w in words:
        if w in COMMERCIAL_STOPWORDS:
            return True
    return any(root in clean for root in COMMERCIAL_ROOTS)


def extract_total_friends_count(html_text: str) -> Optional[int]:
    """
    Extracts total friend count from mobile or desktop HTML headers
    for early Anti-Hub detection on page 1, ignoring <script>/<style> payloads.
    """
    if not html_text:
        return None
    # Check explicit JSON counter attributes first
    json_pat = r'["\'](?:friendCount|friendsCount|totalFriends|friends_count)["\']\s*:\s*(\d+)'
    jm = re.search(json_pat, html_text, re.IGNORECASE)
    if jm:
        val = int(jm.group(1))
        if 1 <= val <= 100000:
            return val

    # Strip script/style blocks before matching visible HTML friend counters
    visible_html = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    patterns = [
        r'<[^>]*class=["\'][^"\']*nav-side_count[^"\']*["\'][^>]*>([\d\s\xa0]+)<',
        r'(?:друзь(?:я|ей)|все)\s*<[^>]*class=["\'][^"\']*(?:counter|badge|count|nav-side_count)[^"\']*["\'][^>]*>([\d\s\xa0]+)<',
        r'<[^>]*class=["\'][^"\']*(?:counter|badge|count|nav-side_count)[^"\']*["\'][^>]*>([\d\s\xa0]+)<(?:[^>]*>)?\s*(?:друзь(?:я|ей)|все)',
        r'(?:^|[\s>(])(\d[\d\s\xa0]{1,7})\s*(?:друг|друга|друзей)\b',
        r'Друзья\s*(?:</span>)?\s*\((\d[\d\s\xa0]{0,7})\)',
        r'st\.cmd=userFriend[^>]*>[^<]*?\((\d[\d\s\xa0]+)\)',
    ]
    for pat in patterns:
        m = re.search(pat, visible_html, re.IGNORECASE)
        if m:
            raw = re.sub(r"[\s\xa0]", "", m.group(1))
            if raw.isdigit():
                val = int(raw)
                if 1 <= val <= 100000:
                    return val
    return None


def extract_surnames(full_name: str) -> Set[str]:
    """
    Extracts surname stems and maiden surnames enclosed in parentheses.
    In OK.ru, maiden names are conventionally written as 'Name Surname (Maiden)'.
    """
    res: Set[str] = set()
    if not full_name:
        return res

    brackets = re.findall(r"\(([^)]+)\)", full_name)
    for b in brackets:
        for w in re.findall(r"[а-яёa-z]{4,}", b.lower()):
            if w not in BAD_NAME_TOKENS:
                res.add(w)

    clean = re.sub(r"\(.*?\)", "", full_name)
    for w in re.findall(r"[а-яёa-z]{4,}", clean.lower()):
        if not any(bad in w for bad in BAD_NAME_TOKENS):
            res.add(w)
    return res


def extract_geo_edu_tokens(html_text: str) -> Set[str]:
    """
    Extracts city, school, and university tokens from a profile HTML page
    to enhance heuristic scoring beyond surnames (colleagues, classmates, neighbors).
    """
    tokens: Set[str] = set()
    if not html_text:
        return tokens

    decoded = html_lib.unescape(html_text)
    patterns = [
        r'(?:Жив[её]т\s+в|Город|Из\s+города)\s*[:\-]?\s*(?:<[^>]+>)*\s*([А-ЯЁA-Z][а-яёa-zA-Z\-]{2,25})',
        r'(?:Школа|Гимназия|Лицей|Колледж|ВУЗ|Университет|Институт)\s*(?:№\s*\d+|«[^»]+»|"[^"]+"|[А-ЯЁA-Z][а-яёa-zA-Z0-9\-\s]{2,30})',
    ]
    for pat in patterns:
        for match in re.finditer(pat, decoded, re.IGNORECASE):
            snippet = re.sub(r"<[^>]+>", " ", match.group(0)).lower()
            for word in re.findall(r"[а-яёa-z0-9\-]{2,}", snippet):
                if word not in GEO_EDU_STOPWORDS and (len(word) >= 4 or word.isdigit()):
                    tokens.add(word)
    return tokens


def compute_priority_score(
    name: str,
    target_surnames: Set[str],
    known_friends_count: Optional[int] = None,
    candidate_geo: Optional[Set[str]] = None,
    target_geo: Optional[Set[str]] = None,
) -> int:
    """
    Computes an A*-style priority score for candidate ordering in the BFS frontier:
      - +500: Direct maiden surname match inside parentheses
      - +350: Surname stem overlap with opposite frontier
      - +120: Shared city / school / university token overlap
      - +100: Organic personal circle (20-250 friends)
      - -150: Near-hub high degree (>350 friends)
    """
    if not name:
        return 0
    score = 0
    name_lower = name.lower()

    # 1. Maiden surname inside parentheses
    brackets = re.findall(r"\(([^)]+)\)", name_lower)
    if brackets:
        score += 30
        for b in brackets:
            for ts in target_surnames:
                if len(ts) >= 4 and (ts in b or b in ts):
                    score += 500

    # 2. Surname stem match
    clean_words = set(re.findall(r"[а-яёa-z]{4,}", re.sub(r"\(.*?\)", "", name_lower)))
    for w in clean_words:
        for ts in target_surnames:
            if len(ts) >= 4 and (ts in w or w in ts):
                score += 350

    # 3. Geo / school / university overlap
    if candidate_geo and target_geo:
        overlap = candidate_geo.intersection(target_geo)
        if overlap:
            score += min(len(overlap), 3) * 120

    # 4. Organic circle size bonus
    if known_friends_count is not None:
        if 20 <= known_friends_count <= 250:
            score += 100
        elif known_friends_count > 350:
            score -= 150

    return score
