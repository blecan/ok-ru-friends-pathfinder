"""
Comprehensive automated test suite using mocked HTTP responses and temporary SQLite/checkpoint files.
Compatible with both `unittest` and `pytest`.
"""

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock

from ok_traverser.auth import SessionHandle, SessionPool
from ok_traverser.cli import build_cli_parser, run_cli
from ok_traverser.crawler import OKCrawler
from ok_traverser.engine import GraphSearchEngine
from ok_traverser.exporters import (
    export_graph_gexf,
    export_paths_csv,
    export_paths_json,
    render_html_report,
)
from ok_traverser.heuristics import (
    build_public_profile_url,
    clean_user_input,
    compute_priority_score,
    extract_geo_edu_tokens,
    extract_surnames,
    extract_total_friends_count,
    is_commercial_account,
)
from ok_traverser.models import AccountSlot
from ok_traverser.storage import CheckpointManager, SQLiteGraphCache


class FakeResponse:
    def __init__(self, status_code=200, url="https://m.ok.ru/profile/100/friends", text="", headers=None):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = headers or {}


class TestHeuristics(unittest.TestCase):
    def test_clean_user_input_variants(self):
        self.assertEqual(clean_user_input("1234567890"), "1234567890")
        self.assertEqual(clean_user_input("https://ok.ru/profile/987654321/friends"), "987654321")
        self.assertEqual(clean_user_input("https://m.ok.ru/dk?st.cmd=friendMain&st.friendId=555777"), "555777")
        self.assertEqual(clean_user_input("https://ok.ru/john.smith/friends"), "john.smith")
        self.assertEqual(build_public_profile_url("12345"), "https://ok.ru/profile/12345")
        self.assertEqual(build_public_profile_url("john.smith"), "https://ok.ru/john.smith")
        self.assertEqual(build_public_profile_url("john.smith", mobile=True), "https://m.ok.ru/john.smith")

    def test_commercial_and_surnames_and_geo(self):
        self.assertTrue(is_commercial_account("Натяжные потолки Быстро"))
        self.assertFalse(is_commercial_account("Иван Петров"))

        surnames = extract_surnames("Ольга Смирнова (Кузнецова)")
        self.assertIn("смирнова", surnames)
        self.assertIn("кузнецова", surnames)

        geo = extract_geo_edu_tokens("<div>Живет в: Новосибирск</div><div>Школа №42</div>")
        self.assertIn("новосибирск", geo)
        self.assertIn("42", geo)

        score = compute_priority_score(
            "Анна Смирнова (Кузнецова)",
            {"кузнецова"},
            known_friends_count=120,
            candidate_geo={"новосибирск"},
            target_geo={"новосибирск"},
        )
        # 30 (brackets) + 500 (maiden match) + 120 (geo overlap) + 100 (organic friends) = 750
        self.assertEqual(score, 750)

    def test_extract_total_friends_count(self):
        html_a = '<span>Друзья (4 123)</span>'
        html_b = '{"totalFriends": 842}'
        self.assertEqual(extract_total_friends_count(html_a), 4123)
        self.assertEqual(extract_total_friends_count(html_b), 842)


class TestStorageAndMigration(unittest.TestCase):
    def test_sqlite_auto_migration_and_checkpoint_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy_path = os.path.join(tmp, "legacy.json")
            db_path = os.path.join(tmp, "cache.db")
            cp_path = os.path.join(tmp, "cp.json")

            with open(legacy_path, "w", encoding="utf-8") as f:
                json.dump({
                    "100": {
                        "name": "Alice Test",
                        "friends": [{"id": "200", "name": "Bob Test", "url": "https://ok.ru/profile/200"}],
                        "is_private": False,
                        "is_hub": False,
                    },
                    "999": {  # Corrupted 0-friend open entry should be skipped on migration
                        "name": "Broken",
                        "friends": [],
                        "is_private": False,
                        "is_hub": False,
                    },
                }, f)

            cache = SQLiteGraphCache(db_path=db_path, legacy_json_path=legacy_path, ttl_days=14)
            try:
                stats = cache.get_stats()
                self.assertEqual(stats["profiles"], 1)
                self.assertEqual(stats["edges"], 1)
                prof = cache.get_profile("100")
                self.assertIsNotNone(prof)
                self.assertEqual(prof.name, "Alice Test")
                self.assertEqual(prof.friends[0]["id"], "200")
                self.assertIsNone(cache.get_profile("999"))
            finally:
                cache.close()

            cp = CheckpointManager(cp_path)
            cp.save({"schema": 1, "forward_frontier": ["1", "2"]})
            loaded = cp.load()
            self.assertEqual(loaded["forward_frontier"], ["1", "2"])
            cp.clear()
            self.assertIsNone(cp.load())


