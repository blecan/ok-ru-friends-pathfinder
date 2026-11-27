"""
Bidirectional BFS Graph Traversal Engine with Async Queue Concurrency,
Anti-Hub Pruning, K-Shortest Paths, Geo/Surname Heuristics, and Crash-Safe Checkpointing.
"""

import asyncio
import re
import sys
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Set, Tuple

from .auth import OKBrowserSession
from .config import Config
from .crawler import OKCrawler
from .heuristics import (
    build_public_profile_url,
    clean_user_input,
    compute_priority_score,
    extract_surnames,
    is_commercial_account,
)
from .models import ProfileResult
from .storage import CheckpointManager


def _build_checkpoint_payload(
    *,
    start_id: str,
    target_id: str,
    name_a: str,
    name_b: str,
    excluded_ids: Set[str],
    max_depth: int,
    max_workers: int,
    max_friends_limit: int,
    k_paths: int,
    deep_osint: bool,
    depth: int,
    forward_visited: Dict[str, Optional[str]],
    backward_visited: Dict[str, Optional[str]],
    forward_frontier: List[str],
    backward_frontier: List[str],
    user_meta: Dict[str, Dict[str, str]],
    geo_meta: Dict[str, List[str]],
    found_intersections: List[Tuple[str, str, bool]],
    active_layer_state: Optional[dict] = None,
) -> dict:
    """Single canonical builder for checkpoint payloads (prevents frontier loss bugs)."""
    return {
        "schema": 1,
        "start_id": start_id,
        "target_id": target_id,
        "name_a": name_a,
        "name_b": name_b,
        "excluded_ids": sorted(excluded_ids),
        "max_depth": max_depth,
        "max_workers": max_workers,
        "max_friends_limit": max_friends_limit,
        "k_paths": k_paths,
        "deep_osint": deep_osint,
        "depth": depth,
        "forward_visited": forward_visited,
        "backward_visited": backward_visited,
        "forward_frontier": forward_frontier,
        "backward_frontier": backward_frontier,
        "user_meta": user_meta,
        "geo_meta": geo_meta,
        "found_intersections": found_intersections,
        "active_layer_state": active_layer_state,
        "timestamp": time.time(),
    }


def _reconstruct_path(
    intersection_id: str,
    forward_parent: Dict[str, Optional[str]],
    backward_parent: Dict[str, Optional[str]],
) -> List[str]:
    """Reconstructs cycle-safe ordered node IDs from A to B via intersection_id."""
    path_f: List[str] = []
    seen_f: Set[str] = set()
    curr: Optional[str] = intersection_id
    while curr is not None and curr not in seen_f:
        seen_f.add(curr)
        path_f.append(curr)
        curr = forward_parent.get(curr)
    path_f.reverse()

    path_b: List[str] = []
    seen_b: Set[str] = set(seen_f)
    curr = backward_parent.get(intersection_id)
    while curr is not None and curr not in seen_b:
        seen_b.add(curr)
        path_b.append(curr)
        curr = backward_parent.get(curr)

    return path_f + path_b


