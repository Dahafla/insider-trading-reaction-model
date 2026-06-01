# src/run_analysis.py

import os
from datetime import datetime

import pandas as pd
import matplotlib.pyplot as plt

from quant_insider_core import (
    load_insider_buys,
    tag_large_buys,
    compute_forward_returns,
    summarize_returns,
)

# ---------------------- PARAMETERS ----------------------
MIN_VALUE = 10_000
START = "2024-01-01"
END = "2025-01-01"
QUANTILE = 0.75
HORIZON = 10
OUTPUT_DIR = "output"
EXCEL_FILENAME = "insider_analysis.xlsx"
GENERATE_CHARTS = False
os.makedirs(OUTPUT_DIR, exist_ok=True)


def plot_bucket_bar(summary: pd.DataFrame, horizon: int, output_path: str):
    order = ["normal_buy", "large_buy"]
    summary = summary.set_index("size_bucket")
    summary = summary.reindex(order).dropna().reset_index()

    labels = summary["size_bucket"].values
    counts = summary["count"].values

    # plot in percent for readability
    means_pct = (summary["mean"] * 100).values

    # optional uncertainty: standard error if std exists
    yerr = None
    if "std" in summary.columns:
        std_pct = (summary["std"] * 100).values
        yerr = std_pct / (counts ** 0.5)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(labels, means_pct, yerr=yerr, capsize=4 if yerr is not None else 0)
    ax.axhline(0, linewidth=1)

    ax.set_ylabel(f"Mean {horizon}-Day Return (%)")
    ax.set_title(f"Mean {horizon}-Day Forward Return\nLarge vs Normal Insider Buys")

    for x, y, n in zip(labels, means_pct, counts):
        va = "bottom" if y >= 0 else "top"
        ax.text(x, y, f"n={n}", ha="center", va=va, fontsize=9)

    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def equity_by_calendar(results: pd.DataFrame, horizon: int):
    col = f"ret_{horizon}d"
    df = results.dropna(subset=[col]).copy()

    # Use trade_date + horizon as "exit date" for the trade
    df["exit_date"] = df["trade_date"] + pd.Timedelta(days=horizon)

    # Average return of all trades ending on the same date
    daily = (
        df.groupby("exit_date")[col]
        .mean()
        .sort_index()
    )

    equity = (1 + daily).cumprod()
    return equity

def max_drawdown(equity: pd.Series) -> float:
    roll_max = equity.cummax()
    drawdown = (equity / roll_max) - 1.0
    return drawdown.min()  # negative


def plot_equity_curve(results: pd.DataFrame, horizon: int, output_path: str):
    col = f"ret_{horizon}d"
    rets = results[col]
    equity = equity_by_calendar(results, HORIZON)
    if equity.empty:
        return

    mdd = max_drawdown(equity)

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(equity.index, equity.values)
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative Return")
    ax.set_title("Equity Curve – Calendar-Time Strategy")
    ax.text(
    0.01, 0.02, f"MDD: {mdd:.2%}",
    transform=ax.transAxes, ha="left", va="bottom", fontsize=9
)
    ax.grid(True, linewidth=0.5)

    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


def _format_events_sheet(results: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Prepare trade-level rows for Excel with readable columns."""
    col = f"ret_{horizon}d"
    df = results.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    if col in df.columns:
        df[f"{horizon}d_return_pct"] = (df[col] * 100).round(4)
    column_order = [
        "ticker",
        "company_name",
        "trade_date",
        "insider_name",
        "insider_role",
        "shares",
        "price",
        "value_usd",
        "size_bucket",
        col,
        f"{horizon}d_return_pct",
    ]
    existing = [c for c in column_order if c in df.columns]
    extra = [c for c in df.columns if c not in existing]
    return df[existing + extra]


def _parameters_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"parameter": "min_value_usd", "value": MIN_VALUE},
            {"parameter": "start_date", "value": START},
            {"parameter": "end_date", "value": END},
            {"parameter": "large_buy_quantile", "value": QUANTILE},
            {"parameter": "forward_horizon_days", "value": HORIZON},
            {"parameter": "generated_at", "value": datetime.now().isoformat(timespec="seconds")},
        ]
    )


def export_to_excel(
    results: pd.DataFrame,
    summary: pd.DataFrame,
    stats: dict,
    horizon: int,
    output_path: str,
) -> str:
    """
    Write the full analysis to a multi-sheet Excel workbook.
    """
    events = _format_events_sheet(results, horizon)
    params_df = _parameters_frame()

    summary_out = summary.copy()
    if "mean" in summary_out.columns:
        summary_out["mean_return_pct"] = (summary_out["mean"] * 100).round(4)
    if "median" in summary_out.columns:
        summary_out["median_return_pct"] = (summary_out["median"] * 100).round(4)

    equity = equity_by_calendar(results, horizon)
    equity_df = equity.reset_index()
    if len(equity_df.columns) == 2:
        equity_df.columns = ["date", "cumulative_return"]
    if not equity.empty:
        stats = {**stats, "max_drawdown": max_drawdown(equity)}

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        events.to_excel(writer, sheet_name="Events", index=False)
        summary_out.to_excel(writer, sheet_name="Summary", index=False)
        equity_df.to_excel(writer, sheet_name="Calendar Equity", index=False)
        params_df.to_excel(writer, sheet_name="Parameters", index=False)

    return output_path


def plot_return_distribution(results: pd.DataFrame, horizon: int, output_path: str):
    col = f"ret_{horizon}d"
    rets = results[col].dropna()

    rets_pct = rets * 100
    lo, hi = rets_pct.quantile([0.01, 0.99])


    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist(rets_pct, bins=40)
    ax.axvline(rets_pct.mean(), linestyle="--", linewidth=1)
    
    ax.set_xlim(lo, hi)
    ax.set_xlabel(f"{horizon}-Day Return (%)")
    ax.set_ylabel("Number of Trades")
    ax.set_title(f"Distribution of {horizon}-Day Returns")
    ax.legend()

    try:
        fig.tight_layout()
    except:
        plt.subplots_adjust(top=0.88)

    fig.savefig(output_path)
    plt.close(fig)


def main():
    print("Loading insider buy data...")
    buys = load_insider_buys(
        min_value_usd=MIN_VALUE,
        start_date=START,
        end_date=END,
    )

    if buys.empty:
        print("No insider buys found for this filter.")
        return

    print(f"Loaded {len(buys)} insider purchase records.")

    print("Tagging large vs normal buys...")
    buys = tag_large_buys(buys, QUANTILE)

    print("Computing forward returns...")
    results = compute_forward_returns(buys, HORIZON)

    if results.empty:
        print("No valid forward returns. Try different parameters.")
        return

    summary, stats = summarize_returns(results, HORIZON)

    print("\n===== SUMMARY =====")
    print(summary)
    print("\n===== STATS =====")
    for k, v in stats.items():
        print(f"{k}: {v}")

    excel_path = os.path.join(OUTPUT_DIR, EXCEL_FILENAME)
    export_to_excel(results, summary, stats, HORIZON, excel_path)
    print(f"\nExcel report saved: {excel_path}")

    if GENERATE_CHARTS:
        charts_dir = os.path.join(OUTPUT_DIR, "charts")
        os.makedirs(charts_dir, exist_ok=True)
        plot_bucket_bar(summary, HORIZON, f"{charts_dir}/large_vs_normal.png")
        plot_equity_curve(results, HORIZON, f"{charts_dir}/equity_curve.png")
        plot_return_distribution(results, HORIZON, f"{charts_dir}/return_dist.png")
        print("Charts saved in:", charts_dir)


if __name__ == "__main__":
    main()
