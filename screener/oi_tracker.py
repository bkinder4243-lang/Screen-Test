"""
Daily OI + IV snapshot tracker.

Persists open interest and average IV per contract each time a chain is fetched.
Used to compute:
  - OI change (today vs yesterday) — distinguishes new positions from closings
  - IV history — track how implied vol has moved over the past N days
"""

import sqlite3
import pandas as pd
from datetime import date, timedelta
from pathlib import Path

_DB = "data/oi_tracker.db"


def _conn() -> sqlite3.Connection:
    Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oi_snapshots (
            snapshot_date  TEXT,
            symbol         TEXT,
            option_symbol  TEXT,
            strike         REAL,
            expiration     TEXT,
            type           TEXT,
            open_interest  INTEGER,
            iv             REAL,
            PRIMARY KEY (snapshot_date, option_symbol)
        )
    """)
    conn.commit()
    return conn


def save_snapshot(symbol: str, chain: pd.DataFrame) -> None:
    """Persist today's OI and IV for every contract in the chain."""
    today = str(date.today())
    rows = []
    for _, r in chain.iterrows():
        opt_sym = r.get("option_symbol", "")
        if not opt_sym:
            continue
        rows.append((
            today, symbol, opt_sym,
            r.get("strike"), r.get("expiration"), r.get("type"),
            int(r.get("open_interest") or 0),
            float(r["iv"]) if pd.notna(r.get("iv")) else None,
        ))
    if not rows:
        return
    with _conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO oi_snapshots VALUES (?,?,?,?,?,?,?,?)", rows
        )


def get_oi_change(symbol: str, chain: pd.DataFrame) -> pd.DataFrame:
    """
    Return chain with an added 'oi_change' column.
    Positive = new contracts opened. Negative = positions closed.
    None = no prior snapshot to compare against.
    """
    yesterday = str(date.today() - timedelta(days=1))
    with _conn() as conn:
        prev = pd.read_sql(
            "SELECT option_symbol, open_interest AS prev_oi FROM oi_snapshots "
            "WHERE symbol=? AND snapshot_date=?",
            conn, params=(symbol, yesterday),
        )
    if prev.empty:
        out = chain.copy()
        out["oi_change"] = None
        return out
    result = chain.merge(prev, on="option_symbol", how="left")
    result["oi_change"] = (result["open_interest"] - result["prev_oi"]).where(
        result["prev_oi"].notna()
    )
    return result


def get_iv_history(symbol: str, days: int = 30) -> pd.DataFrame:
    """
    Return daily average IV (near-term bucket: DTE 7–45) over the last N days.
    Requires that save_snapshot() has been called on prior days for this symbol.
    """
    cutoff = str(date.today() - timedelta(days=days))
    with _conn() as conn:
        df = pd.read_sql(
            "SELECT snapshot_date, AVG(iv) AS avg_iv, COUNT(*) AS contracts "
            "FROM oi_snapshots "
            "WHERE symbol=? AND snapshot_date>=? AND iv IS NOT NULL "
            "  AND expiration > snapshot_date "
            "GROUP BY snapshot_date ORDER BY snapshot_date",
            conn, params=(symbol, cutoff),
        )
    return df
