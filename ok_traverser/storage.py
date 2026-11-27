"""
Persistent storage layer:
  1. SQLiteGraphCache (WAL mode, O(1) incremental writes, TTL, JSON auto-migration)
  2. CheckpointManager (atomic writes preserving complete BFS frontiers)
"""

import json
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import Config
from .models import ProfileResult


class SQLiteGraphCache:
    """
    Thread-safe SQLite graph storage replacing monolithic JSON rewrites.
    Performs O(1) incremental profile/edge upserts using WAL mode and
    automatically imports legacy ok_friends_cache.json on first run.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        legacy_json_path: Optional[str] = None,
        ttl_days: Optional[int] = None,
    ):
        self.db_path = db_path if db_path is not None else Config.db_file
        self.legacy_json_path = legacy_json_path if legacy_json_path is not None else Config.legacy_json_cache_file
        eff_ttl = ttl_days if ttl_days is not None else Config.cache_ttl_days
        self.ttl_seconds = max(eff_ttl, 1) * 86400
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self._auto_migrate_legacy_json()

    def _init_schema(self) -> None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    uid TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    is_private INTEGER NOT NULL DEFAULT 0,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    is_hub INTEGER NOT NULL DEFAULT 0,
                    total_friends_count INTEGER,
                    geo_tokens TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS edges (
                    uid TEXT NOT NULL,
                    friend_id TEXT NOT NULL,
                    friend_name TEXT NOT NULL,
                    friend_url TEXT NOT NULL,
                    relation_type TEXT NOT NULL DEFAULT 'friend',
                    PRIMARY KEY (uid, friend_id)
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_uid ON edges(uid);")
            self.conn.commit()

    def _auto_migrate_legacy_json(self) -> int:
        """One-time migration from ok_friends_cache.json if SQLite DB is empty."""
        if not self.legacy_json_path or not os.path.exists(self.legacy_json_path):
            return 0
        with self.lock:
            count = self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            if count > 0:
                return 0
            try:
                with open(self.legacy_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not data:
                    return 0

                imported = 0
                cur = self.conn.cursor()
                for uid, entry in data.items():
                    friends = entry.get("friends", [])
                    is_priv = 1 if entry.get("is_private") else 0
                    is_hub = 1 if entry.get("is_hub") else 0
                    # Skip corrupted 0-friend open records from legacy cache
                    if len(friends) == 0 and not is_priv and not is_hub:
                        continue
                    name = entry.get("name", f"ID {uid}")
                    ts = float(entry.get("timestamp", time.time()))
                    total_cnt = entry.get("total_friends_count")
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO profiles
                        (uid, name, is_private, is_deleted, is_hub, total_friends_count, geo_tokens, updated_at)
                        VALUES (?, ?, ?, 0, ?, ?, '[]', ?)
                        """,
                        (str(uid), name, is_priv, is_hub, total_cnt, ts),
                    )
                    if friends:
                        cur.executemany(
                            """
                            INSERT OR REPLACE INTO edges
                            (uid, friend_id, friend_name, friend_url, relation_type)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    str(uid),
                                    str(fr["id"]),
                                    fr.get("name", f"ID {fr['id']}"),
                                    fr.get("url", f"https://ok.ru/profile/{fr['id']}"),
                                    fr.get("relation_type", "friend"),
                                )
                                for fr in friends
                                if fr.get("id")
                            ],
                        )
                    imported += 1
                self.conn.commit()
                return imported
            except Exception:
                return 0

    def get_profile(self, uid: str, max_friends: int = 400) -> Optional[ProfileResult]:
        """Returns cached ProfileResult if present and not expired by TTL."""
        uid = str(uid)
        now = time.time()
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM profiles WHERE uid = ?", (uid,)
            ).fetchone()
            if not row:
                return None

            # Deleted / confirmed 0-friend open profiles have a shorter 24h TTL
            edge_rows = self.conn.execute(
                "SELECT friend_id, friend_name, friend_url, relation_type FROM edges WHERE uid = ?",
                (uid,),
            ).fetchall()

            ttl = 86400 if (row["is_deleted"] or (len(edge_rows) == 0 and not row["is_private"] and not row["is_hub"])) else self.ttl_seconds
            if now - float(row["updated_at"]) > ttl:
                return None

            friends = [
                {
                    "id": er["friend_id"],
                    "name": er["friend_name"],
                    "url": er["friend_url"],
                    **({"relation_type": er["relation_type"]} if er["relation_type"] != "friend" else {}),
                }
                for er in edge_rows
            ]
            is_hub = bool(row["is_hub"]) or (len(friends) >= max_friends)
            status = "deleted" if row["is_deleted"] else ("private" if row["is_private"] else ("hub" if is_hub else "ok"))
            try:
                geo_tokens = json.loads(row["geo_tokens"] or "[]")
            except Exception:
                geo_tokens = []

            return ProfileResult(
                uid=uid,
                name=row["name"],
                friends=friends,
                status=status,
                is_private=bool(row["is_private"]),
                is_deleted=bool(row["is_deleted"]),
                is_hub=is_hub,
                total_friends_count=row["total_friends_count"],
                geo_tokens=geo_tokens,
                from_cache=True,
            )

    def save_profile(self, result: ProfileResult) -> None:
        """Atomically persists a valid ProfileResult and its friend edges."""
        if result.partial or result.status in ("rate_limited", "captcha", "auth_expired", "network_error"):
            return

        with self.lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO profiles
                (uid, name, is_private, is_deleted, is_hub, total_friends_count, geo_tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(result.uid),
                    result.name,
                    1 if result.is_private else 0,
                    1 if result.is_deleted else 0,
                    1 if result.is_hub else 0,
                    result.total_friends_count,
                    json.dumps(list(result.geo_tokens), ensure_ascii=False),
                    time.time(),
                ),
            )
            cur.execute("DELETE FROM edges WHERE uid = ?", (str(result.uid),))
            if result.friends:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO edges
                    (uid, friend_id, friend_name, friend_url, relation_type)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(result.uid),
                            str(fr["id"]),
                            fr.get("name", f"ID {fr['id']}"),
                            fr.get("url", f"https://ok.ru/profile/{fr['id']}"),
                            fr.get("relation_type", "friend"),
                        )
                        for fr in result.friends
                        if fr.get("id")
                    ],
                )
            self.conn.commit()

    def update_profile_name(self, uid: str, name: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE profiles SET name = ? WHERE uid = ?", (name, str(uid)))
            self.conn.commit()

    def is_known_hub(self, uid: str, max_friends: int = 400) -> bool:
        uid = str(uid)
        with self.lock:
            row = self.conn.execute(
                "SELECT is_hub, total_friends_count FROM profiles WHERE uid = ?",
                (uid,),
            ).fetchone()
            if not row:
                return False
            if row["is_hub"]:
                return True
            if row["total_friends_count"] and row["total_friends_count"] > max_friends:
                return True
            cnt = self.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE uid = ?", (uid,)
            ).fetchone()[0]
            return cnt >= max_friends

    def get_cached_friend_count(self, uid: str) -> Optional[int]:
        uid = str(uid)
        with self.lock:
            row = self.conn.execute(
                "SELECT total_friends_count FROM profiles WHERE uid = ?", (uid,)
            ).fetchone()
            if not row:
                return None
            if row["total_friends_count"] is not None:
                return int(row["total_friends_count"])
            return int(self.conn.execute("SELECT COUNT(*) FROM edges WHERE uid = ?", (uid,)).fetchone()[0])

    def get_stats(self) -> Dict[str, int]:
        """Returns summary metrics for CLI dashboard and stats command."""
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            hubs = self.conn.execute("SELECT COUNT(*) FROM profiles WHERE is_hub = 1").fetchone()[0]
            priv = self.conn.execute("SELECT COUNT(*) FROM profiles WHERE is_private = 1").fetchone()[0]
            deleted = self.conn.execute("SELECT COUNT(*) FROM profiles WHERE is_deleted = 1").fetchone()[0]
            edges = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            return {
                "profiles": total,
                "hubs": hubs,
                "private": priv,
                "deleted": deleted,
                "edges": edges,
            }

    def clear(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM edges;")
            self.conn.execute("DELETE FROM profiles;")
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class CheckpointManager:
    """Atomic checkpoint manager ensuring crash-safe save/resume without frontier loss."""

    def __init__(self, filepath: Optional[str] = None):
        self.filepath = filepath if filepath is not None else Config.checkpoint_file
        self._lock = threading.Lock()

    def save(self, state: dict) -> None:
        tmp_path = f"{self.filepath}.tmp"
        with self._lock:
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.filepath)
            except Exception as e:
                print(f"[!] Не удалось сохранить контрольную точку: {e}")

    def load(self) -> Optional[dict]:
        if not os.path.exists(self.filepath):
            return None
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def clear(self) -> None:
        for path in (self.filepath, f"{self.filepath}.tmp"):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
