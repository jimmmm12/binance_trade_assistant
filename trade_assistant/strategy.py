from __future__ import annotations

from .binance_client import BinanceClient
from .indicators import adx, average, ema, macd_histogram, pct_change, rsi
from .models import MarketSnapshot, Signal


def build_universe(
    client: BinanceClient,
    market: str,
    quote_asset: str,
    exclude_symbols: list[str],
    min_quote_volume: float,
    limit: int,
) -> list[MarketSnapshot]:
    tradable = client.exchange_symbols(market, quote_asset)
    tickers = client.ticker_24h(market)
    premiums = client.premium_index() if market == "futures" else {}
    rows: list[MarketSnapshot] = []
    for item in tickers:
        symbol = item["symbol"]
        if symbol not in tradable or symbol in exclude_symbols:
            continue
        quote_volume = float(item.get("quoteVolume", 0))
        if quote_volume < min_quote_volume:
            continue
        funding = None
        if market == "futures" and symbol in premiums:
            funding = float(premiums[symbol].get("lastFundingRate", 0)) * 100
        rows.append(
            MarketSnapshot(
                market=market,
                symbol=symbol,
                last=float(item["lastPrice"]),
                change_24h=float(item["priceChangePercent"]),
                quote_volume=quote_volume,
                funding_pct=funding,
            )
        )
    rows.sort(key=lambda x: x.quote_volume, reverse=True)
    return rows[:limit]


def analyze_symbol(client: BinanceClient, snapshot: MarketSnapshot) -> tuple[Signal, Signal] | None:
    try:
        k1 = client.klines(snapshot.market, snapshot.symbol, "1h", 240)
        k4 = client.klines(snapshot.market, snapshot.symbol, "4h", 240)
    except Exception:
        return None

    try:
        k1d = client.klines(snapshot.market, snapshot.symbol, "1d", 120)
    except Exception:
        k1d = []

    closes_1h = [float(row[4]) for row in k1]
    volumes_1h = [float(row[5]) for row in k1]
    closes_4h = [float(row[4]) for row in k4]
    closes_1d = [float(row[4]) for row in k1d]
    returns_1h = tuple(
        (closes_1h[index] / closes_1h[index - 1] - 1) * 100
        for index in range(max(1, len(closes_1h) - 48), len(closes_1h))
        if closes_1h[index - 1] > 0
    )

    last = snapshot.last
    ema20_1h = ema(closes_1h[-60:], 20)
    ema50_1h = ema(closes_1h[-100:], 50)
    ema20_4h = ema(closes_4h[-60:], 20)
    ema50_4h = ema(closes_4h[-100:], 50)
    ema200_4h = ema(closes_4h, 200)
    ema20_1d = ema(closes_1d[-60:], 20) if len(closes_1d) >= 50 else None
    ema50_1d = ema(closes_1d[-100:], 50) if len(closes_1d) >= 50 else None
    rsi_1h = rsi(closes_1h, 14)
    rsi_4h = rsi(closes_4h, 14)
    vol_now = volumes_1h[-2]
    vol_base = average(volumes_1h[-22:-2])
    volume_ratio = vol_now / vol_base if vol_base else 0.0
    momentum_24h = pct_change(closes_1h[-1], closes_1h[-24])
    momentum_3d = pct_change(closes_4h[-1], closes_4h[-18])
    atr_1h_pct = average_true_range_pct(k1, 14)
    atr_4h_pct = average_true_range_pct(k4, 14)
    atr_values = [value for value in [atr_1h_pct, atr_4h_pct] if value is not None]
    atr_pct = max(atr_values) if atr_values else None
    funding = snapshot.funding_pct
    adx_1h = adx(k1, 14)
    adx_4h = adx(k4, 14)
    macd_values = macd_histogram(closes_1h)
    macd_hist_1h = macd_values[-1] if macd_values else None
    macd_hist_delta_1h = macd_values[-1] - macd_values[-2] if len(macd_values) >= 2 else None
    taker_buy_ratio = _taker_buy_ratio(k1[-4:-1])
    obv_slope_pct = _obv_slope_pct(closes_1h[-24:], volumes_1h[-24:])
    support_distance_atr, resistance_distance_atr, breakout_atr = _price_location(k1, last, 14, 24)
    atr_percentile = _atr_percentile(k1, 14, 100)

    long_score = 0
    short_score = 0
    if last > ema20_1h > ema50_1h:
        long_score += 2
    if last < ema20_1h < ema50_1h:
        short_score += 2
    if last > ema20_4h > ema50_4h:
        long_score += 2
    if last < ema20_4h < ema50_4h:
        short_score += 2
    if momentum_24h > 0:
        long_score += 1
    else:
        short_score += 1
    if momentum_3d > 0:
        long_score += 1
    else:
        short_score += 1
    if volume_ratio > 1.2:
        long_score += 1
        short_score += 1
    if rsi_1h < 75:
        long_score += 1
    if rsi_1h > 25:
        short_score += 1
    if funding is None or funding < 0.05:
        long_score += 1
    if funding is not None and funding > 0:
        short_score += 1
    if rsi_1h > 80:
        long_score -= 2
    if funding is not None and funding > 0.08:
        long_score -= 2
    if rsi_1h < 20:
        short_score -= 2
    if funding is not None and funding < -0.05:
        short_score -= 2

    common = {
        "market": snapshot.market,
        "symbol": snapshot.symbol,
        "last": last,
        "change_24h": snapshot.change_24h,
        "quote_volume_m": snapshot.quote_volume / 1_000_000,
        "rsi_1h": rsi_1h,
        "rsi_4h": rsi_4h,
        "volume_ratio": volume_ratio,
        "momentum_24h": momentum_24h,
        "momentum_3d": momentum_3d,
        "funding_pct": funding,
        "atr_pct": atr_pct,
        "atr_1h_pct": atr_1h_pct,
        "atr_4h_pct": atr_4h_pct,
        "ema20_1h": ema20_1h,
        "ema50_1h": ema50_1h,
        "ema50_4h": ema50_4h,
        "ema200_4h": ema200_4h,
        "ema20_1d": ema20_1d,
        "ema50_1d": ema50_1d,
        "adx_1h": adx_1h,
        "adx_4h": adx_4h,
        "macd_hist_1h": macd_hist_1h,
        "macd_hist_delta_1h": macd_hist_delta_1h,
        "taker_buy_ratio": taker_buy_ratio,
        "obv_slope_pct": obv_slope_pct,
        "support_distance_atr": support_distance_atr,
        "resistance_distance_atr": resistance_distance_atr,
        "breakout_atr": breakout_atr,
        "atr_percentile": atr_percentile,
        "returns_1h": returns_1h,
    }
    long_note = "偏多观察：等回踩支撑不破再考虑，不要直接追高"
    short_note = "偏空观察：等反弹到压力位失败再考虑，不要暴跌后追空"
    return (
        Signal(side="long", score=long_score, note=long_note, **common),
        Signal(side="short", score=short_score, note=short_note, **common),
    )


