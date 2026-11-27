"""
Modern, ergonomic CLI interface supporting:
  1. Subcommands: `search`, `resume`, `login`, `stats`, `cache`
  2. Backward-compatible top-level flags: `--start`, `--target`, `--login`, `--clear-cache`
  3. Interactive Dashboard & 1-click Profile Presets (Fast vs Deep OSINT vs Custom)
"""

import argparse
import os
import sys
from typing import Dict, List, Optional, Set, Tuple

from .auth import OKBrowserSession, SessionPool
from .config import Config
from .crawler import OKCrawler
from .engine import GraphSearchEngine
from .exporters import (
    export_graph_gexf,
    export_paths_csv,
    export_paths_json,
    render_html_report,
)
from .heuristics import clean_user_input
from .storage import CheckpointManager, SQLiteGraphCache


class UI:
    """Lightweight ANSI terminal styling with automatic Windows/NO_COLOR detection."""

    ENABLED = sys.stdout.isatty() and os.getenv("NO_COLOR") is None

    RESET = "\033[0m" if ENABLED else ""
    BOLD = "\033[1m" if ENABLED else ""
    DIM = "\033[2m" if ENABLED else ""
    ORANGE = "\033[38;5;208m" if ENABLED else ""
    GREEN = "\033[32m" if ENABLED else ""
    YELLOW = "\033[33m" if ENABLED else ""
    RED = "\033[31m" if ENABLED else ""
    CYAN = "\033[36m" if ENABLED else ""

    @classmethod
    def header(cls, title: str, subtitle: str = "") -> None:
        print(f"\n{cls.ORANGE}{cls.BOLD}╔══════════════════════════════════════════════════════════════════╗{cls.RESET}")
        print(f"{cls.ORANGE}{cls.BOLD}║{cls.RESET}  {cls.BOLD}{title:<64}{cls.RESET}{cls.ORANGE}{cls.BOLD}║{cls.RESET}")
        if subtitle:
            print(f"{cls.ORANGE}{cls.BOLD}║{cls.RESET}  {cls.DIM}{subtitle:<64}{cls.RESET}{cls.ORANGE}{cls.BOLD}║{cls.RESET}")
        print(f"{cls.ORANGE}{cls.BOLD}╚══════════════════════════════════════════════════════════════════╝{cls.RESET}")

    @classmethod
    def ok(cls, msg: str) -> None:
        print(f"{cls.GREEN}✓{cls.RESET} {msg}")

    @classmethod
    def warn(cls, msg: str) -> None:
        print(f"{cls.YELLOW}!{cls.RESET} {msg}")

    @classmethod
    def err(cls, msg: str) -> None:
        print(f"{cls.RED}✗{cls.RESET} {msg}")


def _resolve_exclusions(extra_raw: str, pool: SessionPool, crawler: OKCrawler) -> Set[str]:
    """Combines current user ID, .env OK_EXCLUDED_IDS, and CLI/user exclusions."""
    excluded: Set[str] = set()
    for x in Config.excluded_ids:
        cx = clean_user_input(x)
        if cx:
            excluded.add(cx)

    handle = pool.acquire_handle()
    my_id = (handle.slot.user_id if handle else None) or OKBrowserSession.get_current_user_id(
        handle.http if handle else None
    )
    if my_id:
        excluded.add(my_id)

    if extra_raw:
        for item in extra_raw.split(","):
            cx = clean_user_input(item)
            if cx:
                excluded.add(cx)
    return excluded


def _ensure_authenticated(pool: SessionPool, crawler: OKCrawler, allow_interactive: bool = True) -> bool:
    """Checks SessionPool health and optionally launches interactive Playwright login."""
    if pool.has_sessions() and crawler.check_session():
        return True

    if not allow_interactive:
        UI.err("Нет активной сессии OK.ru. Запустите `python ok_pathfinder.py login` или добавьте `sessions.json`.")
        return False

    UI.warn("Сессия OK.ru не найдена или истекла. Запускаем авторизацию через браузер...")
    if not OKBrowserSession.interactive_login():
        return False
    pool.reload()
    if pool.has_sessions() and crawler.check_session():
        UI.ok("Сессия успешно подтверждена!")
        return True
    UI.err("Не удалось подтвердить сессию после входа.")
    return False


