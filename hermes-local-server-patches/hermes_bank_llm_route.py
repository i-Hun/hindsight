"""Врезка в Hindsight: выбор LLM по банку.

Решение Олега, 11.10.2026 (карточка t_198a8956): обслуживание памяти ЦЕЛИКОМ на
локальной bonsai2-27b, без платного/облачного пути. Прежнее правило 29.09.2026
(«рабочие банки secretary/researcher → корп `magnit-gpt-fast`») отменено: корп-шлюз
отвечал 400 Model not found / 429, и именно на нём консолидация рабочих банков
стояла (secretary: 19855 некonsolidированных наблюдений). Поэтому в hermes.env
HERMES_HS_CORP_BANKS пуст, а `HINDSIGHT_API_CONSOLIDATION_LLM_*` тоже указывает на
локальную модель — корп-адрес из этого контура убран совсем.

Механизм врезки остаётся нужным даже когда retain и consolidation — одна и та же
локальная модель: он ставит ОБЩИЙ МЕЖПРОЦЕССНЫЙ слот-лимит (два слота на хосте,
`hermes_hs_local_slots`), чтобы демон, судья противоречий и ночная прополка не
забивали друг друга в очереди llama-server (`--parallel 2`).

Почему врезка, а не конфиг. В hindsight 0.9.1 модель задаётся одна на весь демон на каждую
операцию (`_retain_llm_config`, `_consolidation_llm_config` в MemoryEngine.__init__); среди
полей, переопределяемых на уровне банка (`_CONFIGURABLE_FIELDS`), llm_* нет. Все боевые вызовы
идут через `<cfg>.with_config(config, bank_id=…, operation=…)` — это единственная точка, где
известен банк, и её мы подменяем маршрутизатором.

Раскладка без новых переменных окружения:
  * «корп» провайдер  = уже собранный демоном `_consolidation_llm_config` (HINDSIGHT_API_CONSOLIDATION_LLM_*,
    с его окном ретраев на 429: 5 попыток, 5→90 с);
  * «локальный» провайдер = `_retain_llm_config` (HINDSIGHT_API_LLM_*, bonsai2-27b).
  retain:        корп-банк → корп, иначе → локальный (как было)
  consolidation: корп-банк → корп (как было), иначе → локальный (НОВОЕ: приватное не уходит наружу)

Локальный сервер держит `--parallel 2`; семафоры Hindsight считают retain и consolidation
порознь (2 + 2), поэтому все вызовы к локальному провайдеру проходят через общий семафор
HERMES_HS_LOCAL_MAX_CONCURRENT (по умолчанию 2) — иначе лишние запросы ждут в очереди llama
и съедают свой таймаут там.

Список корп-банков — HERMES_HS_CORP_BANKS (через запятую), по умолчанию
`hermes-secretary,hermes-researcher`. Не встала — громко в лог и stderr, демон работает по-старому.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from hermes_hs_local_slots import async_local_slot

_TARGET = "hindsight_api.engine.memory_engine"
_installed = False
logger = logging.getLogger("hermes.bank_llm_route")

CORP_BANKS = frozenset(
    b.strip()
    for b in os.getenv("HERMES_HS_CORP_BANKS", "hermes-secretary,hermes-researcher").split(",")
    if b.strip()
)
LOCAL_MAX = int(os.getenv("HERMES_HS_LOCAL_MAX_CONCURRENT", "2"))


def _say(ok: bool, msg: str) -> None:
    text = f"[hermes.bank_llm_route] {'OK' if ok else 'НЕ ВСТАЛ'}: {msg}"
    (logger.info if ok else logger.error)(text)
    print(text, file=sys.stderr, flush=True)


class _LocalGate:
    """Провайдер-обёртка: все вызовы к локальной модели — через один семафор."""

    def __init__(self, inner, sem: asyncio.Semaphore):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_sem", sem)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    async def call(self, *args, **kwargs):
        async with object.__getattribute__(self, "_sem"), async_local_slot():
            return await object.__getattribute__(self, "_inner").call(*args, **kwargs)

    async def call_with_tools(self, *args, **kwargs):
        async with object.__getattribute__(self, "_sem"), async_local_slot():
            return await object.__getattribute__(self, "_inner").call_with_tools(*args, **kwargs)

    def with_config(self, config, **kw):
        wrapped = object.__getattribute__(self, "_inner").with_config(config, **kw)
        # ConfiguredLLMProvider зовёт self._provider.call — подставляем себя, чтобы пройти семафор.
        object.__setattr__(wrapped, "_provider", self)
        return wrapped


class _BankRouter:
    """Подменяет `_retain_llm_config` / `_consolidation_llm_config`: выбор провайдера по bank_id."""

    def __init__(self, op: str, corp, local, default):
        object.__setattr__(self, "_op", op)
        object.__setattr__(self, "_corp", corp)
        object.__setattr__(self, "_local", local)
        object.__setattr__(self, "_default", default)

    def pick(self, bank_id):
        g = object.__getattribute__
        return g(self, "_corp") if bank_id in CORP_BANKS else g(self, "_local")

    def with_config(self, config, *, bank_id=None, operation=None, metadata=None):
        target = self.pick(bank_id)
        return target.with_config(config, bank_id=bank_id, operation=operation, metadata=metadata)

    async def cleanup(self):
        # Реальные провайдеры чистит сам движок по своим атрибутам; здесь — только «свой» дефолт.
        await object.__getattribute__(self, "_default").cleanup()

    def __getattr__(self, name):
        # provider/model/_provider_impl/… для health-проб и батч-проверок — от прежнего провайдера.
        return getattr(object.__getattribute__(self, "_default"), name)


def _describe(p) -> str:
    return f"{getattr(p, 'provider', '?')}/{getattr(p, 'model', '?')}"


def _attach(module) -> None:
    try:
        cls = module.MemoryEngine
        orig = cls.__init__
        if getattr(orig, "_hermes_bank_route", False):
            return

        def __init__(self, *args, **kwargs):
            orig(self, *args, **kwargs)
            try:
                corp = self._consolidation_llm_config
                local_raw = self._retain_llm_config
                if corp.model == local_raw.model and corp.base_url == local_raw.base_url:
                    # Маршрут по банкам не нужен, но слот-лимит нужен ВСЕГДА: демон
                    # и судьи делят один llama-server. Без этого ветка "обе настройки
                    # совпали" молча снимала ограничение (11.10.2026, t_198a8956).
                    local = _LocalGate(local_raw, asyncio.Semaphore(LOCAL_MAX))
                    self._retain_llm_config = _BankRouter("retain", local, local, local_raw)
                    self._consolidation_llm_config = _BankRouter("consolidation", local, local, local_raw)
                    _say(True, f"retain и consolidation на одной модели ({_describe(corp)}) — "
                               f"маршрут не нужен, локальный слот-потолок {LOCAL_MAX} поставлен")
                    return
                local = _LocalGate(local_raw, asyncio.Semaphore(LOCAL_MAX))
                self._retain_llm_config = _BankRouter("retain", corp, local, local_raw)
                self._consolidation_llm_config = _BankRouter("consolidation", corp, local, corp)
                _say(
                    True,
                    f"корп-банки {sorted(CORP_BANKS)} → {_describe(corp)}; "
                    f"остальные (retain+consolidation) → {_describe(local_raw)}, локальный потолок {LOCAL_MAX}",
                )
            except Exception as exc:  # noqa: BLE001 — врезка не роняет демон
                _say(False, f"{type(exc).__name__}: {exc}")

        __init__._hermes_bank_route = True
        cls.__init__ = __init__
    except Exception as exc:  # noqa: BLE001
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
