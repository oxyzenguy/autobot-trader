"""Presentation-only trade selection for dashboard charts and timelines."""

from __future__ import annotations

from typing import TypedDict

import pandas as pd


TimelineItem = TypedDict("TimelineItem", {
    "상태": str, "시각": str, "구분": str, "주문가격": str,
    "수량": str, "주문금액": str, "상세 / 손익": str,
})


def filter_trades_for_candles(
    trades: pd.DataFrame, candle_index: pd.DatetimeIndex, interval: str
) -> pd.DataFrame:
    """Return chart trades without mutating the source trade history."""
    if trades.empty or candle_index.empty or "timestamp" not in trades.columns:
        return trades.iloc[:0].copy()
    selected = trades.copy()
    selected["dt"] = pd.to_datetime(selected["timestamp"], errors="coerce")
    candle_widths = {
        "minute60": pd.DateOffset(hours=1),
        "minute240": pd.DateOffset(hours=4),
        "day": pd.DateOffset(days=1),
        "week": pd.DateOffset(weeks=1),
        "month": pd.DateOffset(months=1),
    }
    window_end = candle_index.max() + candle_widths[interval]
    return selected[(selected["dt"] >= candle_index.min())
                    & (selected["dt"] < window_end)]


def sort_timeline_items(items: list[TimelineItem]) -> list[TimelineItem]:
    """Show newest events first, irrespective of pending/completed status."""
    return sorted(items, key=lambda item: item["시각"], reverse=True)