def _print_and_export_results(
    paths: List[List[Dict[str, str]]],
    edges: List[Tuple[str, str]],
    user_meta: Dict[str, Dict[str, str]],
    excluded_ids: Set[str],
    export_formats: List[str],
    output_prefix: str = "ok_path_result",
    auto_open_html: bool = True,
) -> None:
    """Renders path tree in terminal and saves selected export formats."""
    for p_idx, path in enumerate(paths, 1):
        steps = max(0, len(path) - 1)
        print(f"\n{UI.CYAN}{UI.BOLD}═══ ЦЕПОЧКА #{p_idx} ({steps} рукопожатий) ═══{UI.RESET}")
        for i, node in enumerate(path):
            fc = f" | друзей: {node['friend_count']}" if "friend_count" in node else ""
            if i == 0:
                print(f"  {UI.GREEN}[СТАРТ]{UI.RESET}  {node['name']} (ID {node['id']}{fc})")
                print(f"           {UI.DIM}{node['url']}{UI.RESET}")
            elif i == len(path) - 1:
                print(f"  {UI.ORANGE}[ФИНИШ]{UI.RESET}  └──▶ {node['name']} (ID {node['id']}{fc})")
                print(f"                {UI.DIM}{node['url']}{UI.RESET}")
            else:
                print(f"  {UI.BOLD}[Звено {i}]{UI.RESET} └──▶ {node['name']} (ID {node['id']}{fc})")
                print(f"                {UI.DIM}{node['url']}{UI.RESET}")

    excl_labels = [f"ID {x}" for x in sorted(excluded_ids)]
    for fmt in export_formats:
        fmt_clean = fmt.strip().lower()
        if fmt_clean == "html":
            out = render_html_report(
                paths,
                excluded_names=excl_labels,
                filename=Config.report_file if output_prefix == "ok_path_result" else f"{output_prefix}.html",
                auto_open=auto_open_html,
            )
            UI.ok(f"HTML-отчёт: {out}")
        elif fmt_clean == "json":
            out = export_paths_json(paths, edges, user_meta, f"{output_prefix}.json")
            UI.ok(f"JSON-экспорт: {out}")
        elif fmt_clean == "csv":
            out = export_paths_csv(paths, f"{output_prefix}.csv")
            UI.ok(f"CSV-экспорт: {out}")
        elif fmt_clean == "gexf":
            out = export_graph_gexf(paths, edges, user_meta, f"{output_prefix}.gexf")
            UI.ok(f"GEXF-граф (Gephi/Maltego): {out}")


def _print_stats(cache: SQLiteGraphCache, pool: SessionPool, cp_mgr: CheckpointManager) -> None:
    stats = cache.get_stats()
    cp = cp_mgr.load()
    UI.header("СОСТОЯНИЕ СИСТЕМЫ И КЭША (SQLite WAL)", "ok-ru-friends-pathfinder")
    print(f"  {UI.BOLD}Профилей в базе:{UI.RESET}   {stats['profiles']} (хабы: {stats['hubs']}, закрытые: {stats['private']}, удалённые: {stats['deleted']})")
    print(f"  {UI.BOLD}Рёбер графа:{UI.RESET}       {stats['edges']}")
    print(f"  {UI.BOLD}Файл SQLite БД:{UI.RESET}    {os.path.abspath(cache.db_path)}")
    print(f"  {UI.BOLD}Пулов сессий:{UI.RESET}      {len(pool.handles)}")
    for h in pool.handles:
        proxy_label = h.slot.proxy or "direct"
        print(f"    • {h.slot.name} | proxy: {proxy_label} | user_id: {h.slot.user_id or 'auto'}")
    if Config.excluded_ids:
        print(f"  {UI.BOLD}Исключения (.env):{UI.RESET} {', '.join(Config.excluded_ids)}")
    if cp:
        pending_cnt = len((cp.get("active_layer_state") or {}).get("pending_uids", []))
        print(f"  {UI.YELLOW}Чекпоинт:{UI.RESET}          {cp.get('name_a')} ➔ {cp.get('name_b')} | слой {cp.get('depth')} | в очереди: {pending_cnt}")
    else:
        print(f"  {UI.BOLD}Чекпоинт:{UI.RESET}          нет активных")


