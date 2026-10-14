"""Hindsight operation-validator: отсев журналов частых кронов + рамка крона + УКАЗАТЕЛЬ ЯЗЫКА
фрагмента. Кандидат в бой по карточке t_0436227b (продолжение t_13904d6b / t_ad3ad30e).

Отличие от боевой версии (4011 Б) — РОВНО ОДИН рычаг:

`lang_directive` (автор t_ad3ad30e). Язык фрагмента считается детерминированно (счёт слов) и
приписывается к РАМКЕ (`context`) отдельной строкой «Язык этого фрагмента:». Текст фрагмента
(`content`) НЕ трогается — поэтому кандидат не несёт риска из независимого код-ревью
(`code-review/verdict.md`, `code-review/round2/verdict2.md`, пункт «content перестаёт быть
валидным JSON»), который относился к рычагу нормализации текста. Тот рычаг здесь ОТСУТСТВУЕТ
целиком: третий отказ подряд по классу (a) «ложный diff» показал, что рамкой и текстовой
нормализацией класс не закрывается (см. §«Границы» ниже).

Выключатель без правки кода: HINDSIGHT_API_OPERATION_VALIDATOR_LANG_DIRECTIVE=off
(в drop-in 70-retain-filter.conf). Любая ошибка внутри директивы не имеет права сломать retain —
блок обёрнут try/except, при падении остаётся исходный `context`.

ГРАНИЦЫ (честно, карточка t_0436227b, 12.10.2026)
Класс (a) «ложный diff» НЕ закрывается этим кандидатом и переведён в апстрим-путь: корень —
представление входа в вендоренном сплиттере (документ приходит ОДНОЙ строкой с ЛИТЕРАЛЬНЫМИ
«\\n», `fact_extraction.py:471-501`, `_RECURSIVE_TEXT_SEPARATORS` / `_split_oversized_unit`), и
чинится он там, а не в валидаторе. Класс «числа» (агрегатные счётчики в фразе) и `id_preserved`
— отдельные замеры, здесь не закрываются.

Отсев по префиксам document_id — как было: HINDSIGHT_API_OPERATION_VALIDATOR_SKIP_DOC_PREFIXES.
Отсев — `accept_with(contents=[])`, а не reject: reject поднимает ошибку, и плагин Hermes писал бы
WARNING каждые 20 минут.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re

from hindsight_api.extensions.operation_validator import (
    OperationValidatorExtension,
    RecallContext,
    ReflectContext,
    RetainContext,
    ValidationResult,
)

logger = logging.getLogger("hermes.retain_filter")

# ---------------------------------------------------------------- язык фрагмента

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
_CYR = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[A-Za-z]")
_DIRECTIVE_MARK = "Язык этого фрагмента:"


def lang_of(text: str) -> tuple[str | None, int, int]:
    """Язык фрагмента по СЛОВАМ (кириллические против латинских), не по мнению модели.

    Пороги калиброваны по РЕАЛЬНЫМ чанкам (замер 11.10.2026, слова: рус/лат):
      cbef3b4f490a_121008_0  118/197 -> ru   (факты выходили по-английски — дефект)
      secretary_8c9ee866_30  101/239 -> ru   (факты верные по-русски, указатель лишь закрепляет)
      secretary_e9ab08be_3    24/69  -> ru   (факт вышел по-английски — дефект)
      researcher_585f96cc_2   38/307 -> None (факты ВЕРНЫЕ по-русски: авто-уведомления англ.,
                                              содержательные заголовки рус. Указатель на
                                              английский сломал бы это — поэтому «en» только
                                              при почти полном отсутствии кириллицы)
      secretary_cron_a1e34daec48d_1 0/298 -> en (чисто английский diff)

    ПОПРАВКА 11.10.2026 по независимому код-ревью (карточка t_13904d6b). Ревьюер прошёл 700
    документов банка и нашёл два, где счёт по СЛОВАМ давал «en» при 34 % и 35 % кириллических
    БУКВ (9 и 7 русских слов против 25 и 20 латинских): короткая русская проза среди длинных
    идентификаторов. Директива «формулируй все факты по-английски» на таком фрагменте — прямое
    вредительство, поэтому «en» теперь требует ещё и низкой ДОЛИ кириллических букв. На
    калибровочном случае (0 русских слов) поправка не срабатывает: доля 0 %.
    """
    body = text or ""
    words = _WORD.findall(body)
    cyr = sum(1 for w in words if _CYR.search(w))
    lat = len(words) - cyr
    if cyr >= 20 and cyr * 3 >= lat:
        return "ru", cyr, lat
    if lat >= 20 and cyr < 10:
        cyr_letters = len(_CYR.findall(body))
        lat_letters = len(_LAT.findall(body))
        if cyr_letters * 20 < max(1, cyr_letters + lat_letters):   # доля кириллицы < 5 %
            return "en", cyr, lat
    return None, cyr, lat


def lang_directive(text: str) -> str:
    lang, cyr, lat = lang_of(text)
    if lang is None:
        return ""
    target = "по-русски" if lang == "ru" else "по-английски"
    return (
        f"\n\n{_DIRECTIVE_MARK} {lang} ({cyr} кириллических слов против {lat} латинских). "
        f"Формулируй ВСЕ факты {target}; на другом языке оставляй только дословные названия, "
        f"идентификаторы, URL и цитаты. Английские служебные заголовки, обёртки логов и имена "
        f"полей язык фрагмента не задают."
    )


def apply_lang_directive(context: str, text: str) -> str:
    """Идемпотентно: прежняя директива снимается перед добавлением новой."""
    directive = lang_directive(text)
    if not directive:
        return context
    base = context or ""
    if _DIRECTIVE_MARK in base:
        base = base.split("\n\n" + _DIRECTIVE_MARK)[0]
    return base + directive


# ---------------------------------------------------------------- расширение

_OFF = {"off", "0", "no", "false"}


class RetainFilter(OperationValidatorExtension):
    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        raw = config.get("skip_doc_prefixes", "")
        self.prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
        self.lang_directive_on = (config.get("lang_directive", "on") or "on").strip().lower() not in _OFF
        logger.info("[hermes.retain_filter] armed, prefixes=%s, lang_directive=%s",
                    self.prefixes, self.lang_directive_on)

    def _skip(self, doc_id: str | None) -> bool:
        return bool(doc_id) and doc_id.startswith(self.prefixes)

    def _cron_context(self, bank_id: str) -> str | None:
        """`retain_context_cron` из конфига профиля банка. Апстримный плагин Hermes этот ключ
        не знает (с 21.09 — 0 применений): журналы кронов шли с рамкой живой сессии, где
        «Олег участвует», и экстрактор приписывал Олегу решения крона. Подставляем здесь."""
        prof = bank_id.removeprefix("hermes-")
        home = pathlib.Path.home() / ".hermes"
        cfg = home / "hindsight/config.json" if prof == "default" else home / f"profiles/{prof}/hindsight/config.json"
        try:
            return json.loads(cfg.read_text()).get("retain_context_cron") or None
        except (OSError, ValueError):
            return None

    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        changed = False
        out = []
        cron_ctx = None
        for c in ctx.contents:
            doc = c.get("document_id") or ctx.document_id or ""
            if self.prefixes and self._skip(doc):
                changed = True
                continue
            context = c.get("context")
            if doc.startswith("cron_"):
                cron_ctx = cron_ctx or self._cron_context(ctx.bank_id)
                if cron_ctx and context != cron_ctx:
                    context = cron_ctx
                    changed = True
            if self.lang_directive_on:
                try:
                    new_context = apply_lang_directive(context or "", c.get("content") or "")
                except Exception:  # noqa: BLE001 — директива не имеет права ломать retain
                    logger.exception("[hermes.retain_filter] lang directive failed; keep context as is")
                    new_context = context
                if new_context != context:
                    context = new_context
                    changed = True
            if context != c.get("context"):
                c = {**c, "context": context}
            out.append(c)
        if not changed:
            return ValidationResult.accept()
        logger.info("[hermes.retain_filter] bank=%s doc=%s kept %d/%d, cron_ctx=%s, lang=%s", ctx.bank_id,
                    ctx.document_id or (ctx.contents[0].get("document_id") if ctx.contents else None),
                    len(out), len(ctx.contents), bool(cron_ctx), self.lang_directive_on)
        return ValidationResult.accept_with(contents=out)

    async def validate_recall(self, ctx: RecallContext) -> ValidationResult:
        return ValidationResult.accept()

    async def validate_reflect(self, ctx: ReflectContext) -> ValidationResult:
        return ValidationResult.accept()