class GraphSearchEngine:
    """
    Asynchronous Bidirectional BFS Engine.
    Coordinates async workers without holding mutexes over sleeps or user input.
    """

    def __init__(
        self,
        crawler: OKCrawler,
        checkpoint_mgr: Optional[CheckpointManager] = None,
        ui_log: Optional[Callable[[str], None]] = None,
    ):
        self.crawler = crawler
        self.checkpoint_mgr = checkpoint_mgr or CheckpointManager()
        self.log = ui_log or print

    async def find_paths_async(
        self,
        start_user: str,
        target_user: str,
        excluded_ids: Optional[Set[str]] = None,
        max_depth: int = Config.max_depth,
        max_workers: int = Config.max_workers,
        max_friends_limit: int = Config.max_friends_limit,
        k_paths: int = 1,
        deep_osint: bool = Config.deep_osint,
        non_interactive: bool = False,
        resume_checkpoint: Optional[dict] = None,
    ) -> Tuple[List[List[Dict[str, str]]], Dict[str, Dict[str, str]], List[Tuple[str, str]]]:
        """
        Finds up to `k_paths` shortest independent paths between `start_user` and `target_user`.

        Returns:
            (paths_list, user_meta_dict, discovered_edges)
        """
        start_id = clean_user_input(start_user)
        target_id = clean_user_input(target_user)
        excl = set(excluded_ids or set())
        k_paths = max(1, k_paths)

        if not start_id or not target_id:
            self.log("[!] Ошибка: необходимо указать оба профиля.")
            return [], {}, []
        if start_id == target_id:
            self.log("[!] Ошибка: начальный и целевой профили совпадают.")
            return [], {}, []

        user_meta: Dict[str, Dict[str, str]] = {}
        geo_meta: Dict[str, List[str]] = {}
        forward_visited: Dict[str, Optional[str]] = {}
        backward_visited: Dict[str, Optional[str]] = {}
        forward_frontier: List[str] = []
        backward_frontier: List[str] = []
        discovered_edges: List[Tuple[str, str]] = []
        found_intersections: List[Tuple[str, str, bool]] = []  # (intersection_uid, parent_uid, expand_forward)
        depth = 2
        name_a = f"ID {start_id}"
        name_b = f"ID {target_id}"
        active_layer_state: Optional[dict] = None

        if resume_checkpoint:
            start_id = resume_checkpoint.get("start_id", start_id)
            target_id = resume_checkpoint.get("target_id", target_id)
            excl = set(resume_checkpoint.get("excluded_ids", excl))
            max_depth = resume_checkpoint.get("max_depth", max_depth)
            max_workers = resume_checkpoint.get("max_workers", max_workers)
            max_friends_limit = resume_checkpoint.get("max_friends_limit", max_friends_limit)
            k_paths = resume_checkpoint.get("k_paths", k_paths)
            deep_osint = resume_checkpoint.get("deep_osint", deep_osint)
            depth = resume_checkpoint.get("depth", 2)
            forward_visited = resume_checkpoint.get("forward_visited", {})
            backward_visited = resume_checkpoint.get("backward_visited", {})
            forward_frontier = resume_checkpoint.get("forward_frontier", [])
            backward_frontier = resume_checkpoint.get("backward_frontier", [])
            user_meta = resume_checkpoint.get("user_meta", {})
            geo_meta = resume_checkpoint.get("geo_meta", {})
            found_intersections = [tuple(x) for x in resume_checkpoint.get("found_intersections", [])]
            active_layer_state = resume_checkpoint.get("active_layer_state")
            name_a = resume_checkpoint.get("name_a", user_meta.get(start_id, {}).get("name", f"ID {start_id}"))
            name_b = resume_checkpoint.get("name_b", user_meta.get(target_id, {}).get("name", f"ID {target_id}"))

            self.log(
                f"\n[*] Возобновление поиска из контрольной точки: {name_a} -> {name_b} "
                f"(слой {depth + 1}, найдено путей: {len(found_intersections)}/{k_paths})"
            )
        else:
            # 1. Fetch starting node A and target node B with full endpoint page limit (up to 200 pages)
            self.log(f"[*] 1/2. Загрузка связей узла A (ID {start_id})...")
            res_a = await asyncio.to_thread(
                self.crawler.get_user_data,
                start_id,
                Config.max_pages_endpoints,
                999999,
                None,
                False,
                deep_osint,
            )
            if res_a.status in ("rate_limited", "captcha", "auth_expired", "network_error"):
                self.log(f"[!] Сбой загрузки узла A: статус={res_a.status}")
                return [], user_meta, []
            name_a = res_a.name
            user_meta[start_id] = {"id": start_id, "name": name_a, "url": build_public_profile_url(start_id), "friend_count": len(res_a.friends)}
            if res_a.geo_tokens:
                geo_meta[start_id] = res_a.geo_tokens
            if res_a.is_deleted or (res_a.is_private and not res_a.friends):
                self.log(f"[!] Узел A ({name_a}) недоступен (статус: {res_a.status}).")
                return [], user_meta, []
            self.log(f"    ✓ {name_a} | Контактов: {len(res_a.friends)}")

            self.log(f"[*] 2/2. Загрузка связей узла B (ID {target_id})...")
            res_b = await asyncio.to_thread(
                self.crawler.get_user_data,
                target_id,
                Config.max_pages_endpoints,
                999999,
                None,
                False,
                deep_osint,
            )
            if res_b.status in ("rate_limited", "captcha", "auth_expired", "network_error"):
                self.log(f"[!] Сбой загрузки узла B: статус={res_b.status}")
                return [], user_meta, []
            name_b = res_b.name
            user_meta[target_id] = {"id": target_id, "name": name_b, "url": build_public_profile_url(target_id), "friend_count": len(res_b.friends)}
            if res_b.geo_tokens:
                geo_meta[target_id] = res_b.geo_tokens
            if res_b.is_deleted or (res_b.is_private and not res_b.friends):
                self.log(f"[!] Узел B ({name_b}) недоступен (статус: {res_b.status}).")
                return [], user_meta, []
            self.log(f"    ✓ {name_b} | Контактов: {len(res_b.friends)}")

            friends_a_clean = [f for f in res_a.friends if f["id"] not in excl]
            friends_b_clean = [f for f in res_b.friends if f["id"] not in excl]

            for f in friends_a_clean:
                user_meta[f["id"]] = f
                discovered_edges.append((start_id, f["id"]))
            for f in friends_b_clean:
                user_meta[f["id"]] = f
                discovered_edges.append((target_id, f["id"]))

            ids_a = {f["id"] for f in friends_a_clean}
            ids_b = {f["id"] for f in friends_b_clean}

            # Hop 1: Direct connection
            if target_id in ids_a:
                self.log("\n[+] Прямая связь (1 рукопожатие)!")
                self.checkpoint_mgr.clear()
                return [[user_meta[start_id], user_meta[target_id]]], user_meta, discovered_edges

            # Hop 2: Mutual contacts using lightweight 1-page Anti-Hub probe
            mutual = ids_a.intersection(ids_b)
            if mutual:
                valid_mutual: List[Tuple[str, str, int]] = []
                for mid in mutual:
                    m_name_hint = user_meta.get(mid, {}).get("name", "")
                    if is_commercial_account(m_name_hint):
                        continue
                    probe = await asyncio.to_thread(
                        self.crawler.get_user_data,
                        mid,
                        max((max_friends_limit // 18) + 2, 25),
                        max_friends_limit,
                        m_name_hint,
                        True,
                        False,
                    )
                    if not probe.is_hub and not probe.is_deleted:
                        cnt = probe.total_friends_count if probe.total_friends_count is not None else len(probe.friends)
                        valid_mutual.append((mid, probe.name, cnt))
                        if len(valid_mutual) >= k_paths:
                            break
                    elif probe.is_hub:
                        self.log(f"    [Anti-Hub] Пропущен общий хаб: {probe.name} (ID {mid})")

                if valid_mutual:
                    valid_mutual.sort(key=lambda x: x[2])
                    paths_2: List[List[Dict[str, str]]] = []
                    for mid, b_name, b_cnt in valid_mutual[:k_paths]:
                        cperson = dict(user_meta[mid])
                        cperson["name"] = b_name
                        cperson["friend_count"] = b_cnt
                        paths_2.append([user_meta[start_id], cperson, user_meta[target_id]])
                    self.log(f"\n[+] Найдено общих связей 2-го порядка: {len(paths_2)}")
                    self.checkpoint_mgr.clear()
                    return paths_2, user_meta, discovered_edges

            forward_visited = {start_id: None}
            backward_visited = {target_id: None}
            for f in friends_a_clean:
                forward_visited[f["id"]] = start_id
            for f in friends_b_clean:
                backward_visited[f["id"]] = target_id

            forward_frontier = list(ids_a)
            backward_frontier = list(ids_b)
            depth = 2

        def save_state(layer_state: Optional[dict] = None, save_depth: Optional[int] = None) -> None:
            self.checkpoint_mgr.save(
                _build_checkpoint_payload(
                    start_id=start_id,
                    target_id=target_id,
                    name_a=name_a,
                    name_b=name_b,
                    excluded_ids=excl,
                    max_depth=max_depth,
                    max_workers=max_workers,
                    max_friends_limit=max_friends_limit,
                    k_paths=k_paths,
                    deep_osint=deep_osint,
                    depth=save_depth if save_depth is not None else depth,
                    forward_visited=forward_visited,
                    backward_visited=backward_visited,
                    forward_frontier=forward_frontier,
                    backward_frontier=backward_frontier,
                    user_meta=user_meta,
                    geo_meta=geo_meta,
                    found_intersections=found_intersections,
                    active_layer_state=layer_state,
                )
            )

        save_state(active_layer_state)

        try:
            while (forward_frontier and backward_frontier and depth < max_depth) or active_layer_state is not None:
                if active_layer_state is not None:
                    # Resume interrupted layer from exact remaining queue
                    expand_forward = bool(active_layer_state["expand_forward"])
                    valid_frontier = list(active_layer_state["pending_uids"])
                    next_layer_nodes = list(active_layer_state["next_layer_nodes"])
                    direction_label = active_layer_state.get("direction_label", f"слой {depth}")
                    if len(found_intersections) >= k_paths:
                        break
                else:
                    depth += 1
                    # Keep snapshot of frontiers BEFORE mutating for layer execution
                    if len(forward_frontier) <= len(backward_frontier):
                        expand_forward = True
                        raw_frontier = list(forward_frontier)
                        direction_label = f"со стороны {name_a}"
                        opp_visited = backward_visited
                    else:
                        expand_forward = False
                        raw_frontier = list(backward_frontier)
                        direction_label = f"со стороны {name_b}"
                        opp_visited = forward_visited

                    opposite_surnames: Set[str] = set()
                    opposite_geo: Set[str] = set()
                    for fid in opp_visited:
                        opposite_surnames.update(extract_surnames(user_meta.get(fid, {}).get("name", "")))
                        opposite_geo.update(geo_meta.get(fid, []))

                    valid_frontier = []
                    comm_cnt = 0
                    hub_cnt = 0
                    for uid in raw_frontier:
                        if uid in excl:
                            continue
                        if is_commercial_account(user_meta.get(uid, {}).get("name", "")):
                            comm_cnt += 1
                            continue
                        if self.crawler.cache.is_known_hub(uid, max_friends=max_friends_limit):
                            hub_cnt += 1
                            continue
                        valid_frontier.append(uid)

                    self.log(f"\n[*] Слой {depth} ({direction_label}): кандидатов={len(valid_frontier)} (отсечено: комм={comm_cnt}, хабы={hub_cnt})")
                    if not valid_frontier:
                        if expand_forward:
                            forward_frontier = []
                        else:
                            backward_frontier = []
                        break

                    valid_frontier.sort(
                        key=lambda uid: compute_priority_score(
                            user_meta.get(uid, {}).get("name", ""),
                            opposite_surnames,
                            self.crawler.cache.get_cached_friend_count(uid),
                            set(geo_meta.get(uid, [])) if uid in geo_meta else None,
                            opposite_geo,
                        ),
                        reverse=True,
                    )

                    if Config.safe_layer_cap > 0 and len(valid_frontier) > Config.safe_layer_cap:
                        self.log(f"    [Cap] Применён SAFE_LAYER_CAP={Config.safe_layer_cap} из {len(valid_frontier)} кандидатов.")
                        valid_frontier = valid_frontier[: Config.safe_layer_cap]

                    next_layer_nodes = []

                # Track pending items explicitly so mid-layer checkpoints NEVER lose frontier nodes
                pending_deque: deque = deque(valid_frontier)
                in_flight: Set[str] = set()
                retry_buffer: List[str] = []
                total_candidates = len(valid_frontier)
                completed_count = 0
                consecutive_failures = 0
                stop_layer = False
                pause_event = asyncio.Event()
                pause_event.set()
                cooldown_lock = asyncio.Lock()

                def snapshot_layer_state() -> dict:
                    remaining = list(retry_buffer) + list(in_flight) + list(pending_deque)
                    return {
                        "expand_forward": expand_forward,
                        "pending_uids": remaining,
                        "next_layer_nodes": next_layer_nodes,
                        "direction_label": direction_label,
                    }

                async def handle_throttle(trigger_uid: str) -> bool:
                    """
                    Coordinates rate-limit/auth recovery across all workers without holding locks during network I/O.
                    Returns True if recovered, False if aborting.
                    """
                    nonlocal stop_layer, consecutive_failures
                    if cooldown_lock.locked():
                        await pause_event.wait()
                        return not stop_layer

                    async with cooldown_lock:
                        pause_event.clear()
                        save_state(layer_state=snapshot_layer_state(), save_depth=depth)

                        self.log("\n" + "=" * 60)
                        self.log("⏳ ВРЕМЕННОЕ ОГРАНИЧЕНИЕ ЗАПРОСОВ (RATE LIMIT / COOLDOWN)")
                        self.log("=" * 60)

                        # Check if other slots in SessionPool are still active
                        if not self.crawler.pool.all_in_cooldown_or_dead():
                            self.log("   ↻ Переключение на резервный слот сессии из пула...")
                            consecutive_failures = 0
                            pause_event.set()
                            return True

                        wait_sec = max(int(self.crawler.pool.min_cooldown_remaining()), Config.cooldown_seconds)
                        for attempt in (1, 2):
                            for rem in range(wait_sec, 0, -5):
                                self.log(f"   Пауза охлаждения... осталось {rem:2d} сек   ")
                                await asyncio.sleep(min(5, rem))
                            self.crawler.pool.clear_all_cooldowns()
                            if await asyncio.to_thread(self.crawler.check_session):
                                self.log("   ✓ Доступ к сессии подтверждён!")
                                consecutive_failures = 0
                                pause_event.set()
                                return True
                            if attempt == 1:
                                self.log("   ⏳ Ограничение ещё действует, повторная пауза...")

                        # Interactive prompt ONLY if TTY is attached and non_interactive is False
                        can_prompt = (not non_interactive) and sys.stdin is not None and sys.stdin.isatty()
                        if not can_prompt:
                            self.log("[!] Сессия не восстановилась (non-interactive). Контрольная точка сохранена.")
                            stop_layer = True
                            pause_event.set()
                            return False

                        self.log("[Enter] Ждать ещё 30 сек | [1] Перевход в браузере | [S] Сохранить и выйти")
                        choice = (await asyncio.to_thread(input, "Выбор [Enter / 1 / S]: ")).strip().lower()
                        if choice == "1":
                            await asyncio.to_thread(OKBrowserSession.interactive_login)
                            self.crawler.pool.reload()
                            if await asyncio.to_thread(self.crawler.check_session):
                                consecutive_failures = 0
                                pause_event.set()
                                return True
                        elif choice == "s":
                            stop_layer = True
                            pause_event.set()
                            return False
                        else:
                            await asyncio.sleep(30)
                            self.crawler.pool.clear_all_cooldowns()
                            if await asyncio.to_thread(self.crawler.check_session):
                                consecutive_failures = 0
                                pause_event.set()
                                return True

                        stop_layer = True
                        pause_event.set()
                        return False

                def collect_friends(c_uid: str, friends_list: List[Dict[str, str]]) -> List[str]:
                    seen_local: Set[str] = set()
                    bridge_cands: List[str] = []
                    for fr in friends_list:
                        fid = fr["id"]
                        if fid in excl or fid in seen_local:
                            continue
                        seen_local.add(fid)
                        if self.crawler.cache.is_known_hub(fid, max_friends=max_friends_limit):
                            continue

                        user_meta[fid] = fr
                        discovered_edges.append((c_uid, fid))

                        target_visited = backward_visited if expand_forward else forward_visited
                        own_visited = forward_visited if expand_forward else backward_visited

                        if fid in target_visited:
                            if not any(x[0] == fid for x in found_intersections) and fid not in bridge_cands:
                                bridge_cands.append(fid)
                        elif fid not in own_visited:
                            own_visited[fid] = c_uid
                            next_layer_nodes.append(fid)
                    return bridge_cands

                async def verify_bridges(c_uid: str, bridge_cands: List[str]) -> None:
                    nonlocal stop_layer
                    own_visited = forward_visited if expand_forward else backward_visited
                    hub_pages_cap = max((max_friends_limit // 18) + 2, 25)
                    for fid in bridge_cands:
                        if stop_layer:
                            return
                        bridge_probe = await asyncio.to_thread(
                            self.crawler.get_user_data,
                            fid,
                            hub_pages_cap,
                            max_friends_limit,
                            user_meta.get(fid, {}).get("name"),
                            True,
                            False,
                        )
                        if bridge_probe.is_hub or bridge_probe.is_deleted:
                            self.log(f"    [Anti-Hub] Отклонён ложный мост: {bridge_probe.name} (ID {fid})")
                            continue
                        user_meta[fid] = {
                            "id": fid,
                            "name": bridge_probe.name,
                            "url": build_public_profile_url(fid),
                            "friend_count": bridge_probe.total_friends_count
                            if bridge_probe.total_friends_count is not None
                            else len(bridge_probe.friends),
                        }
                        if fid not in own_visited:
                            own_visited[fid] = c_uid
                        if not any(x[0] == fid for x in found_intersections):
                            found_intersections.append((fid, c_uid, expand_forward))
                        if len(found_intersections) >= k_paths:
                            stop_layer = True
                            return

                async def worker() -> None:
                    nonlocal completed_count, consecutive_failures, stop_layer
                    while not stop_layer:
                        await pause_event.wait()
                        if stop_layer:
                            break
                        if retry_buffer:
                            c_uid = retry_buffer.pop(0)
                        elif pending_deque:
                            c_uid = pending_deque.popleft()
                        else:
                            break

                        in_flight.add(c_uid)
                        c_known = user_meta.get(c_uid, {}).get("name")
                        res: ProfileResult = await asyncio.to_thread(
                            self.crawler.get_user_data,
                            c_uid,
                            Config.max_pages_per_profile,
                            max_friends_limit,
                            c_known,
                            False,
                            deep_osint,
                        )
                        in_flight.discard(c_uid)

                        if res.geo_tokens:
                            geo_meta[c_uid] = res.geo_tokens

                        if res.status in ("rate_limited", "captcha", "auth_expired", "network_error"):
                            consecutive_failures += 1
                            retry_buffer.append(c_uid)
                            self.log(f"    [!] Сбой ({res.status}): {res.name} -> в очередь повтора")
                            if res.status in ("rate_limited", "captcha", "auth_expired") or consecutive_failures >= 2:
                                ok = await handle_throttle(c_uid)
                                if not ok:
                                    break
                            continue

                        consecutive_failures = 0
                        completed_count += 1
                        clean_disp = re.sub(r"\s+", " ", res.name).strip()
                        disp = (clean_disp[:46] + "..") if len(clean_disp) > 48 else clean_disp

                        if res.is_deleted:
                            self.log(f"    🗑️  [{completed_count}/{total_candidates}] Удалён/забанен: {disp}")
                        elif res.is_private and not res.friends:
                            self.log(f"    🔒 [{completed_count}/{total_candidates}] Закрыт: {disp}")
                        elif res.is_hub:
                            self.log(f"    ⏩ [{completed_count}/{total_candidates}] Хаб пропущен: {disp} (>{max_friends_limit} друзей)")
                        elif not res.friends:
                            self.log(f"    ⚪ [{completed_count}/{total_candidates}] 0 друзей: {disp}")
                        else:
                            tag = " (deep)" if (res.is_private and deep_osint) else ""
                            self.log(f"    ⚡ [{completed_count}/{total_candidates}] {disp}: +{len(res.friends)} связей{tag}")
                            bridge_cands = collect_friends(c_uid, res.friends)
                            if bridge_cands:
                                await verify_bridges(c_uid, bridge_cands)

                        # Periodic mid-layer checkpoint every 25 processed candidates
                        if completed_count % 25 == 0 and not stop_layer:
                            save_state(layer_state=snapshot_layer_state(), save_depth=depth)

                worker_tasks = [asyncio.create_task(worker()) for _ in range(max(1, max_workers))]
                await asyncio.gather(*worker_tasks)

                if stop_layer and len(found_intersections) < k_paths and (pending_deque or retry_buffer or in_flight):
                    # Aborted mid-layer due to unrecoverable rate limit -> keep checkpoint intact!
                    save_state(layer_state=snapshot_layer_state(), save_depth=depth)
                    return [], user_meta, discovered_edges

                active_layer_state = None
                if expand_forward:
                    forward_frontier = next_layer_nodes
                else:
                    backward_frontier = next_layer_nodes

                save_state(layer_state=None, save_depth=depth)

                if len(found_intersections) >= k_paths:
                    break

        except (KeyboardInterrupt, asyncio.CancelledError):
            self.log("\n[!] Поиск прерван. Сохранение контрольной точки...")
            save_state(layer_state=locals().get("snapshot_layer_state", lambda: None)(), save_depth=depth)
            return [], user_meta, discovered_edges

        if not found_intersections:
            self.checkpoint_mgr.clear()
            self.log(f"\n[-] В пределах глубины {max_depth} пути не найдены.")
            return [], user_meta, discovered_edges

        self.checkpoint_mgr.clear()

        # Reconstruct K shortest paths
        final_paths: List[List[Dict[str, str]]] = []
        for fid, parent_uid, fwd in found_intersections[:k_paths]:
            # Temporarily ensure direct parent link on the expanded side
            if fwd:
                f_map = dict(forward_visited)
                f_map[fid] = parent_uid
                b_map = backward_visited
            else:
                f_map = forward_visited
                b_map = dict(backward_visited)
                b_map[fid] = parent_uid

            id_chain = _reconstruct_path(fid, f_map, b_map)
            hydrated: List[Dict[str, str]] = []
            for uid in id_chain:
                meta = dict(user_meta.get(uid, {"id": uid, "name": f"ID {uid}", "url": build_public_profile_url(uid)}))
                if not str(uid).isdigit():
                    meta["url"] = build_public_profile_url(uid)
                cached = self.crawler.cache.get_profile(uid, max_friends=max_friends_limit)
                if cached:
                    meta["name"] = cached.name if not cached.name.startswith("ID ") else meta["name"]
                    if cached.total_friends_count is not None:
                        meta["friend_count"] = cached.total_friends_count
                    elif cached.friends:
                        meta["friend_count"] = len(cached.friends)
                hydrated.append(meta)
            final_paths.append(hydrated)

        return final_paths, user_meta, discovered_edges

    def find_paths(self, *args, **kwargs):
        """Synchronous convenience wrapper over find_paths_async."""
        return asyncio.run(self.find_paths_async(*args, **kwargs))