def interactive_dashboard() -> int:
    """Ergonomic interactive dashboard with preset profiles and instant resume."""
    pool = SessionPool()
    cache = SQLiteGraphCache()
    cp_mgr = CheckpointManager()
    crawler = OKCrawler(pool, cache)
    engine = GraphSearchEngine(crawler, cp_mgr)

    # Resolve current user ID once at dashboard startup if cookies exist
    resolved_uid = OKBrowserSession.get_current_user_id()
    if not resolved_uid and pool.has_sessions():
        h0 = pool.acquire_handle()
        if h0 is not None:
            resolved_uid = h0.slot.user_id or OKBrowserSession.get_current_user_id(h0.http)
            if resolved_uid:
                h0.slot.user_id = resolved_uid

    try:
        while True:
            cp = cp_mgr.load()
            stats = cache.get_stats()
            session_count = len(pool.handles)
            h_curr = pool.handles[0] if pool.handles else None
            my_id = (h_curr.slot.user_id if h_curr else None) or resolved_uid or OKBrowserSession.get_current_user_id()

            UI.header(
                "ok-ru-friends-pathfinder",
                "OSINT & Bidirectional BFS Social Graph Explorer",
            )
            auth_str = (
                f"{UI.GREEN}Подключено ({session_count} слот(ов){f', ID {my_id}' if my_id else ''}){UI.RESET}"
                if session_count > 0
                else f"{UI.RED}Не авторизован{UI.RESET}"
            )
            print(f"  Сессия:    {auth_str}")
            print(f"  Кэш БД:    {stats['profiles']} узлов / {stats['edges']} связей (SQLite WAL)")
            if cp:
                cp_a = cp.get("name_a", cp.get("start_id"))
                cp_b = cp.get("name_b", cp.get("target_id"))
                print(f"  Пауза:     {UI.YELLOW}{cp_a} ➔ {cp_b} (слой {cp.get('depth', 2)}){UI.RESET}")
            print("  " + "─" * 64)

            if cp:
                print(f"  {UI.GREEN}[R]{UI.RESET} Продолжить прерванный поиск ({cp.get('name_a')} ➔ {cp.get('name_b')})")
            print(f"  {UI.CYAN}[1]{UI.RESET} Новый поиск цепочки связей")
            print(f"  {UI.CYAN}[2]{UI.RESET} Подробная статистика кэша и прокси-пула")
            print(f"  {UI.CYAN}[3]{UI.RESET} Войти в аккаунт OK.ru (Playwright браузер)")
            print(f"  {UI.CYAN}[4]{UI.RESET} Очистить кэш / сбросить контрольную точку")
            print(f"  {UI.DIM}[0] Выход{UI.RESET}")
            print("  " + "─" * 64)

            choice = input("  Выберите действие: ").strip().lower()

            if choice == "r" and cp:
                if not _ensure_authenticated(pool, crawler, allow_interactive=True):
                    continue
                paths, meta, edges = engine.find_paths(
                    cp["start_id"],
                    cp["target_id"],
                    resume_checkpoint=cp,
                )
                if paths:
                    _print_and_export_results(
                        paths, edges, meta, set(cp.get("excluded_ids", [])), ["html"]
                    )

            elif choice == "1":
                if not _ensure_authenticated(pool, crawler, allow_interactive=True):
                    continue
                if cp:
                    cp_mgr.clear()

                print("\n  " + "─" * 64)
                user_a = input("  Профиль A (URL или ID) [напр. 1234567890]: ").strip()
                user_b = input("  Профиль B (URL или ID) [напр. 9876543210]: ").strip()
                if not user_a or not user_b:
                    UI.warn("Оба профиля обязательны.")
                    continue

                extra_excl = input("  Исключить ID (через запятую, Enter — только свой/из .env): ").strip()
                excluded = _resolve_exclusions(extra_excl, pool, crawler)

                print("\n  Режим поиска:")
                print("    [1] Быстрый и безопасный  (по умолчанию: 2 потока, глубина 5, хабы >400, 1 путь)")
                print("    [2] Глубокий OSINT        (глубина 6, до 3 путей, deep-скан лайков/комментов на закрытых)")
                print("    [3] Ручная настройка параметров и форматов экспорта")
                preset = input("  Выбор [1]: ").strip() or "1"

                max_depth = Config.max_depth
                max_workers = Config.max_workers
                max_friends = Config.max_friends_limit
                k_paths = 1
                deep_osint = Config.deep_osint
                exports = ["html"]

                if preset == "2":
                    max_depth = 6
                    k_paths = 3
                    deep_osint = True
                    exports = ["html", "json"]
                elif preset == "3":
                    d_in = input(f"    Макс. глубина [{Config.max_depth}]: ").strip()
                    w_in = input(f"    Потоки [{Config.max_workers}]: ").strip()
                    h_in = input(f"    Порог Anti-Hub [{Config.max_friends_limit}]: ").strip()
                    k_in = input("    Сколько путей искать [1]: ").strip()
                    deep_in = input("    Deep OSINT для закрытых профилей? [y/N]: ").strip().lower()
                    exp_in = input("    Форматы экспорта (html,json,csv,gexf) [html]: ").strip()

                    max_depth = int(d_in) if d_in.isdigit() and 1 <= int(d_in) <= 8 else Config.max_depth
                    max_workers = int(w_in) if w_in.isdigit() and 1 <= int(w_in) <= 8 else Config.max_workers
                    max_friends = int(h_in) if h_in.isdigit() and int(h_in) >= 30 else Config.max_friends_limit
                    k_paths = int(k_in) if k_in.isdigit() and 1 <= int(k_in) <= 10 else 1
                    deep_osint = deep_in in ("y", "yes", "д", "да")
                    exports = [x.strip() for x in exp_in.split(",") if x.strip()] or ["html"]

                paths, meta, edges = engine.find_paths(
                    user_a,
                    user_b,
                    excluded_ids=excluded,
                    max_depth=max_depth,
                    max_workers=max_workers,
                    max_friends_limit=max_friends,
                    k_paths=k_paths,
                    deep_osint=deep_osint,
                )
                if paths:
                    _print_and_export_results(paths, edges, meta, excluded, exports)

            elif choice == "2":
                _print_stats(cache, pool, cp_mgr)
                input("\n  Нажмите Enter для возврата в меню...")

            elif choice == "3":
                OKBrowserSession.interactive_login()
                pool.reload()

            elif choice == "4":
                confirm = input("  Сбросить чекпоинт и очистить SQLite кэш? [y/N]: ").strip().lower()
                if confirm in ("y", "yes", "д", "да"):
                    cp_mgr.clear()
                    cache.clear()
                    UI.ok("База кэша и чекпоинт очищены.")

            elif choice in ("0", "q", "exit"):
                print("До свидания!")
                return 0
            else:
                UI.warn("Неизвестная команда.")
    finally:
        cache.close()


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ok_pathfinder.py",
        description="ok-ru-friends-pathfinder — OSINT Social Graph Pathfinding Tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Backward-compatible root flags
    parser.add_argument("--start", "-s", type=str, help="Start profile URL or ID (Node A)")
    parser.add_argument("--target", "-t", type=str, help="Target profile URL or ID (Node B)")
    parser.add_argument("--exclude", "-e", type=str, default="", help="Comma-separated IDs to exclude")
    parser.add_argument("--depth", "-d", type=int, default=Config.max_depth, help="Maximum handshake depth")
    parser.add_argument("--workers", "-w", type=int, default=Config.max_workers, help="Concurrent async worker count")
    parser.add_argument("--hub-limit", type=int, default=Config.max_friends_limit, help="Anti-Hub friend count threshold")
    parser.add_argument("--paths", "-k", type=int, default=1, help="Number of shortest paths to discover (K-shortest)")
    parser.add_argument("--deep-osint", action="store_true", help="Fallback to public comments/reactions on private profiles")
    parser.add_argument("--export", type=str, default="html", help="Comma-separated export formats: html,json,csv,gexf")
    parser.add_argument("--output", "-o", type=str, default="ok_path_result", help="Output filename prefix for exports")
    parser.add_argument("--non-interactive", action="store_true", help="Fail fast instead of prompting on stdin")
    parser.add_argument("--no-browser-open", action="store_true", help="Do not auto-open generated HTML report")
    parser.add_argument("--login", action="store_true", help="Launch Playwright browser for interactive login")
    parser.add_argument("--clear-cache", action="store_true", help="Clear SQLite cache and checkpoints")

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # `search` subcommand
    sp_search = subparsers.add_parser("search", help="Find shortest paths between two profiles")
    sp_search.add_argument("--start", "-s", required=True, help="Start profile URL or ID")
    sp_search.add_argument("--target", "-t", required=True, help="Target profile URL or ID")
    sp_search.add_argument("--exclude", "-e", default="", help="Comma-separated excluded IDs")
    sp_search.add_argument("--depth", "-d", type=int, default=Config.max_depth)
    sp_search.add_argument("--workers", "-w", type=int, default=Config.max_workers)
    sp_search.add_argument("--hub-limit", type=int, default=Config.max_friends_limit)
    sp_search.add_argument("--paths", "-k", type=int, default=1)
    sp_search.add_argument("--deep-osint", action="store_true")
    sp_search.add_argument("--export", default="html")
    sp_search.add_argument("--output", "-o", default="ok_path_result")
    sp_search.add_argument("--non-interactive", action="store_true")
    sp_search.add_argument("--no-browser-open", action="store_true")

    # `resume` subcommand
    sp_resume = subparsers.add_parser("resume", help="Resume an interrupted search from checkpoint")
    sp_resume.add_argument("--export", default="html")
    sp_resume.add_argument("--output", "-o", default="ok_path_result")
    sp_resume.add_argument("--non-interactive", action="store_true")
    sp_resume.add_argument("--no-browser-open", action="store_true")

    # `login`, `stats`, `cache`
    subparsers.add_parser("login", help="Authenticate interactively via Playwright")
    subparsers.add_parser("stats", help="Display SQLite cache and SessionPool statistics")
    sp_cache = subparsers.add_parser("cache", help="Manage local SQLite cache and checkpoints")
    sp_cache.add_argument("--clear", action="store_true", help="Wipe SQLite cache and checkpoints")

    return parser


