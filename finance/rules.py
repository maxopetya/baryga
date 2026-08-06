"""Правила первичной классификации событий по ключевым словам.

Задача не заменять LLM, а грубо тегировать новости, чтобы:
1) отфильтровать очевидный мусор,
2) поднять в брифинге релевантные типы событий,
3) дать LLM подсказку в промпте.
"""
from __future__ import annotations

import re

# (тег, паттерн, знак: bullish|bearish|neutral)
RULES: list[tuple[str, str, str]] = [
    ("dividends",   r"(дивиденд|div\b|рекоменд(овал|ует|ация)[а-я\s]{0,20}(выплат|распределен)|годов(ое|ых) собрани[ея])", "bullish"),
    ("buyback",     r"(байбэк|обратн[а-я]+ выкуп|buyback|program of repurchase|программа выкупа)", "bullish"),
    ("earnings",    r"(мсфо|рсбу|отчёт|отчет|финансов(ых|ые) результат|прибыл[ья]\s|выручк[аи]|ebitda|EBITDA)", "neutral"),
    ("guidance",    r"(прогноз|guidance|повыс(ил|ила) прогноз|снизила прогноз|подтвердил прогноз)", "neutral"),
    ("split",       r"(сплит|дробление акций|обратный сплит|reverse split)", "neutral"),
    ("index",       r"(включен[ие][а-я\s]* в индекс|исключ(ен|ение) из индекса|включен[ие][а-я\s]* MSCI|базы расчёта индекс)", "bullish"),
    ("mna",         r"(поглощен|слияние|приобрет(ает|аёт|ает|ла|ение)[а-я\s]{0,10}(долю|акции|актив)|\bSPAC\b|\bIPO\b|\bSPO\b|размещен(ие|ия) акций)", "neutral"),
    ("sanctions",   r"(санкц|\bSDN\b|\bOFAC\b|блокирующ|ограничительн[а-я\s]+мер|delist)", "bearish"),
    ("sanctions_lift", r"(снят(ы|и)? санкц|исключ[а-я]+ из санкцио|снятие ограничен)", "bullish"),
    ("cbr_rate",    r"(ключев(ая|ой) ставк|ЦБ.*ставк|повыси(ла?|ло) ставк|снизи(ла?|ло) ставк|решение по ставке)", "neutral"),
    ("regulation",  r"(налог[а-я\s]+нефт|НДПИ|акциз[а-я\s]+повыш|госрегулир|указ президент|распоряжен|постановлени)", "bearish"),
    ("commodities", r"(нефть.*(вырос|упа|подорож|подешев)|Brent|WTI|цены на газ|цены на никель|цены на палладий|цены на золото)", "neutral"),
    ("fx",          r"(рубль (укреп|ослаб|обвал|вырос|упал)|USD/RUB|EUR/RUB|CNY/RUB)", "neutral"),
    ("insider",     r"(инсайд(ер|ерская)|крупн(ый|ая) акционер|доля [а-я]+ в компании)", "neutral"),
    ("management",  r"(генеральн(ый|ого) директор|CEO|уход|назначен|избран[а-я]+ председател|совет директоров назначил)", "neutral"),
    ("dispute",     r"\b(суд|иск(ов|у|ом|а|е)?|арбитраж|обвин|расследован|арест|обыск)\b", "bearish"),
    ("outage",      r"(авари|прорыв|остановк[а-я]+ производств|пожар на объект|разлив|катастроф)", "bearish"),
    ("redomicil",   r"(редомицил|переезд в РФ|расконвертац|smart-share|\bGDR\b|\bАДР\b)", "bullish"),
    ("bond_default",r"(дефолт|техдефолт|неисполнени[а-я]+ обязательств)", "bearish"),
    ("special_div", r"(специальн(ый|ая|ые) дивиденд|дополнительн(ый|ая|ые) дивиденд)", "bullish"),
]

_COMPILED = [(tag, re.compile(pat, re.IGNORECASE), sign) for tag, pat, sign in RULES]


def classify(text: str) -> list[dict]:
    """Вернуть список подходящих правил: [{tag, sign, snippet}]."""
    if not text:
        return []
    hits: list[dict] = []
    for tag, rx, sign in _COMPILED:
        m = rx.search(text)
        if m:
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            snippet = text[start:end].replace("\n", " ")
            hits.append({"tag": tag, "sign": sign, "snippet": snippet})
    return hits


def tags(text: str) -> list[str]:
    return [h["tag"] for h in classify(text)]
