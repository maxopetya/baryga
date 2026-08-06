"""Справочник тикеров MOEX 1-2 эшелона."""
from __future__ import annotations

import asyncio
import re

from .http import client
from .storage import init_db, upsert_tickers

ISS_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"
    "?iss.meta=off"
    "&securities.columns=SECID,SHORTNAME,SECNAME,ISIN,LISTLEVEL,SECTYPE"
)

# Ручные синонимы для топовых бумаг. Основа для NER по русским новостям.
# Ключ — SECID обыкновенной акции; для префов синонимы наследуются.
MANUAL_SYNONYMS: dict[str, list[str]] = {
    "SBER":  ["сбер", "сбербанк", "sberbank", "sber"],
    "GAZP":  ["газпром", "gazprom"],
    "LKOH":  ["лукойл", "lukoil"],
    "GMKN":  ["норникель", "гмк", "норильский никель", "nornickel", "норильск"],
    "ROSN":  ["роснефть", "rosneft"],
    "NVTK":  ["новатэк", "novatek"],
    "TATN":  ["татнефть", "tatneft"],
    "SNGS":  ["сургутнефтегаз", "сургут", "surgutneftegaz"],
    "MGNT":  ["магнит", "magnit"],
    "MTSS":  ["мтс", "mts"],
    "YDEX":  ["яндекс", "yandex", "yadex"],
    "VTBR":  ["втб", "vtb"],
    "MOEX":  ["мосбиржа", "московская биржа", "мск биржа"],
    "PLZL":  ["полюс", "polyus"],
    "CHMF":  ["северсталь", "severstal"],
    "NLMK":  ["нлмк", "новолипецкий"],
    "MAGN":  ["ммк", "магнитогорский", "магнитогорск"],
    "ALRS":  ["алроса", "alrosa"],
    "PHOR":  ["фосагро", "phosagro"],
    "TRNFP": ["транснефть"],
    "RTKM":  ["ростелеком", "rostelecom"],
    "FEES":  ["россети", "фск"],
    "IRAO":  ["интер рао", "интеррао", "inter rao"],
    "HYDR":  ["русгидро", "rushydro"],
    "AFLT":  ["аэрофлот", "aeroflot"],
    "AFKS":  ["афк система", "система"],
    "OZON":  ["озон", "ozon"],
    "TCSG":  ["т-банк", "тинькофф", "тбанк", "т банк", "tcs group", "tinkoff"],
    "VKCO":  ["вк", "вконтакте", "vk"],
    "POSI":  ["позитив", "positive technologies", "позитив технолоджиз"],
    "HHRU":  ["hh", "хедхантер", "headhunter"],
    "FIVE":  ["x5", "х5", "пятёрочка", "перекрёсток"],
    "FIXP":  ["fix price", "фикс прайс"],
    "SMLT":  ["самолет", "самолёт"],
    "PIKK":  ["пик"],
    "LSRG":  ["лср"],
    "ETLN":  ["эталон"],
    "RUAL":  ["русал", "rusal"],
    "MTLR":  ["мечел"],
    "MTLRP": ["мечел"],
    "ENPG":  ["эн+ груп", "эн плюс"],
    "SGZH":  ["сегежа", "segezha"],
    "AKRN":  ["акрон", "akron"],
    "KZOS":  ["казаньоргсинтез"],
    "NKNC":  ["нижнекамскнефтехим"],
    "UPRO":  ["юнипро", "unipro"],
    "MSNG":  ["мосэнерго"],
    "OGKB":  ["огк-2"],
    "TGKA":  ["тгк-1"],
    "MSTT":  ["мостотрест"],
    "BSPB":  ["банк спб", "санкт-петербург"],
    "CBOM":  ["мкб", "московский кредитный"],
    "SVCB":  ["совкомбанк"],
    "RENI":  ["ренессанс страхование"],
    "SFIN":  ["сфи", "sfi"],
    "GEMC":  ["юмг", "european medical"],
    "MDMG":  ["мать и дитя", "mother"],
    "RAGR":  ["русагро"],
    "AGRO":  ["русагро", "ros agro"],
    "BELU":  ["новабев", "белуга"],
    "ABRD":  ["абрау-дюрсо", "абрау"],
    "BANE":  ["башнефть"],
    "SELG":  ["селигдар"],
    "UGLD":  ["ужу", "южуралзолото", "южно-уральское золото"],
    "LENT":  ["лента"],
    "MVID":  ["м.видео", "мвидео", "м видео"],
    "DIAS":  ["диасофт"],
    "ASTR":  ["астра", "группа астра"],
    "SOFL":  ["софтлайн", "softline"],
    "WUSH":  ["whoosh", "вуш"],
    "DELI":  ["делимобиль"],
    "ABIO":  ["артген"],
    "IVAT":  ["иваткин", "ива", "ivat"],
}

LEGAL_FORMS = re.compile(
    r"\b(ПАО|ОАО|ЗАО|ООО|АО|Group|Ltd|Inc|Corp|Holding|Холдинг|Groupe|PJSC|Public Joint Stock Company)\b",
    re.IGNORECASE,
)
SHARE_MARKERS = re.compile(r"\b(ао|ап|обыкн|прив|привил|prefer|common)\b\.?", re.IGNORECASE)


def _clean_name(name: str) -> str:
    s = LEGAL_FORMS.sub(" ", name)
    s = SHARE_MARKERS.sub(" ", s)
    s = re.sub(r'[«»"()]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_synonyms(secid: str, shortname: str, secname: str) -> list[str]:
    syns: set[str] = set()
    for src in (shortname, secname):
        cleaned = _clean_name(src).lower()
        if cleaned and len(cleaned) >= 3:
            syns.add(cleaned)
    manual = MANUAL_SYNONYMS.get(secid)
    if manual:
        syns.update(s.lower() for s in manual)
    # для префов дотягиваем синонимы от базы
    if secid.endswith("P") and secid[:-1] in MANUAL_SYNONYMS:
        syns.update(s.lower() for s in MANUAL_SYNONYMS[secid[:-1]])
    return sorted(syns)


async def fetch_moex_shares() -> list[dict]:
    async with client() as c:
        r = await c.get(ISS_URL)
        r.raise_for_status()
        data = r.json()["securities"]
    cols = data["columns"]
    idx = {name: i for i, name in enumerate(cols)}
    rows: list[dict] = []
    for row in data["data"]:
        sectype = row[idx["SECTYPE"]]
        listlevel = row[idx["LISTLEVEL"]]
        # 1 = обыкновенная акция, 2 = преф. ETF/БПИФ (J) и прочее — мимо.
        if sectype not in ("1", "2"):
            continue
        if listlevel not in (1, 2):
            continue
        secid = row[idx["SECID"]]
        shortname = row[idx["SHORTNAME"]]
        secname = row[idx["SECNAME"]]
        is_pref = sectype == "2"
        base = None
        if is_pref and secid.endswith("P"):
            base = secid[:-1]
        rows.append(
            {
                "secid": secid,
                "shortname": shortname,
                "secname": secname,
                "isin": row[idx["ISIN"]],
                "board": "TQBR",
                "listlevel": listlevel,
                "is_pref": is_pref,
                "base_secid": base,
                "synonyms": _build_synonyms(secid, shortname, secname),
            }
        )
    return rows


async def refresh() -> int:
    init_db()
    rows = await fetch_moex_shares()
    return upsert_tickers(rows)


def main() -> None:
    n = asyncio.run(refresh())
    print(f"Загружено тикеров: {n}")


if __name__ == "__main__":
    main()