class TestCrawlerEdgeCases(unittest.TestCase):
    def _make_crawler(self, tmp_dir):
        db_path = os.path.join(tmp_dir, "test.db")
        cache = SQLiteGraphCache(db_path=db_path, legacy_json_path="", ttl_days=14)
        pool = SessionPool.__new__(SessionPool)
        pool._lock = __import__("threading").Lock()
        pool._rr_index = 0
        slot = AccountSlot(name="test", cookies={"AUTHCODE": "abc"}, user_id="1000")
        handle = SessionHandle(slot)
        handle.http = MagicMock()
        pool.handles = [handle]
        return OKCrawler(pool, cache), handle, cache

    def test_honest_check_session_rejects_429_and_ambiguous_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            crawler, handle, cache = self._make_crawler(tmp)
            try:
                handle.http.get.return_value = FakeResponse(status_code=429, text="")
                self.assertFalse(crawler.check_session(handle))

                # Ambiguous page without login marker AND without userMain/my_id must return False
                handle.http.get.return_value = FakeResponse(status_code=200, url="https://m.ok.ru/cdk/st.cmd/main", text="<html>welcome</html>")
                self.assertFalse(crawler.check_session(handle))

                # Valid authenticated page
                handle.http.get.return_value = FakeResponse(status_code=200, url="https://m.ok.ru/dk?st.cmd=userMain", text='<div userid="1000">выйти</div>')
                self.assertTrue(crawler.check_session(handle))
            finally:
                cache.close()

    def test_private_false_positive_protection_and_partial_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            crawler, handle, cache = self._make_crawler(tmp)
            try:
                # Friend literally named "Профиль закрыт" on an OPEN friends list must NOT mark page private
                open_html = (
                    '<a href="/dk?st.cmd=friendMain&st.friendId=201">Профиль закрыт</a>'
                    '<a href="/dk?st.cmd=friendMain&st.friendId=202">Иван Иванов</a>'
                )
                handle.http.get.return_value = FakeResponse(status_code=200, url="https://m.ok.ru/profile/101/friends", text=open_html)
                res = crawler.get_user_data("101", known_name="Test User")
                self.assertEqual(res.status, "ok")
                self.assertFalse(res.is_private)
                self.assertEqual(len(res.friends), 2)

                # Partial pagination failure (page 1 OK, page 2 hits 429) MUST NOT write incomplete friends to SQLite
                page1 = (
                    '<a href="/dk?st.cmd=friendMain&st.friendId=301">Друг Один</a>'
                    '<a href="/profile/102/friends?st.page=2">Далее</a>'
                )
                handle.http.get.side_effect = [
                    FakeResponse(status_code=200, url="https://m.ok.ru/profile/102/friends", text=page1),
                    FakeResponse(status_code=429, url="https://m.ok.ru/profile/102/friends?st.page=2", text=""),
                ]
                res2 = crawler.get_user_data("102", known_name="Paginated User")
                self.assertEqual(res2.status, "rate_limited")
                self.assertTrue(res2.partial)
                self.assertIsNone(cache.get_profile("102"))
            finally:
                cache.close()

    def test_deleted_and_captcha_and_deep_osint(self):
        with tempfile.TemporaryDirectory() as tmp:
            crawler, handle, cache = self._make_crawler(tmp)
            try:
                handle.http.get.return_value = FakeResponse(status_code=200, url="https://m.ok.ru/profile/404", text="<div>Страница удалена</div>")
                res_del = crawler.get_user_data("404", known_name="Deleted")
                self.assertTrue(res_del.is_deleted)
                self.assertEqual(res_del.status, "deleted")

                # Deep OSINT fallback on private profile extracts public wall commenters
                # while filtering out logged-in user's own nav/sidebar links (both userPage and /friends "Друзья")
                priv_wall_html = (
                    '<a href="/profile/1000?st.cmd=userPage">Мой Профиль</a>'
                    '<a href="/profile/9999/friends">Друзья</a>'
                    '<a href="/profile/777">Алексей Комментатор</a>'
                )
                handle.http.get.side_effect = [
                    FakeResponse(status_code=200, url="https://m.ok.ru/dk?st.cmd=friendRestricted&st.friendId=505", text="<div>Доступ ограничен</div>"),
                    FakeResponse(status_code=200, url="https://m.ok.ru/profile/505", text=priv_wall_html),
                ]
                res_priv = crawler.get_user_data("505", known_name="Private User", deep_osint=True)
                self.assertTrue(res_priv.is_private)
                self.assertEqual(len(res_priv.friends), 1)
                self.assertEqual(res_priv.friends[0]["id"], "777")
                self.assertEqual(res_priv.friends[0]["relation_type"], "implicit_tie")

                # Uncached hub probe with total_cnt=None (unified layout) must paginate past 21 friends
                # and detect hub when friends >= max_friends
                h_p1 = '<div data-block="FriendsPageMRB">' + "".join(
                    f'<a href="/dk?st.cmd=friendMain&st.friendId={3000 + i}">Френд {i}</a>' for i in range(21)
                ) + "</div>"
                h_p2 = '<div data-block="FriendsPageMRB">' + "".join(
                    f'<a href="/dk?st.cmd=friendMain&st.friendId={4000 + i}">Френд {i}</a>' for i in range(21)
                ) + "</div>"
                handle.http.get.side_effect = [
                    FakeResponse(status_code=200, url="https://m.ok.ru/profile/707/friends", text=h_p1),
                    FakeResponse(status_code=200, url="https://m.ok.ru/profile/707/friends?st.page=2", text=h_p2),
                ]
                res_hub = crawler.get_user_data("707", max_friends=35, known_name="Uncached Hub", hub_probe_only=True)
                self.assertTrue(res_hub.is_hub)
                self.assertEqual(res_hub.status, "hub")

                # Unified OK.ru infinite-scroll layout (21 friends on page 1, no static st.page=2 link)
                p1_cards = "".join(f'<a href="/dk?st.cmd=friendMain&st.friendId={1000 + i}">Друг {i}</a>' for i in range(21))
                p1_html = f'<div data-block="FriendsPageMRB">{p1_cards}</div>'
                p2_cards = "".join(f'<a href="/dk?st.cmd=friendMain&st.friendId={2000 + i}">Друг Стр2 {i}</a>' for i in range(5))
                p2_html = f'<div data-block="FriendsPageMRB">{p2_cards}</div>'
                handle.http.get.side_effect = [
                    FakeResponse(status_code=200, url="https://m.ok.ru/profile/606/friends", text=p1_html),
                    FakeResponse(status_code=200, url="https://m.ok.ru/profile/606/friends?st.page=2", text=p2_html),
                ]
                res_inf = crawler.get_user_data("606", known_name="Infinite Scroll User")
                self.assertEqual(res_inf.status, "ok")
                self.assertEqual(len(res_inf.friends), 26)
            finally:
                cache.close()


