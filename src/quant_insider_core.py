# src/quant_insider_core.py

import math
import pandas as pd
from sqlalchemy import create_engine
from config import DB_URL

# Create a global engine
engine = create_engine(DB_URL)


# ----------------------------------------------------
# 1. LOAD INSIDER BUYS FROM DB
# ----------------------------------------------------
def load_insider_buys(
    min_value_usd: float = 10_000,
    start_date: str = "2024-01-01",
    end_date: str = "2025-01-01",
) -> pd.DataFrame:
    """
    Load insider purchase transactions (Form 4 open-market buys)
    from the insider_transactions table.

    Filters:
    - transaction_type = 'P'  (open-market purchases)
    - value_usd >= min_value_usd
    - trade_date in [start_date, end_date)
    """
    query = """
        SELECT
            ticker,
            company_name,
            trade_date,
            insider_name,
            insider_role,
            transaction_type,
            shares,
            price,
            value_usd
        FROM insider_transactions
        WHERE transaction_type = 'P'
          AND value_usd >= %s
          AND trade_date >= %s
          AND trade_date < %s
    """


    params = (min_value_usd, start_date, end_date)

    df = pd.read_sql(query, engine, params=params)

    if df.empty:
        return df

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    return df


# ----------------------------------------------------
# 2. TAG LARGE VS NORMAL BUYS (within each ticker)
# ----------------------------------------------------
def tag_large_buys(df: pd.DataFrame, quantile: float = 0.75) -> pd.DataFrame:
    """
    Tag each insider buy as 'large_buy' or 'normal_buy' within its ticker
    based on the value_usd quantile.
    """
    if df.empty:
        df["size_bucket"] = []
        return df

    df = df.copy()
    threshold = df.groupby("ticker")["value_usd"].transform(
        lambda s: s.quantile(quantile)
    )
    df["size_bucket"] = (df["value_usd"] >= threshold).map(
        {True: "large_buy", False: "normal_buy"}
    )
    return df


# ----------------------------------------------------
# 3. LOAD PRE-DOWNLOADED PRICES FROM daily_prices
# ----------------------------------------------------
def load_prices_from_db(
    tickers: list[str], start_date: pd.Timestamp, end_date: pd.Timestamp
) -> dict[str, pd.DataFrame]:
    """
    Load price history from the pre-populated daily_prices table.

    We keep the SQL simple and filter tickers in Python to avoid
    driver-specific array quirks.
    """
    price_dict: dict[str, pd.DataFrame] = {}

    if not tickers:
        return price_dict

    # Normalize to uppercase to match stored tickers
    tickers = sorted({t.strip().upper() for t in tickers if t})

    query = """
        SELECT ticker, trade_date, adj_close
        FROM daily_prices
        WHERE trade_date >= %s
          AND trade_date <= %s
        ORDER BY trade_date
    """

    df = pd.read_sql(query, engine, params=(start_date, end_date))

    if df.empty:
        return price_dict

    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    # Filter by tickers in Python to keep SQL generic
    df = df[df["ticker"].isin(tickers)]

    for t in tickers:
        df_t = df[df["ticker"] == t][["trade_date", "adj_close"]]
        if df_t.empty:
            continue
        df_t = df_t.set_index("trade_date").sort_index()
        price_dict[t] = df_t

    return price_dict


# ----------------------------------------------------
# 4. COMPUTE FORWARD RETURNS USING LOCAL PRICES
# ----------------------------------------------------
def compute_forward_returns(df_buys: pd.DataFrame, horizon: int = 10) -> pd.DataFrame:
    """
    For each insider buy event, compute forward returns over `horizon` days
    using prices from the daily_prices table.
    """
    if df_buys.empty:
        return df_buys

    # Determine the price range we need
    min_date = df_buys["trade_date"].min()
    max_date = df_buys["trade_date"].max()

    price_start = min_date
    price_end = max_date + pd.Timedelta(days=horizon + 5)

    # Load all needed prices from DB
    prices = load_prices_from_db(
        df_buys["ticker"].tolist(),
        price_start,
        price_end,
    )

    rows = []
    for _, row in df_buys.iterrows():
        t = row["ticker"]
        event_date = row["trade_date"]

        px = prices.get(t)
        if px is None or px.empty:
            continue

        # Align event_date to next available trading date if needed
        if event_date not in px.index:
            idx_future = px.index.searchsorted(event_date)
            if idx_future >= len(px.index):
                continue
            event_date = px.index[idx_future]

        idx = px.index.get_loc(event_date)
        if isinstance(idx, slice):
            idx = idx.start

        if idx + horizon >= len(px.index):
            # Not enough future data
            continue

        p0 = px.iloc[idx]["adj_close"]
        p_h = px.iloc[idx + horizon]["adj_close"]

        if pd.isna(p0) or pd.isna(p_h) or p0 == 0:
            continue

        ret = (p_h / p0) - 1.0

        out = row.to_dict()
        out[f"ret_{horizon}d"] = ret
        rows.append(out)

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


# ----------------------------------------------------
# 5. SUMMARIZE RESULTS (bucket-level + strategy stats)
# ----------------------------------------------------
def summarize_returns(df: pd.DataFrame, horizon: int = 10):
    """
    Summarize forward returns by size_bucket and compute simple strategy stats.
    """
    col = f"ret_{horizon}d"
    if col not in df.columns:
        return pd.DataFrame(), {}

    df = df.dropna(subset=[col])
    if df.empty:
        return pd.DataFrame(), {}

    summary = (
        df.groupby("size_bucket")[col]
        .agg(count="count", mean="mean", median="median")
        .reset_index()
    )

    rets = df[col]
    avg_trade = float(rets.mean())
    hit_rate = float((rets > 0).mean())
    sharpe = float(
        (rets.mean() / rets.std()) * math.sqrt(252 / horizon)
    ) if rets.std() else float("nan")

    stats = {
        "num_trades": len(rets),
        "avg_trade_return": avg_trade,
        "hit_rate": hit_rate,
        "approx_annualized_sharpe": sharpe,
    }

    return summary, stats