def run_cli(argv: Optional[List[str]] = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    if args.login or args.command == "login":
        return 0 if OKBrowserSession.interactive_login() else 1

    pool = SessionPool()
    cache = SQLiteGraphCache()
    cp_mgr = CheckpointManager()
    crawler = OKCrawler(pool, cache)
    engine = GraphSearchEngine(crawler, cp_mgr)

    try:
        if args.clear_cache or (args.command == "cache" and getattr(args, "clear", False)):
            cp_mgr.clear()
            cache.clear()
            UI.ok("SQLite кэш и контрольная точка очищены.")
            return 0

        if args.command == "stats":
            _print_stats(cache, pool, cp_mgr)
            return 0

        if args.command == "resume":
            cp = cp_mgr.load()
            if not cp:
                UI.err("Контрольная точка не найдена.")
                return 1
            if not _ensure_authenticated(pool, crawler, allow_interactive=not args.non_interactive):
                return 1
            paths, meta, edges = engine.find_paths(
                cp["start_id"],
                cp["target_id"],
                non_interactive=args.non_interactive,
                resume_checkpoint=cp,
            )
            if paths:
                _print_and_export_results(
                    paths,
                    edges,
                    meta,
                    set(cp.get("excluded_ids", [])),
                    [x.strip() for x in args.export.split(",") if x.strip()],
                    output_prefix=args.output,
                    auto_open_html=not args.no_browser_open,
                )
                return 0
            return 1

        if args.command == "search" or (args.start and args.target):
            if not _ensure_authenticated(pool, crawler, allow_interactive=not args.non_interactive):
                return 1
            excluded = _resolve_exclusions(args.exclude, pool, crawler)
            paths, meta, edges = engine.find_paths(
                args.start,
                args.target,
                excluded_ids=excluded,
                max_depth=args.depth,
                max_workers=args.workers,
                max_friends_limit=args.hub_limit,
                k_paths=args.paths,
                deep_osint=args.deep_osint,
                non_interactive=args.non_interactive,
            )
            if paths:
                _print_and_export_results(
                    paths,
                    edges,
                    meta,
                    excluded,
                    [x.strip() for x in args.export.split(",") if x.strip()],
                    output_prefix=args.output,
                    auto_open_html=not args.no_browser_open,
                )
                return 0
            return 1

        return -1  # No subcommand/targets -> launch interactive dashboard
    finally:
        cache.close()


def main() -> int:
    ret = run_cli()
    if ret == -1:
        return interactive_dashboard()
    return ret
