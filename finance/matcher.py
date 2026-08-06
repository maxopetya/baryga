"""Привязка новостей к тикерам: поиск SECID и синонимов в тексте."""
from __future__ import annotations

import json
import re
from functools import lru_cache

from .storage import all_tickers

# Word boundaries: кириллица + латиница + цифры
_WORD = r"[\wА-Яа-яЁё]"


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, set[str]], re.Pattern, re.Pattern]:
    """Возвращает (синоним→{SECID}, регекс синонимов, регекс тикеров)."""
    syn_map: dict[str, set[str]] = {}
    all_secids: set[str] = set()
    for row in all_tickers():
        secid = row["secid"]
        all_secids.add(secid)
        try:
            synonyms = json.loads(row["synonyms"] or "[]")
        except json.JSONDecodeError:
            synonyms = []
        for s in synonyms:
            s = s.strip().lower()
            if len(s) < 3:
                continue
            syn_map.setdefault(s, set()).add(secid)
        # преф → добавляем и базу как синоним
        base = row["base_secid"]
        if base:
            syn_map.setdefault(row["shortname"].lower(), set()).add(secid)
            if base in {r["secid"] for r in all_tickers()}:
                syn_map.setdefault(base.lower(), set()).add(base)

    # Ранжируем синонимы: длинные раньше коротких, чтобы «сбербанк» матчился раньше «сбер»
    sorted_syns = sorted(syn_map.keys(), key=lambda s: (-len(s), s))
    syn_pattern = re.compile(
        r"(?<!" + _WORD + r")(" + "|".join(re.escape(s) for s in sorted_syns) + r")(?!" + _WORD + r")",
        re.IGNORECASE,
    )
    # Точные тикерные упоминания: SBER, GAZP, MOEX:SBER, SBER.ME, $SBER
    ticker_pattern = re.compile(
        r"(?:\$|MOEX:|MCX:)?\b(" + "|".join(re.escape(t) for t in sorted(all_secids, key=lambda t: -len(t))) + r")\b(?:\.ME|\.MX|\.RTS)?",
        re.IGNORECASE,
    )
    return syn_map, syn_pattern, ticker_pattern


def reset_cache() -> None:
    _index.cache_clear()


def match(text: str) -> list[str]:
    """Вернуть отсортированный список SECID, упомянутых в тексте."""
    if not text:
        return []
    syn_map, syn_re, tick_re = _index()
    found: set[str] = set()
    # 1) синонимы (кириллица / английские имена)
    for m in syn_re.finditer(text.lower()):
        found.update(syn_map.get(m.group(1), ()))
    # 2) прямые упоминания тикеров
    for m in tick_re.finditer(text):
        found.add(m.group(1).upper())
    return sorted(found)