class TestGraphEngineAndExporters(unittest.TestCase):
    def test_bidirectional_bfs_k_paths_and_anti_hub(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SQLiteGraphCache(db_path=os.path.join(tmp, "g.db"), legacy_json_path="", ttl_days=14)
            cp = CheckpointManager(os.path.join(tmp, "cp.json"))

            # Build mocked social graph:
            # A(1) -> {2, 3, HUB(99)}
            # 2 -> {4}
            # 3 -> {5}
            # HUB(99) -> {B(10)} (should be rejected as bridge!)
            # B(10) -> {4, 5, HUB(99)}
            # Mutual cycles exist: 4 -> {2, 10}, 5 -> {3, 10}
            graph = {
                "1": ("Node A", [("2", "User Two"), ("3", "User Three"), ("99", "Mega Hub")], False),
                "2": ("User Two", [("1", "Node A"), ("4", "Bridge One")], False),
                "3": ("User Three", [("1", "Node A"), ("5", "Bridge Two")], False),
                "4": ("Bridge One", [("2", "User Two"), ("10", "Node B")], False),
                "5": ("Bridge Two", [("3", "User Three"), ("10", "Node B")], False),
                "99": ("Mega Hub", [("1", "Node A"), ("10", "Node B")], True),
                "10": ("Node B", [("4", "Bridge One"), ("5", "Bridge Two"), ("99", "Mega Hub")], False),
            }

            class MockCrawler:
                def __init__(self, cache_obj):
                    self.cache = cache_obj
                    self.pool = MagicMock()

                def get_user_data(self, uid, max_pages=40, max_friends=400, known_name=None, hub_probe_only=False, deep_osint=False):
                    from ok_traverser.models import ProfileResult
                    name, flist, is_hub = graph[uid]
                    if is_hub:
                        return ProfileResult(uid=uid, name=name, friends=[], status="hub", is_hub=True, total_friends_count=5000)
                    return ProfileResult(
                        uid=uid,
                        name=name,
                        friends=[{"id": fid, "name": fn, "url": f"https://ok.ru/profile/{fid}"} for fid, fn in flist],
                        status="ok",
                        total_friends_count=len(flist),
                    )

            try:
                engine = GraphSearchEngine(MockCrawler(cache), cp, ui_log=lambda _: None)
                paths, meta, edges = engine.find_paths("1", "10", max_depth=5, max_workers=2, max_friends_limit=400, k_paths=2, non_interactive=True)
                self.assertGreaterEqual(len(paths), 2)
                for p in paths:
                    ids = [node["id"] for node in p]
                    self.assertEqual(ids[0], "1")
                    self.assertEqual(ids[-1], "10")
                    self.assertNotIn("99", ids)  # Anti-hub rejected!

                # Test exporters + XSS escaping
                paths[0][1]["name"] = '<script>alert("xss")</script>'
                html_out = render_html_report(paths, excluded_names=['<bad>'], filename=os.path.join(tmp, "out.html"), auto_open=False)
                with open(html_out, encoding="utf-8") as hf:
                    html_content = hf.read()
                self.assertNotIn("<script>alert", html_content)
                self.assertIn("&lt;script&gt;alert", html_content)

                json_out = export_paths_json(paths, edges, meta, os.path.join(tmp, "out.json"))
                self.assertTrue(os.path.exists(json_out))

                csv_out = export_paths_csv(paths, os.path.join(tmp, "out.csv"))
                self.assertTrue(os.path.exists(csv_out))

                gexf_out = export_graph_gexf(paths, edges, meta, os.path.join(tmp, "out.gexf"))
                tree = ET.parse(gexf_out)
                self.assertIn("gexf", tree.getroot().tag)
            finally:
                cache.close()

    def test_cli_parser_subcommands_and_legacy_flags(self):
        parser = build_cli_parser()
        args_sub = parser.parse_args(["search", "-s", "111", "-t", "222", "--paths", "2", "--deep-osint"])
        self.assertEqual(args_sub.command, "search")
        self.assertEqual(args_sub.start, "111")
        self.assertEqual(args_sub.paths, 2)
        self.assertTrue(args_sub.deep_osint)

        args_legacy = parser.parse_args(["--start", "111", "--target", "222", "--non-interactive"])
        self.assertEqual(args_legacy.start, "111")
        self.assertTrue(args_legacy.non_interactive)

        # Stats command runs cleanly in an isolated temp directory without touching workspace files
        from ok_traverser import config as cfg_mod
        with tempfile.TemporaryDirectory() as tmp:
            orig_db = cfg_mod.Config.db_file
            orig_leg = cfg_mod.Config.legacy_json_cache_file
            orig_cp = cfg_mod.Config.checkpoint_file
            cfg_mod.Config.db_file = os.path.join(tmp, "stats.db")
            cfg_mod.Config.legacy_json_cache_file = os.path.join(tmp, "none.json")
            cfg_mod.Config.checkpoint_file = os.path.join(tmp, "cp.json")
            try:
                self.assertEqual(run_cli(["stats"]), 0)
            finally:
                cfg_mod.Config.db_file = orig_db
                cfg_mod.Config.legacy_json_cache_file = orig_leg
                cfg_mod.Config.checkpoint_file = orig_cp

    def test_mid_layer_checkpoint_resume_preserves_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SQLiteGraphCache(db_path=os.path.join(tmp, "g2.db"), legacy_json_path="", ttl_days=14)
            cp = CheckpointManager(os.path.join(tmp, "cp2.json"))
            from ok_traverser.models import ProfileResult

            # Layer 3 candidate "2" hits rate_limited in non_interactive mode -> must keep checkpoint with pending "2"
            call_counts = {"2": 0}

            class RateLimitThenRecoverCrawler:
                def __init__(self, cache_obj):
                    self.cache = cache_obj
                    self.pool = MagicMock()
                    self.pool.all_in_cooldown_or_dead.return_value = True
                    self.pool.min_cooldown_remaining.return_value = 0

                def check_session(self):
                    return False

                def get_user_data(self, uid, max_pages=40, max_friends=400, known_name=None, hub_probe_only=False, deep_osint=False):
                    if uid == "1":
                        return ProfileResult(uid="1", name="Start", friends=[{"id": "2", "name": "Mid", "url": "https://ok.ru/profile/2"}], status="ok")
                    if uid == "10":
                        return ProfileResult(uid="10", name="Target", friends=[{"id": "3", "name": "Other", "url": "https://ok.ru/profile/3"}], status="ok")
                    if uid == "2":
                        call_counts["2"] += 1
                        if call_counts["2"] == 1:
                            return ProfileResult(uid="2", name="Mid", friends=[], status="rate_limited", partial=True)
                        return ProfileResult(uid="2", name="Mid", friends=[{"id": "3", "name": "Other", "url": "https://ok.ru/profile/3"}], status="ok")
                    return ProfileResult(uid=uid, name=f"U{uid}", friends=[], status="ok")

            try:
                mock_c = RateLimitThenRecoverCrawler(cache)
                # Override cooldown seconds briefly so test runs fast
                from ok_traverser import config as cfg_mod
                orig_cd = cfg_mod.Config.cooldown_seconds
                cfg_mod.Config.cooldown_seconds = 0
                try:
                    engine = GraphSearchEngine(mock_c, cp, ui_log=lambda _: None)
                    paths, _, _ = engine.find_paths("1", "10", max_depth=4, max_workers=1, non_interactive=True)
                    self.assertEqual(paths, [])
                    saved = cp.load()
                    self.assertIsNotNone(saved)
                    self.assertIsNotNone(saved.get("active_layer_state"))
                    self.assertIn("2", saved["active_layer_state"]["pending_uids"])

                    # Now resume from checkpoint -> node "2" succeeds and connects to "3" -> finds path!
                    paths2, _, _ = engine.find_paths("1", "10", max_depth=4, max_workers=1, non_interactive=True, resume_checkpoint=saved)
                    self.assertEqual(len(paths2), 1)
                    self.assertEqual([n["id"] for n in paths2[0]], ["1", "2", "3", "10"])
                    self.assertIsNone(cp.load())
                finally:
                    cfg_mod.Config.cooldown_seconds = orig_cd
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main()
