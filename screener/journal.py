"""
Trade Journal — SQLite-backed storage for option plays.

Tracks both real positions (Entered) and paper plays (Watching).
Reprices open entries using Black-Scholes + live yfinance spot price.
"""

import sqlite3
import math
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "journal.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS journal (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                added_date         TEXT NOT NULL,
                symbol             TEXT NOT NULL,
                strategy           TEXT,
                setup              TEXT,
                signal             TEXT,
                composite_score    REAL,
                strike             REAL,
                contract_type      TEXT,
                expiry             TEXT,
                dte_at_entry       INTEGER,
                entry_premium      REAL,
                entry_delta        REAL,
                entry_iv           REAL,
                stock_price_entry  REAL,
                technical_target   REAL,
                analyst_target     REAL,
                option_breakeven   REAL,
                pct_to_breakeven   REAL,
                status             TEXT DEFAULT 'Watching',
                exit_price         REAL,
                exit_date          TEXT,
                realized_pnl       REAL,
                realized_pnl_pct   REAL,
                notes              TEXT
            )
        """)


_init_db()


def add_trade_to_journal(r, status: str = "Watching", notes: str = "") -> int:
    """Save a ScreenerResult to the journal. Returns the new row id."""
    mid = (r.rec_bid + r.rec_ask) / 2 if r.rec_bid and r.rec_ask else None
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO journal (
                added_date, symbol, strategy, setup, signal, composite_score,
                strike, contract_type, expiry, dte_at_entry,
                entry_premium, entry_delta, entry_iv, stock_price_entry,
                technical_target, analyst_target, option_breakeven, pct_to_breakeven,
                status, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            r.symbol,
            r.strategy,
            r.setup,
            r.signal,
            r.composite,
            r.rec_strike,
            r.contract_type,
            _expiry_from_dte(r.rec_dte),
            r.rec_dte,
            mid,
            r.rec_delta,
            r.rec_iv,
            r.price,
            r.technical_target,
            r.analyst_target,
            r.option_breakeven,
            r.pct_to_breakeven,
            status,
            notes,
        ))
        return cur.lastrowid


def add_entry_raw(
    symbol: str,
    strategy: str,
    setup: str,
    signal: str,
    strike: Optional[float],
    contract_type: str,
    expiry: Optional[str],
    dte: Optional[int],
    entry_premium: Optional[float],
    delta: Optional[float],
    iv: Optional[float],
    stock_price: Optional[float],
    technical_target: Optional[float] = None,
    analyst_target: Optional[float] = None,
    option_breakeven: Optional[float] = None,
    pct_to_breakeven: Optional[float] = None,
    composite_score: Optional[float] = None,
    status: str = "Watching",
    notes: str = "",
) -> int:
    """Save a trade directly without a ScreenerResult. Used by Trade Decision panel."""
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO journal (
                added_date, symbol, strategy, setup, signal, composite_score,
                strike, contract_type, expiry, dte_at_entry,
                entry_premium, entry_delta, entry_iv, stock_price_entry,
                technical_target, analyst_target, option_breakeven, pct_to_breakeven,
                status, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            symbol, strategy, setup, signal, composite_score,
            strike, contract_type, expiry, dte,
            entry_premium, delta, iv, stock_price,
            technical_target, analyst_target, option_breakeven, pct_to_breakeven,
            status, notes,
        ))
        return cur.lastrowid


def get_entries(status_filter: str = "All") -> pd.DataFrame:
    """Load journal entries. status_filter: All | Watching | Entered | Closed."""
    with _conn() as con:
        if status_filter == "All":
            rows = con.execute("SELECT * FROM journal ORDER BY added_date DESC").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM journal WHERE status=? ORDER BY added_date DESC",
                (status_filter,)
            ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def close_trade_position(entry_id: int, exit_price: float) -> None:
    """Mark an entry as Closed with an exit price and compute realized P&L."""
    with _conn() as con:
        row = con.execute("SELECT entry_premium, strategy FROM journal WHERE id=?",
                          (entry_id,)).fetchone()
        if not row:
            return
        entry_premium = row["entry_premium"] or 0
        realized_pnl     = exit_price - entry_premium
        realized_pnl_pct = (realized_pnl / entry_premium * 100) if entry_premium else None
        con.execute("""
            UPDATE journal
            SET status='Closed', exit_price=?, exit_date=?,
                realized_pnl=?, realized_pnl_pct=?
            WHERE id=?
        """, (exit_price, date.today().isoformat(), realized_pnl, realized_pnl_pct, entry_id))


def delete_entry(entry_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM journal WHERE id=?", (entry_id,))


def update_notes(entry_id: int, notes: str) -> None:
    with _conn() as con:
        con.execute("UPDATE journal SET notes=? WHERE id=?", (notes, entry_id))


def reprice_trade_with_blackscholes(row: dict) -> dict:
    """
    Re-estimate current option value via Black-Scholes using live spot price.
    Returns a dict with keys: current_stock, current_premium, unrealized_pnl, unrealized_pnl_pct, dte_remaining.
    """
    result = {
        "current_stock":    None,
        "current_premium":  None,
        "unrealized_pnl":   None,
        "unrealized_pnl_pct": None,
        "dte_remaining":    None,
        "expired":          False,
    }
    try:
        import yfinance as yf
        info = yf.Ticker(row["symbol"]).info
        spot = info.get("regularMarketPrice") or info.get("currentPrice")
        if not spot:
            return result
        result["current_stock"] = spot

        expiry = row.get("expiry")
        if not expiry:
            return result
        exp_date = datetime.strptime(expiry, "%Y-%m-%d")
        dte = max(0, (exp_date - datetime.now()).days)
        result["dte_remaining"] = dte

        if dte == 0:
            result["expired"] = True
            return result

        iv = row.get("entry_iv") or 0.30
        strike = row.get("strike")
        flag   = "c" if row.get("contract_type") == "call" else "p"

        price = _bs_price(flag, spot, strike, dte / 365.0, iv)
        if price is not None:
            result["current_premium"] = round(price, 2)
            entry = row.get("entry_premium") or 0
            if entry > 0:
                result["unrealized_pnl"]     = round(price - entry, 2)
                result["unrealized_pnl_pct"] = round((price - entry) / entry * 100, 1)
    except Exception as e:
        logger.warning(f"Reprice failed for {row.get('symbol')}: {e}")
    return result


def reprice_all_open() -> dict[int, dict]:
    """Reprice every open (Watching/Entered) journal entry. Returns {id: reprice_dict}."""
    df = get_entries("All")
    if df.empty:
        return {}
    open_df = df[df["status"].isin(["Watching", "Entered"])]
    results = {}
    for _, row in open_df.iterrows():
        results[int(row["id"])] = reprice_trade_with_blackscholes(row.to_dict())
    return results


def _expiry_from_dte(dte: Optional[int]) -> Optional[str]:
    if dte is None:
        return None
    from datetime import timedelta
    return (date.today() + timedelta(days=dte)).isoformat()


def _bs_price(flag: str, S: float, K: float, t: float, iv: float, r: float = 0.05) -> Optional[float]:
    if t <= 0 or iv <= 0 or not S or not K:
        return None
    try:
        from scipy.stats import norm
        d1 = (math.log(S / K) + (r + 0.5 * iv**2) * t) / (iv * math.sqrt(t))
        d2 = d1 - iv * math.sqrt(t)
        if flag == "c":
            return max(0.0, S * norm.cdf(d1) - K * math.exp(-r * t) * norm.cdf(d2))
        return max(0.0, K * math.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1))
    except Exception:
        return None
