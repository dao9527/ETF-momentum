#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""
ETF rotation strategy - weight signal version (final stable)
"""

import os
import logging
import requests
import pandas as pd
import time
from datetime import datetime
from urllib.parse import quote

# ======================== Optional dependencies ========================

try:
    import baostock as baostock_lib
    BAOSTOCK_AVAILABLE = True
except ImportError:
    BAOSTOCK_AVAILABLE = False

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False

# ======================== Config ========================

BARK_KEY = os.getenv("BARK_KEY", "")

CONFIG = {

    "INDEX_CODE": "000300",

    # Trend parameters
    "MA": 200,
    "MOM": 60,
    "VOL": 20,

    # Position settings
    "TOP_N": 2,
    "MAX_SINGLE": 0.4,

    # Liquidity filter (CNY)
    "MIN_AMOUNT": 5e7,

    # Retry settings
    "RETRY_TIMES": 3,
    "RETRY_SLEEP": 2,
}

# ======================== ETF Pool ========================

ETF_POOL = {
    "159875": "新能源",
    "512480": "半导体",
    "515980": "人工智能",
    "588000": "科创50",
    "512000": "券商",
    "512170": "医药",
    "159928": "消费",
    "515080": "红利",
    "512660": "军工",
    "512400": "有色金属",
}

# ======================== Logging ========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ======================== Push ========================

def push(msg):
    """Send Bark notification."""

    if not BARK_KEY:
        return

    try:
        parts = [msg[i:i + 180] for i in range(0, len(msg), 180)]

        for p in parts:
            encoded = quote(p, safe="")

            requests.get(
                f"https://api.day.app/{BARK_KEY}/{encoded}",
                timeout=10
            )

            time.sleep(0.5)

    except Exception as e:
        logging.warning(f"Bark push failed: {e}")

# ======================== Helpers ========================

def is_trading_day():
    return datetime.now().weekday() < 5

def retry(func, *args, **kwargs):

    times = CONFIG["RETRY_TIMES"]
    sleep = CONFIG["RETRY_SLEEP"]
    last_exc = None

    for attempt in range(times):

        try:
            return func(*args, **kwargs)

        except Exception as e:
            last_exc = e
            logging.warning(f"Attempt {attempt + 1}/{times} failed: {e}")

            if attempt < times - 1:
                time.sleep(sleep)

    raise last_exc

def validate_data(df):

    if df is None:
        return False

    if len(df) < 30:
        return False

    try:
        df.index = pd.to_datetime(df.index)
    except Exception:
        return False

    if (datetime.now().date() - df.index[-1].date()).days > 5:
        return False

    if "close" not in df.columns:
        return False

    if df["close"].isna().any():
        return False

    if df["close"].iloc[-1] <= 0:
        return False

    return True

# ======================== ETF Data ========================

def _fetch_etf_akshare(code):

    df = ak.fund_etf_hist_em(
        symbol=code,
        period="daily"
    )

    if df is None or df.empty:
        return None

    df.rename(columns={
        "日期": "date",
        "收盘": "close",
        "成交额": "amount",
        "成交金额": "amount",
    }, inplace=True)

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    if "amount" in df.columns:
        amt = pd.to_numeric(df["amount"], errors="coerce")

        if amt.tail(20).mean() < CONFIG["MIN_AMOUNT"]:
            logging.info(f"ETF {code} liquidity too low, skipped")
            return None

    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    return df[["close"]] if validate_data(df) else None

def _fetch_etf_baostock(code, bs_session):

    bs_code = (
        f"sh.{code}"
        if code.startswith("51") or code.startswith("58")
        else f"sz.{code}"
    )

    rs = bs_session.query_history_k_data_plus(
        bs_code,
        "date,close,volume",
        start_date="2015-01-01",
        end_date=datetime.now().strftime("%Y-%m-%d"),
    )

    df = rs.get_data()

    if df.empty:
        return None

    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    df = df.dropna().set_index("date")

    df["amount"] = df["close"] * df["volume"]

    if df["amount"].tail(20).mean() < CONFIG["MIN_AMOUNT"]:
        logging.info(f"ETF {code} (baostock) liquidity too low")
        return None

    return df[["close"]] if validate_data(df) else None

def get_etf(code, bs_session=None):

    if AKSHARE_AVAILABLE:
        try:
            df = retry(_fetch_etf_akshare, code)

            if df is not None:
                return df

        except Exception as e:
            logging.warning(f"AKShare ETF failed {code}: {e}")

    if bs_session is not None:
        try:
            df = retry(_fetch_etf_baostock, code, bs_session)

            if df is not None:
                return df

        except Exception as e:
            logging.warning(f"Baostock ETF failed {code}: {e}")

    return None

# ======================== Index Data ========================

def _fetch_index_akshare():

    df = ak.stock_zh_index_daily_em(
        symbol=CONFIG["INDEX_CODE"]
    )

    if df is None or df.empty:
        return None

    df.rename(columns={
        "日期": "date",
        "收盘": "close"
    }, inplace=True)

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")

    df["close"] = pd.to_numeric(
        df["close"],
        errors="coerce"
    )

    df = df[["close"]]

    return df if validate_data(df) else None

def _fetch_index_baostock(bs_session):

    rs = bs_session.query_history_k_data_plus(
        f"sh.{CONFIG['INDEX_CODE']}",
        "date,close",
        start_date="2015-01-01",
        end_date=datetime.now().strftime("%Y-%m-%d"),
    )

    df = rs.get_data()

    if df.empty:
        return None

    df["date"] = pd.to_datetime(df["date"])

    df["close"] = pd.to_numeric(
        df["close"],
        errors="coerce"
    )

    df = df.dropna().set_index("date")

    return df[["close"]] if validate_data(df) else None

def get_index(bs_session=None):

    try:
        df = get_etf("510300", bs_session)

        if df is not None:
            return df, "ETF(510300)"

    except Exception as e:
        logging.warning(f"Index source ETF failed: {e}")

    if AKSHARE_AVAILABLE:
        try:
            df = retry(_fetch_index_akshare)

            if df is not None:
                return df, "AKSHARE"

        except Exception as e:
            logging.warning(f"AKShare index failed: {e}")

    if bs_session is not None:
        try:
            df = retry(_fetch_index_baostock, bs_session)

            if df is not None:
                return df, "BAOSTOCK"

        except Exception as e:
            logging.warning(f"Baostock index failed: {e}")

    push("All index sources failed")

    return None, "FAIL"

# ======================== Signals ========================

def momentum(df):

    if len(df) < CONFIG["MOM"]:
        return None

    try:
        ret = (
            df["close"].iloc[-1]
            / df["close"].iloc[-CONFIG["MOM"]]
            - 1
        )

        vol = (
            df["close"]
            .pct_change()
            .tail(CONFIG["VOL"])
            .std()
        )

        return ret / max(vol, 0.001)

    except Exception:
        return None

def market_coef(series):

    available = len(series)

    if available < CONFIG["MA"]:
        logging.warning(
            f"Index data too short: {available} rows "
            f"(need {CONFIG['MA']}), coef=0.5"
        )
        return 0.5

    ma_val = series.rolling(CONFIG["MA"]).mean().iloc[-1]

    if pd.isna(ma_val):
        logging.warning("MA200 is NaN, coef=0.5")
        return 0.5

    dev = (series.iloc[-1] - ma_val) / ma_val

    logging.info(
        f"MA200={ma_val:.4f}, "
        f"last={series.iloc[-1]:.4f}, "
        f"dev={dev:.2%}"
    )

    if dev < -0.10:
        return 0.0

    if dev < -0.06:
        return 0.3

    if dev < -0.03:
        return 0.6

    return 1.0

# ======================== Main ========================

def main():

    if not is_trading_day():
        logging.info("Weekend, skipping")
        return

    bs_session = None

    if BAOSTOCK_AVAILABLE:
        try:
            result = baostock_lib.login()

            if result.error_code == "0":
                bs_session = baostock_lib
                logging.info("Baostock login OK")

            else:
                logging.warning(
                    f"Baostock login failed: {result.error_msg}"
                )

        except Exception as e:
            logging.warning(f"Baostock init failed: {e}")

    try:

        idx, source = get_index(bs_session)

        if idx is None:
            logging.error("Cannot fetch index data")
            return

        logging.info(
            f"Index rows: {len(idx)}, "
            f"last date: {idx.index[-1].date()}"
        )

        mcoef = market_coef(idx["close"])

        etfs = {}

        for code, name in ETF_POOL.items():

            df = get_etf(code, bs_session)

            if df is not None:
                etfs[code] = {
                    "name": name,
                    "df": df
                }

            else:
                logging.warning(
                    f"ETF {code}({name}) unavailable"
                )

        total = len(ETF_POOL)
        valid = len(etfs)

        if valid < total * 0.5:
            push(f"Data warning: {valid}/{total} ETFs valid")
            return

        scores = []

        for code, info in etfs.items():

            mom = momentum(info["df"])

            if mom is not None:
                scores.append((code, mom))

        scores.sort(
            key=lambda x: x[1],
            reverse=True
        )

        selected = scores[:CONFIG["TOP_N"]]

        targets = {}

        if selected and mcoef > 0:

            weight = min(
                mcoef / len(selected),
                CONFIG["MAX_SINGLE"]
            )

            for code, score in selected:
                targets[code] = {
                    "name": ETF_POOL[code],
                    "weight": weight,
                    "score": round(score, 2),
                }

        today_str = datetime.now().strftime("%Y-%m-%d")

        msg = f"ETF Signal {today_str}\n"
        msg += f"Index: {source}\n"
        msg += f"ETFs: {valid}/{total}\n"
        msg += f"Market coef: {mcoef:.2f}\n\n"

        if not targets:
            msg += "Hold cash"

        else:
            msg += "Target weights:\n"

            total_weight = 0.0

            for code, info in targets.items():

                w = info["weight"]

                total_weight += w

                msg += (
                    f"{info['name']}({code}) "
                    f"{w:.0%}\n"
                )

            cash = 1.0 - total_weight

            if cash > 0.001:
                msg += f"Cash {cash:.0%}"

        push(msg)

        logging.info(msg)

    except Exception as e:

        logging.exception("Main loop error")

        push(f"Error: {str(e)[:120]}")

    finally:

        if bs_session is not None:
            try:
                baostock_lib.logout()

            except Exception:
                pass

if __name__ == "__main__":
    main()
