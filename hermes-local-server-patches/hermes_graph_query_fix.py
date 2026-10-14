"""Врезка в Hindsight: ускорение графового поиска наблюдений (expand_observations).

Причина (24.09.2026, EXPLAIN ANALYZE на боевом банке): CTE `scored` джойнит
`unnest(source_memory_ids)` кандидатов с CTE `connected_sources`. У CTE нет статистики,
планировщик считает в нём 1 строку и строит Nested Loop: 2655 x 2110 = 5,6 млн проходов
unnest — ~3 с на КАЖДЫЙ recall (две трети всего времени запроса). Замена джойна на
`s.source_id = ANY(connected_array.source_ids)` — то же множество (connected_sources уже
DISTINCT), результат байт-в-байт совпал, время 2958 → 77 мс.

Апстрим 0.10.1 содержит тот же запрос, поэтому правка живёт снаружи пакета (как
hermes_dedup_veto): .pth → хук импорта → замена исходника метода. Не нашёл якорь —
пишет громко в лог и stderr и оставляет оригинал (поиск медленный, но рабочий).
"""
from __future__ import annotations

import inspect
import logging
import sys
import textwrap

_TARGET = "hindsight_api.engine.db.ops_postgresql"
_OLD_SELECT = "COUNT(DISTINCT cs.source_id)::float AS score"
_NEW_SELECT = "COUNT(DISTINCT s.source_id)::float AS score"
_OLD_JOIN = "JOIN connected_sources cs ON cs.source_id = s.source_id"
_NEW_JOIN = "CROSS JOIN connected_array ca2 WHERE s.source_id = ANY(ca2.source_ids)"
_installed = False
logger = logging.getLogger("hermes.graph_query_fix")


def _say(ok: bool, msg: str) -> None:
    text = f"[hermes.graph_query_fix] {'OK' if ok else 'НЕ ВСТАЛ'}: {msg}"
    (logger.info if ok else logger.error)(text)
    print(text, file=sys.stderr, flush=True)


def _attach(module) -> None:
    try:
        cls = module.PostgreSQLOps
        fn = cls.expand_observations
        if getattr(fn, "_hermes_graph_fix", False):
            return
        src = textwrap.dedent(inspect.getsource(fn))
        if src.count(_OLD_JOIN) != 1 or src.count(_OLD_SELECT) != 1:
            _say(False, "якорь не найден — апстрим поменял запрос; оставлен оригинал")
            return
        src = src.replace(_OLD_JOIN, _NEW_JOIN).replace(_OLD_SELECT, _NEW_SELECT)
        ns: dict = {}
        exec(compile(src, f"<hermes_graph_query_fix:{module.__file__}>", "exec"), module.__dict__, ns)
        new = ns["expand_observations"]
        new._hermes_graph_fix = True
        cls.expand_observations = new
        _say(True, "expand_observations пропатчен (ANY вместо nested loop)")
    except Exception as exc:  # noqa: BLE001 — врезка не роняет демон
        _say(False, f"{type(exc).__name__}: {exc}")


class _Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != _TARGET:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            spec = find(fullname, path, target) if find else None
            if spec is not None and spec.loader is not None:
                break
        else:
            return None
        real_exec = spec.loader.exec_module

        def exec_module(module):
            real_exec(module)
            _attach(module)

        spec.loader.exec_module = exec_module
        return spec


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    mod = sys.modules.get(_TARGET)
    if mod is not None:
        _attach(mod)
    else:
        sys.meta_path.insert(0, _Finder())