def average_true_range_pct(klines: list, period: int = 14) -> float | None:
    if len(klines) <= period:
        return None
    true_ranges: list[float] = []
    start = len(klines) - period
    for index in range(start, len(klines)):
        high = float(klines[index][2])
        low = float(klines[index][3])
        previous_close = float(klines[index - 1][4])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    last_close = float(klines[-1][4])
    if last_close <= 0:
        return None
    return average(true_ranges) / last_close * 100


def _average_true_range_value(klines: list, period: int = 14, end: int | None = None) -> float | None:
    rows = klines if end is None else klines[:end]
    if len(rows) <= period:
        return None
    true_ranges: list[float] = []
    for index in range(len(rows) - period, len(rows)):
        high = float(rows[index][2])
        low = float(rows[index][3])
        previous_close = float(rows[index - 1][4])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return average(true_ranges)


def _price_location(
    klines: list,
    last: float,
    atr_period: int,
    lookback: int,
) -> tuple[float | None, float | None, float | None]:
    if len(klines) < lookback + 2:
        return None, None, None
    atr_value = _average_true_range_value(klines, atr_period)
    if atr_value is None or atr_value <= 0:
        return None, None, None
    reference = klines[-lookback - 1 : -1]
    support = min(float(row[3]) for row in reference)
    resistance = max(float(row[2]) for row in reference)
    return (
        round(max(0.0, last - support) / atr_value, 4),
        round(max(0.0, resistance - last) / atr_value, 4),
        round((last - resistance) / atr_value, 4),
    )


def _atr_percentile(klines: list, period: int, lookback: int) -> float | None:
    if len(klines) < period + 3:
        return None
    start = max(period + 1, len(klines) - lookback)
    samples: list[float] = []
    for end in range(start, len(klines) + 1):
        atr_value = _average_true_range_value(klines, period, end)
        close = float(klines[end - 1][4])
        if atr_value is not None and close > 0:
            samples.append(atr_value / close * 100)
    if not samples:
        return None
    current = samples[-1]
    below_or_equal = sum(value <= current for value in samples)
    return round(below_or_equal / len(samples) * 100, 2)


def _taker_buy_ratio(klines: list) -> float | None:
    total_volume = sum(float(row[5]) for row in klines if len(row) > 9)
    if total_volume <= 0:
        return None
    taker_buy = sum(float(row[9]) for row in klines if len(row) > 9)
    return round(taker_buy / total_volume, 4)


def _obv_slope_pct(closes: list[float], volumes: list[float]) -> float | None:
    if len(closes) < 4 or len(closes) != len(volumes):
        return None
    obv = 0.0
    values = [obv]
    for index in range(1, len(closes)):
        if closes[index] > closes[index - 1]:
            obv += volumes[index]
        elif closes[index] < closes[index - 1]:
            obv -= volumes[index]
        values.append(obv)
    scale = max(sum(volumes) / len(volumes), 1e-12)
    return round((values[-1] - values[0]) / scale / max(1, len(values) - 1) * 100, 4)


def scan_market(client: BinanceClient, market: str, settings: dict, top: int) -> tuple[list[Signal], list[Signal]]:
    universe = build_universe(
        client=client,
        market=market,
        quote_asset=settings["quote_asset"],
        exclude_symbols=settings["exclude_symbols"],
        min_quote_volume=float(settings["min_quote_volume"]),
        limit=top,
    )
    longs: list[Signal] = []
    shorts: list[Signal] = []
    for snapshot in universe:
        result = analyze_symbol(client, snapshot)
        if result is None:
            continue
        long_signal, short_signal = result
        longs.append(long_signal)
        shorts.append(short_signal)
    longs.sort(key=lambda x: (x.score, x.quote_volume_m), reverse=True)
    shorts.sort(key=lambda x: (x.score, x.quote_volume_m), reverse=True)
    return longs, shorts
