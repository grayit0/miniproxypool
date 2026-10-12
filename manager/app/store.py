"""SQLite 存储层：节点表 + 上游代理表。

并发量很小，直接用一把可重入锁把 sqlite 串起来，够用且零依赖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id       TEXT PRIMARY KEY,
    host          TEXT    NOT NULL,
    port          INTEGER NOT NULL,
    egress_host   TEXT    NOT NULL,
    egress_port   INTEGER NOT NULL,
    scheme        TEXT    NOT NULL DEFAULT 'http',
    region        TEXT    NOT NULL DEFAULT '',
    tags          TEXT    NOT NULL DEFAULT '[]',
    version       TEXT    NOT NULL DEFAULT '',
    meta          TEXT    NOT NULL DEFAULT '{}',
    name          TEXT    NOT NULL DEFAULT '',
    registered_at REAL    NOT NULL,
    last_seen     REAL    NOT NULL,
    latency_ms    REAL,
    alive         INTEGER NOT NULL DEFAULT 1,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    served        INTEGER NOT NULL DEFAULT 0,
    active_conns  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nodes_alive ON nodes (alive);

CREATE TABLE IF NOT EXISTS upstreams (
    url        TEXT PRIMARY KEY,
    source     TEXT NOT NULL DEFAULT 'manual',
    added_at   REAL NOT NULL,
    last_check REAL,
    ok         INTEGER,
    latency_ms REAL,
    fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_upstreams_ok ON upstreams (ok);
"""


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 节点
    def upsert_node(
        self,
        *,
        node_id: str,
        host: str,
        port: int,
        egress_host: str,
        egress_port: int,
        scheme: str = "http",
        region: str = "",
        tags: Optional[List[str]] = None,
        version: str = "",
        meta: Optional[Dict[str, Any]] = None,
        name: str = "",
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = now or time.time()
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock:
            exists = self._conn.execute(
                "SELECT registered_at FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if exists is None:
                self._conn.execute(
                    "INSERT INTO nodes (node_id, host, port, egress_host, egress_port, scheme, region,"
                    " tags, version, meta, name, registered_at, last_seen, alive, fail_count)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)",
                    (node_id, host, port, egress_host, egress_port, scheme, region,
                     tags_json, version, meta_json, name, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE nodes SET host=?, port=?, egress_host=?, egress_port=?, scheme=?, region=?,"
                    " tags=?, version=?, meta=?, name=?, last_seen=?, alive=1, fail_count=0"
                    " WHERE node_id=?",
                    (host, port, egress_host, egress_port, scheme, region,
                     tags_json, version, meta_json, name, now, node_id),
                )
            self._conn.commit()
            return self.get_node(node_id) or {}

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            return self._row_to_node(row) if row else None

    def list_nodes(self, alive_only: bool = False, region: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM nodes"
        where, params = [], []
        if alive_only:
            where.append("alive = 1")
        if region:
            where.append("region = ?")
            params.append(region)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_node(r) for r in rows]

    def heartbeat(self, node_id: str, *, active_conns: Optional[int] = None,
                  served: Optional[int] = None, latency_ms: Optional[float] = None) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT node_id FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            if row is None:
                return False
            sets = ["last_seen = ?", "alive = 1", "fail_count = 0"]
            params: List[Any] = [time.time()]
            if active_conns is not None:
                sets.append("active_conns = ?")
                params.append(int(active_conns))
            if served is not None:
                sets.append("served = ?")
                params.append(int(served))
            if latency_ms is not None:
                sets.append("latency_ms = ?")
                params.append(float(latency_ms))
            params.append(node_id)
            self._conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE node_id = ?", params)
            self._conn.commit()
            return True

    def mark_check(self, node_id: str, ok: bool, latency_ms: Optional[float], threshold: int) -> None:
        """记录一次健康探测结果。"""
        with self._lock:
            if ok:
                self._conn.execute(
                    "UPDATE nodes SET alive = 1, fail_count = 0, latency_ms = ? WHERE node_id = ?",
                    (latency_ms, node_id),
                )
            else:
                row = self._conn.execute(
                    "SELECT fail_count FROM nodes WHERE node_id = ?", (node_id,)
                ).fetchone()
                fails = (row["fail_count"] if row else 0) + 1
                alive = 0 if fails >= max(1, threshold) else 1
                self._conn.execute(
                    "UPDATE nodes SET fail_count = ?, alive = ? WHERE node_id = ?",
                    (fails, alive, node_id),
                )
            self._conn.commit()

    def report_bad(self, node_id: str, threshold: int) -> Dict[str, Any]:
        """客户端上报某节点不可用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT fail_count, alive FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                return {"found": False, "alive": False, "fail_count": 0}
            fails = int(row["fail_count"]) + 1
            alive = 0 if fails >= max(1, threshold) else int(row["alive"])
            self._conn.execute(
                "UPDATE nodes SET fail_count = ?, alive = ? WHERE node_id = ?", (fails, alive, node_id)
            )
            self._conn.commit()
            return {"found": True, "alive": bool(alive), "fail_count": fails}

    def expire_stale(self, ttl: float) -> int:
        """把长时间没心跳的节点标记为掉线。"""
        deadline = time.time() - max(1.0, ttl)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nodes SET alive = 0 WHERE alive = 1 AND last_seen < ?", (deadline,)
            )
            self._conn.commit()
            return cur.rowcount or 0

    def delete_node(self, node_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def delete_dead_nodes(self, older_than: float = 3600.0) -> int:
        deadline = time.time() - older_than
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM nodes WHERE alive = 0 AND last_seen < ?", (deadline,)
            )
            self._conn.commit()
            return cur.rowcount or 0

    # ---------------------------------------------------------------- 上游池
    def add_upstreams(self, urls: List[str], source: str = "manual") -> int:
        inserted = 0
        now = time.time()
        with self._lock:
            for url in urls:
                url = (url or "").strip()
                if not url:
                    continue
                if "://" not in url:
                    url = "http://" + url
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO upstreams (url, source, added_at) VALUES (?,?,?)",
                    (url, source, now),
                )
                inserted += cur.rowcount or 0
            self._conn.commit()
        return inserted

    def list_upstreams(self, usable_only: bool = False) -> List[Dict[str, Any]]:
        """usable_only=True 时排除「已明确校验失败」的上游。

        注意这里用的是 COALESCE(ok, 1)：刚灌进来还没校验过（ok 为 NULL）的上游
        同样要下发给节点，否则关掉主动校验时就等于一条都发不下去。
        """
        sql = "SELECT * FROM upstreams"
        if usable_only:
            sql += " WHERE COALESCE(ok, 1) = 1"
        sql += " ORDER BY latency_ms IS NULL, latency_ms ASC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
            return [dict(r) for r in rows]

    def mark_upstream(self, url: str, ok: bool, latency_ms: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE upstreams SET ok = ?, latency_ms = ?, last_check = ?,"
                " fail_count = CASE WHEN ? = 1 THEN 0 ELSE fail_count + 1 END"
                " WHERE url = ?",
                (1 if ok else 0, latency_ms, time.time(), 1 if ok else 0, url),
            )
            self._conn.commit()

    def delete_upstream(self, url: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM upstreams WHERE url = ?", (url,))
            self._conn.commit()
            return bool(cur.rowcount)

    def delete_bad_upstreams(self, min_fails: int = 5) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM upstreams WHERE ok = 0 AND fail_count >= ?", (min_fails,)
            )
            self._conn.commit()
            return cur.rowcount or 0

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> Dict[str, Any]:
        node = dict(row)
        node["tags"] = _loads(node.get("tags"), [])
        node["meta"] = _loads(node.get("meta"), {})
        node["alive"] = bool(node.get("alive"))
        return node
