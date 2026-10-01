"""只追加存储：所有记录按序追加，不提供更新与删除。

可选地持久化到 JSONL 文件（每行一条记录），重启后重放恢复。
"""

from __future__ import annotations

import json
import os
import threading


class AppendOnlyStore:
    """记录结构：{"seq": int, "kind": str, "payload": dict}。"""

    def __init__(self, path: str | None = None):
        self._records: list[dict] = []
        self._lock = threading.Lock()
        self._path = path
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._records.append(json.loads(line))

    def append(self, kind: str, payload: dict) -> dict:
        """追加一条记录并返回含序号的完整记录。"""
        with self._lock:
            record = {"seq": len(self._records), "kind": kind, "payload": payload}
            self._records.append(record)
            if self._path:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record

    def records(self, kind: str | None = None) -> list[dict]:
        """按追加顺序返回记录（可指定类型），调用方不得修改返回值。"""
        if kind is None:
            return [dict(r) for r in self._records]
        return [dict(r) for r in self._records if r["kind"] == kind]

    def payloads(self, kind: str) -> list[dict]:
        return [r["payload"] for r in self._records if r["kind"] == kind]

    def __len__(self) -> int:
        return len(self._records)
