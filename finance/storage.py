from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    secid       TEXT PRIMARY KEY,
    shortname   TEXT NOT NULL,
    secname     TEXT NOT NULL,
    isin        TEXT,
    board       TEXT,
    listlevel   INTEGER,
    is_pref     INTEGER DEFAULT 0,
    base_secid  TEXT,       -- для префов: соответствующая обыкновенная
    synonyms    TEXT,       -- JSON list of lowercase strings for name matching
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,          -- edisclosure|moex|interfax|rbc|vedomosti|...
    source_id     TEXT,                   -- native id if available
    url           TEXT UNIQUE,
    hash          TEXT UNIQUE NOT NULL,   -- sha256 of normalized title+body for dedup
    published_at  TEXT NOT NULL,          -- ISO8601 with tz
    fetched_at    TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT,
    tickers       TEXT,                   -- JSON list of matched SECIDs
    tags          TEXT,                   -- JSON list of pre-classified event tags (rules)
    raw           TEXT                    -- optional JSON blob with source-specific fields
);

CREATE INDEX IF NOT EXISTS ix_news_published ON news(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_source    ON news(source);

CREATE TABLE IF NOT EXISTS ticker_vol (
    secid            TEXT PRIMARY KEY,
    sector_index     TEXT,
    n_days           INTEGER,
    mean_daily_ret   REAL,
    vol_daily        REAL,
    calibrated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticker_betas (
    secid         TEXT NOT NULL,
    factor        TEXT NOT NULL,
    beta          REAL,
    r2            REAL,
    n             INTEGER,
    calibrated_at TEXT NOT NULL,
    PRIMARY KEY (secid, factor)
);

CREATE TABLE IF NOT EXISTS index_predictors (
    target        TEXT PRIMARY KEY,   -- IMOEX, MOEXOG, ..., USDRUB
    intercept     REAL,
    coef_json     TEXT NOT NULL,      -- JSON {predictor: coef}
    r2            REAL,
    n_obs         INTEGER,
    predictors    TEXT NOT NULL,      -- JSON list [names] в порядке
    calibrated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS briefing_predictions (
    day             TEXT NOT NULL,           -- YYYY-MM-DD (МСК)
    secid           TEXT NOT NULL,
    section         TEXT NOT NULL,           -- 'up' | 'down'
    effective_pct   REAL,                    -- показанный прогноз (модель + news_alpha)
    model_pct       REAL,                    -- сырой прогноз модели
    news_alpha      REAL,                    -- вклад новостей
    morning_pct     REAL,                    -- утренняя сессия к моменту брифинга
    vol_ratio       REAL,                    -- утренний объём / вчерашний дневной
    confidence      TEXT,                    -- высокая/средняя/низкая
    status          TEXT,                    -- подтв/усилено/конфликт/только модель/…
    created_at      TEXT NOT NULL,
    PRIMARY KEY (day, secid, section)
);

CREATE TABLE IF NOT EXISTS briefing_evaluations (
    day                TEXT NOT NULL,
    secid              TEXT NOT NULL,
    section            TEXT NOT NULL,
    predicted_pct      REAL,                 -- effective_pct из briefing_predictions
    actual_day_pct     REAL,                 -- close(D) vs close(D-1)
    open_close_pct     REAL,                 -- open(D) vs prev close (гэп)
    direction_correct  INTEGER,              -- 1 если знак совпал
    magnitude_error_pp REAL,                 -- |predicted - actual| п.п.
    attribution_url    TEXT,                 -- post-hoc найденная новость (если есть)
    attribution_title  TEXT,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (day, secid, section)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    stats        TEXT      -- JSON: {source: {fetched, new, errors}}
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def content_hash(title: str, body: str | None) -> str:
    norm = (title or "").strip().lower() + "\n" + (body or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def upsert_news(items: Iterable[dict]) -> tuple[int, int]:
    """Insert news items, skipping duplicates. Returns (inserted, skipped)."""
    inserted = 0
    skipped = 0
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with tx() as conn:
        for it in items:
            h = content_hash(it["title"], it.get("body"))
            try:
                conn.execute(
                    """
                    INSERT INTO news
                        (source, source_id, url, hash, published_at, fetched_at,
                         title, body, tickers, tags, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        it["source"],
                        it.get("source_id"),
                        it.get("url"),
                        h,
                        it["published_at"],
                        now,
                        it["title"],
                        it.get("body"),
                        json.dumps(it.get("tickers") or [], ensure_ascii=False),
                        json.dumps(it.get("tags") or [], ensure_ascii=False),
                        json.dumps(it.get("raw")) if it.get("raw") else None,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
    return inserted, skipped


def fetch_news_since(since_iso: str) -> list[sqlite3.Row]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM news WHERE published_at >= ? ORDER BY published_at DESC",
            (since_iso,),
        ).fetchall()
    return list(rows)


def upsert_tickers(rows: Iterable[dict]) -> int:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    n = 0
    with tx() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO tickers
                    (secid, shortname, secname, isin, board, listlevel,
                     is_pref, base_secid, synonyms, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(secid) DO UPDATE SET
                    shortname=excluded.shortname,
                    secname=excluded.secname,
                    isin=excluded.isin,
                    board=excluded.board,
                    listlevel=excluded.listlevel,
                    is_pref=excluded.is_pref,
                    base_secid=excluded.base_secid,
                    synonyms=excluded.synonyms,
                    updated_at=excluded.updated_at
                """,
                (
                    r["secid"],
                    r["shortname"],
                    r["secname"],
                    r.get("isin"),
                    r.get("board"),
                    r.get("listlevel"),
                    1 if r.get("is_pref") else 0,
                    r.get("base_secid"),
                    json.dumps(r.get("synonyms") or [], ensure_ascii=False),
                    now,
                ),
            )
            n += 1
    return n


def all_tickers() -> list[sqlite3.Row]:
    with connect() as conn:
        return list(conn.execute("SELECT * FROM tickers ORDER BY secid"))
