"""Детерминированное вето на слияние наблюдений, различающихся датой или временем.

Зачем. Штатный `_dedup_adjudicate` (hindsight_api/engine/consolidation/consolidator.py)
отдаёт решение merge/keep одному LLM-вызову. Замер 30.08.2026 на 120 размеченных парах
(`~/.hermes/hindsight/dedup-gate-validate.json`): арбитр слил 100 пар, из них **53 ложно**,
`fn=0` — он не отверг НИ ОДНОЙ настоящей пары. То есть это не судья, а штамп «слить».
При этом 41 из 53 ложных слияний различаются числом, хотя в `_DEDUP_PROMPT` УЖЕ написано
«differ in ANY important detail — a number/quantity …→ keep». Восьмая строчка в списке,
где не работают семь, ничего не даст — поэтому заслон детерминированный, в коде.

Правило («вариант G» замера): вето, если обе стороны несут даты (или обе — время) и ни
одно множество не вложено в другое. Вложение — законный дубль: одна заметка просто
подробнее (в ней даты первой плюс своя). Замер на тех же 120 парах: G убивает 11 ложных
слияний и **не стоит ни одного верного** (варианты B/H режут вдвое больше брака ценой
5-8 пропущенных дублей — дороже, чем бесплатный G, поэтому начинаем с G).

Цена ошибки несимметрична: ложное слияние теряет факт безвозвратно, пропущенный дубль
всего лишь шумит.

Модуль — единственный источник правды для трёх потребителей:
  * арбитр консолидации (через `.pth` в venv демона → `install()` ниже);
  * `hindsight-contradiction-sweep.py` (он СНОСИТ источники — цена ошибки ещё выше);
  * `hindsight-dedup-gate-validate.py` (замер `--veto`).

Правку site-packages НЕ делаем: файлы venv — хардлинки в кэш uv, правка расползается по
всем venv хоста. Здесь только НОВЫЙ файл + `.pth`, апстрим не тронут.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import json
import logging
import os
import re
import sys

logger = logging.getLogger("hermes.dedup_veto")

# Куда врезка кладёт свой исход. Файл — единственный канал, переживающий то, что
# лог на момент импорта модуля может быть ещё не настроен; его же читает
# `hindsight-veto-check.py` из ежедневного `system-health-audit`.
# Переопределение — только для приёмки (`hindsight-dedup-veto-test.py`): пробе
# нужен процесс, похожий на демон, а писать при этом в боевой статус нельзя —
# сторож увидел бы отметку от уже мёртвого pid и поднял ложную тревогу.
STATUS_PATH = (os.environ.get("HERMES_DEDUP_VETO_STATUS")
               or os.path.expanduser("~/.hermes/state/dedup-veto-status.json"))

# Месяцы — длинные варианты первыми: «март» и «мая» иначе схлопываются на префиксе «ма».
_RU_MONTHS = [
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("мая", 5), ("май", 5),
    ("июн", 6), ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10),
    ("ноябр", 11), ("декабр", 12),
]
_EN_MONTHS = [
    ("jan", 1), ("feb", 2), ("mar", 3), ("apr", 4), ("may", 5), ("jun", 6),
    ("jul", 7), ("aug", 8), ("sep", 9), ("oct", 10), ("nov", 11), ("dec", 12),
]
_MONTH_NUM = {name: num for name, num in _RU_MONTHS + _EN_MONTHS}

_RU_ALT = "|".join(name for name, _ in _RU_MONTHS)
_EN_ALT = "|".join(name for name, _ in _EN_MONTHS)

_RE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
# «17 августа», «17 Aug», а также перечисление и диапазон при общем месяце:
# «11 и 13 августа», «29–30 июля». Без цепочки слева правило вложенности ломается:
# слитое наблюдение несёт ОБЕ даты, и, увидев из них одну, заслон принял бы законный
# дубль за конфликт.
_RE_DAY_MONTH = re.compile(
    rf"(?<!\d)((?:\d{{1,2}}\s*(?:[–—\-,]|и|and|&)\s*)*\d{{1,2}})\s+({_RU_ALT}|{_EN_ALT})[а-яё]*",
    re.I)
_RE_DAY_CHAIN = re.compile(r"\d{1,2}")
# «Aug 17» — английский порядок
_RE_MONTH_DAY = re.compile(rf"\b({_EN_ALT})[a-z]*\.?\s+(\d{{1,2}})(?!\d)", re.I)
# «28.08», «28.08.2026». Обе части ОБЯЗАТЕЛЬНО двузначные, а по краям запрещены цифра и
# точка — иначе версии («2.16.4», «AWG 3.1», «1.4.0») и IP («10.8.1.8», «147.45.230.125»)
# читались бы как даты и давали ложное вето на настоящих дублях.
_RE_DOTTED = re.compile(r"(?<![\d.])(\d{2})\.(\d{2})(?:\.(\d{2,4}))?(?![\d.])")
_RE_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")


def dates_in(text: str) -> set[tuple[int, int]]:
    """Множество дат как (месяц, день). Год намеренно отброшен: формат «11 августа» его
    не несёт, и требовать совпадения года значило бы не сводить его с «2026-08-11».
    """
    out: set[tuple[int, int]] = set()
    if not text:
        return out
    for _, m, d in _RE_ISO.findall(text):
        out.add((int(m), int(d)))
    for chain, name in _RE_DAY_MONTH.findall(text):
        num = _month_num(name)
        if not num:
            continue
        for d in _RE_DAY_CHAIN.findall(chain):
            if 1 <= int(d) <= 31:
                out.add((num, int(d)))
    for name, d in _RE_MONTH_DAY.findall(text):
        num = _month_num(name)
        if num and 1 <= int(d) <= 31:
            out.add((num, int(d)))
    for d, m, _year in _RE_DOTTED.findall(text):
        if 1 <= int(d) <= 31 and 1 <= int(m) <= 12:
            out.add((int(m), int(d)))
    return out


def _month_num(name: str) -> int | None:
    low = name.lower()
    for key, num in _RU_MONTHS + _EN_MONTHS:
        if low.startswith(key):
            return num
    return _MONTH_NUM.get(low)


def times_in(text: str) -> set[tuple[int, int]]:
    """Множество отметок времени как (час, минута)."""
    if not text:
        return set()
    return {(int(h), int(m)) for h, m in _RE_TIME.findall(text)}


def _incompatible(a: set, b: set) -> bool:
    """Обе стороны что-то несут и ни одна не вложена в другую."""
    return bool(a) and bool(b) and not (a <= b or b <= a)


def veto_reason(new_text: str, existing_text: str) -> str | None:
    """`None` — слияние допустимо; строка — почему слияние запрещено.

    Строка попадает в лог демона (`dedup-veto`) и в отчёт прохода, поэтому она несёт
    сами конфликтующие значения: «вето» без значений нечем перепроверить.
    """
    da, db = dates_in(new_text), dates_in(existing_text)
    if _incompatible(da, db):
        return (f"конфликт дат: {_fmt_dates(da)} vs {_fmt_dates(db)}")
    ta, tb = times_in(new_text), times_in(existing_text)
    if _incompatible(ta, tb):
        return (f"конфликт времени: {_fmt_times(ta)} vs {_fmt_times(tb)}")
    return None


def _fmt_dates(s: set[tuple[int, int]]) -> str:
    return ",".join(f"{d:02d}.{m:02d}" for m, d in sorted(s))


def _fmt_times(s: set[tuple[int, int]]) -> str:
    return ",".join(f"{h:02d}:{m:02d}" for h, m in sorted(s))


# --------------------------------------------------------------------------- #
# Врезка в арбитра консолидации
# --------------------------------------------------------------------------- #

_TARGET = "hindsight_api.engine.consolidation.consolidator"
_ANCHOR_PARAM = "anchor_text"
_installed = False


class VetoNotInstalled(RuntimeError):
    """Врезаться не во что: апстрим переименовал или переставил.

    Отдельный тип, а не `return False`, потому что молчаливый отказ — ровно тот
    сценарий, ради которого всё это писано: заслон исчезает, лог чист, а арбитр
    снова сливает наблюдения с разными датами. Поднявшего ловит `_attach`.
    """


def _anchor_index(func) -> int:
    """Позиция `anchor_text` в сигнатуре арбитра.

    Раньше индекс был зашит (`args[5]`). Это вторая, тихая точка дрейфа: при
    перестановке параметров врезка осталась бы на месте, но сравнивала бы не те
    тексты — вето без единой жалобы стало бы случайным. Индекс выводим из живой
    сигнатуры, а отсутствие параметра — отказ, а не догадка.
    """
    params = list(inspect.signature(func).parameters)
    if _ANCHOR_PARAM not in params:
        raise VetoNotInstalled(
            f"в сигнатуре `_dedup_adjudicate` нет параметра `{_ANCHOR_PARAM}`: {params}")
    return params.index(_ANCHOR_PARAM)


def patch_module(module) -> bool:
    """Обернуть `_dedup_adjudicate` вето. Идемпотентно.

    `True` — обернули сейчас; `False` — уже стояло. Всё остальное (нет функции,
    нет `_DedupOutcome`, уехала сигнатура) — `VetoNotInstalled`.
    """
    orig = getattr(module, "_dedup_adjudicate", None)
    if orig is None:
        raise VetoNotInstalled(
            "в модуле нет `_dedup_adjudicate` — апстрим переименовал или перенёс функцию")
    if getattr(orig, "_hermes_veto", False):
        return False
    outcome_cls = getattr(module, "_DedupOutcome", None)
    if outcome_cls is None:
        raise VetoNotInstalled("в модуле нет `_DedupOutcome` — исход арбитра не собрать")
    anchor_at = _anchor_index(orig)

    async def _adjudicate_with_veto(*args, **kwargs):
        outcome = await orig(*args, **kwargs)
        if outcome.best_id is None:
            return outcome
        # Апстрим логирует ТОЛЬКО слияние, поэтому «арбитр сказал keep» и «близнеца не
        # нашлось» неотличимы молчанием — а это разные вещи: первое означает, что арбитр
        # жив и работает, второе — что до него ничего не доходит. Пишем, когда близнец
        # НАЙДЕН: строка редкая (нужен косинус выше порога) и без неё заслон непроверяем.
        logger.info("[CONSOLIDATION] dedup-probe %s: арбитр сказал %s",
                    outcome.best_id[:8], "merge" if outcome.should_merge else "keep")
        if not outcome.should_merge:
            return outcome
        # anchor_text оба штатных вызова передают позиционно; индекс взят из живой
        # сигнатуры (см. `_anchor_index`), kwargs поддержаны на случай смены стиля.
        anchor = kwargs.get(_ANCHOR_PARAM) or (args[anchor_at] if len(args) > anchor_at else "")
        reason = veto_reason(anchor or "", outcome.best_text or "")
        if reason is None:
            return outcome
        logger.warning("[CONSOLIDATION] dedup-veto %s: %s", outcome.best_id[:8], reason)
        return outcome_cls(best_id=outcome.best_id, merged_text="",
                           should_merge=False, best_text=outcome.best_text)

    _adjudicate_with_veto._hermes_veto = True
    module._dedup_adjudicate = _adjudicate_with_veto
    return True


# --------------------------------------------------------------------------- #
# Громкий отчёт: врезался / не врезался
# --------------------------------------------------------------------------- #


def _is_daemon_process() -> bool:
    """Мы внутри ДОЛГОЖИВУЩЕГО демона Hindsight, а не разового вызова того же бинаря?

    Статус-файл один на хост, а `.pth` исполняется в КАЖДОМ процессе venv — в том
    числе в `hindsight-dedup-veto-test.py`, который импортирует настоящий модуль
    консолидатора, и в разовых CLI-вызовах (`hindsight-api --version` и т.п.).
    Без этой калитки такой вызов перетирал бы статус своим (уже мёртвым к моменту
    проверки) pid и сам же порождал ложную тревогу.

    Раньше калитка проверяла только имя («hindsight-api» где-то в argv) — этого
    было мало: разовый CLI-вызов того же бинаря (без `--daemon`) проходил калитку
    ничуть не хуже настоящего демона и перетирал боевой статус своим pid, который
    тут же умирал. Пойман 27.09.2026 (карточка t_ebc8c53d, класс
    hindsight-dedup-veto-status-false-armed): `--version` в один заход стёр статус
    только что перезапущенного живого демона. Теперь требуем ТОТ ЖЕ признак, что и
    сторож (`hindsight-veto-check.py::daemon_pids`) — имя ПЛЮС `--daemon` в argv,
    иначе оба места узнавали бы демона по-разному и снова разъезжались бы.

    Читаем `/proc/self/cmdline`, а не `sys.argv`: во время исполнения `.pth`
    (site.py, инициализация интерпретатора) argv ещё может быть не выставлен.
    """
    try:
        with open("/proc/self/cmdline", "rb") as fh:
            raw = fh.read().split(b"\0")
    except OSError:
        raw = [a.encode() for a in sys.argv]
    argv = [a.decode("utf-8", "replace") for a in raw if a]
    return any(os.path.basename(a).startswith("hindsight-api") for a in argv) \
        and "--daemon" in argv


def _status_would_downgrade(stage: str) -> bool:
    """Мы вот-вот перетрём УЖЕ ПОДТВЕРЖДЁННЫЙ статус живого демона отметкой «armed»
    от постороннего процесса?

    `_is_daemon_process()` ловит по имени «hindsight-api» ЛЮБОЙ процесс venv, в том
    числе разовый CLI-вызов того же бинаря (не `--daemon`). `install()` шлёт «armed»
    БЕЗУСЛОВНО на каждом таком запуске. Разовый вызов быстро умирает — и если он
    успел перетереть файл своим pid, сторож (`hindsight-veto-check.py`) сравнивает
    мёртвый pid с живым демоном и орёт «врезка не отработала», хотя демон её давно
    прошёл. Поймано на себе 27.09.2026 (карточка t_ebc8c53d): статус застрял на
    «armed» от pid 349157 (уже мёртв), пока живой демон вовсю ветировал слияния.

    Правило: «armed» не имеет права перетереть «patched» от ЖИВОГО pid. Даунгрейд
    легитимен только когда прежний pid уже мёртв (реальный рестарт демона).
    """
    if stage != "armed":
        return False
    try:
        with open(STATUS_PATH, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        return False
    if prev.get("stage") != "patched":
        return False
    prev_pid = prev.get("pid")
    return isinstance(prev_pid, int) and os.path.exists(f"/proc/{prev_pid}")


def _write_status(ok: bool, stage: str, detail: str | None) -> None:
    """Атомарно записать исход врезки. Сам никогда не роняет процесс."""
    try:
        if _status_would_downgrade(stage):
            return
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        payload = {
            "ok": bool(ok),
            "stage": stage,          # armed — хук взведён; patched — модуль пройден
            "detail": detail,
            "pid": os.getpid(),
            "at": _dt.datetime.now().isoformat(timespec="seconds"),
            "target": _TARGET,
            "veto_module": os.path.abspath(__file__),
        }
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_PATH)
    except Exception:  # noqa: BLE001 — статус вторичен, демон важнее
        logger.exception("dedup-veto: не удалось записать %s", STATUS_PATH)


def _report(ok: bool, stage: str, detail: str | None) -> None:
    if ok:
        logger.info("[CONSOLIDATION] dedup-veto: заслон %s (%s)",
                    "встал на _dedup_adjudicate" if stage == "patched" else "взведён",
                    detail or "штатно")
    else:
        msg = (f"dedup-veto НЕ ВСТАЛ — арбитр дедупа работает БЕЗ заслона "
               f"и снова волен сливать наблюдения с разными датами: {detail}")
        logger.error("[CONSOLIDATION] %s", msg)
        # Дублируем в stderr: врезка происходит на импорте модуля, а логирование
        # к этому моменту может быть ещё не настроено — тогда logger.error немой.
        print(f"[hermes.dedup_veto] {msg}", file=sys.stderr, flush=True)
    if _is_daemon_process():
        _write_status(ok, stage, detail)


def _attach(module) -> bool:
    """Врезаться в НАСТОЯЩИЙ модуль демона и отчитаться об исходе вслух."""
    try:
        fresh = patch_module(module)
    except Exception as exc:  # noqa: BLE001 — врезка не имеет права ронять демон
        _report(False, "patched", f"{type(exc).__name__}: {exc}")
        return False
    _report(True, "patched", None if fresh else "уже стоял, повторный импорт")
    return True


class _VetoFinder:
    """Хук импорта: патчим модуль СРАЗУ ПОСЛЕ его исполнения.

    Импортировать `hindsight_api` прямо из `.pth` нельзя — это тянет полпакета на старте
    любого процесса venv и ломается на циклах. Поэтому ждём естественного импорта.
    """

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

        spec.loader.exec_module = exec_module  # loader создаётся заново на каждый spec
        return spec


def install() -> None:
    """Взвести хук. Вызывается из `.pth`; повторный вызов безвреден."""
    global _installed
    if _installed:
        return
    _installed = True
    mod = sys.modules.get(_TARGET)
    if mod is not None:  # модуль уже импортирован — патчим на месте
        _attach(mod)
        return
    sys.meta_path.insert(0, _VetoFinder())
    # Отметка «взведён» с pid ЖИВОГО демона. Консолидатор импортируется лениво —
    # первым обращением к консолидации, а не на старте (проверено: ни
    # `hindsight_api.server`, ни `worker.poller` его не тянут). Без этой отметки
    # сторож после каждого рестарта демона видел бы статус от прежнего pid и орал
    # бы «заслон не встал» до первой ночной консолидации.
    _report(True, "armed", "консолидатор ещё не импортирован")
