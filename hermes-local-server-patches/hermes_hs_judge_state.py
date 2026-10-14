"""Persistent exact-input verdicts and fair attempts, with no raw facts in keys.

Legacy timestamps are scheduling hints, never evidence of a successful verdict.
SQLite commits every result; no whole-file read/modify/write lost updates.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


class JudgeState:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        path.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS verdicts '
                        '(key TEXT PRIMARY KEY, verdict TEXT, attempted REAL NOT NULL)')
        self.db.commit()

    def get(self, key):
        row = self.db.execute('SELECT verdict FROM verdicts WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def attempted(self, key):
        row = self.db.execute('SELECT attempted FROM verdicts WHERE key=?', (key,)).fetchone()
        return row[0] if row else 0

    def record(self, key, verdict=None):
        with self.db:
            self.db.execute('INSERT INTO verdicts VALUES (?,?,?) ON CONFLICT(key) '
                            'DO UPDATE SET verdict=excluded.verdict, attempted=excluded.attempted',
                            (key, json.dumps(verdict, ensure_ascii=False) if verdict is not None else None,
                             time.time()))

    def close(self):
        self.db.close()


def sweep_valid(v, n):
    if not isinstance(v, dict) or type(v.get('contradiction')) is not bool or type(v.get('duplicate')) is not bool:
        return False
    current, old, groups = v.get('current'), v.get('superseded'), v.get('duplicate_groups')
    valid_id = lambda i: type(i) is int and 1 <= i <= n
    if current is not None and not valid_id(current):
        return False
    if not isinstance(old, list) or not all(valid_id(i) for i in old) or len(set(old)) != len(old):
        return False
    if current in old or (not v['contradiction'] and (old or current is not None)):
        return False
    if old and current is None:
        return False
    if not isinstance(groups, list) or bool(groups) != v['duplicate']:
        return False
    used = set()
    for g in groups:
        if not isinstance(g, list) or len(g) < 2 or not all(valid_id(i) for i in g):
            return False
        if len(set(g)) != len(g) or used.intersection(g):
            return False
        used.update(g)
    return True


def round_clean(report):
    return all(report.get(k, 0) == 0 for k in ('flagged', 'errors', 'unchecked', 'truncated', 'resplit_dropped'))
