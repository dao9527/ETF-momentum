#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ETF Trend Rotation Strategy
============================

架构：
    低频调仓 + 高频风控 + 每日健康检查

每日：
    - 获取指数数据
    - 检查 ETF 数据健康状态
    - 风控检查（MA200）
    - Bark 心跳推送

调仓日（每月第1、第3个周五）：
    - 运行完整轮动逻辑
    - 推送正式调仓信号

核心逻辑：
    - 沪深300 MA200 → 风险开关
    - 60日风险调整动量 → ETF 选择
    - 半月调仓 → 降低换手率

设计原则：
    - 信号周期与执行周期统一
    - 不确定时保守（coef=0.5）
    - GitHub Actions 无状态兼容
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

    # Trend
    "MA": 200,
    "MOM": 60,
    "VOL": 60,   # 修复：波动率周期与动量周期统一

    # Position
    "TOP_N": 2,
    "MAX_SINGLE": 0.4,

    # Liquidity
    "MIN_AMOUNT": 5e7,

    # Retry
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

def push(title, body=None):

    if not BARK_KEY:
        return

    content = f"{title}\n{body}" if body else title

    try:

        parts = [
            content[i:i + 180]
            for i in range(0, len(content), 180)
        ]

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

def is_trading_day(idx_df):
    """
    用指数最新数据日期判断是否真正开盘。
    避免节假日误判。
    """

    if idx_df is None or idx_df.empty:
        return False

    last_date = idx_df.index[-1].date()
    today = datetime.now().date()

    return last_date == today


def is_rebalance_day():
    """
    每月第1、第3个周五
    """

    today = datetime.now()

    if today.weekday() != 4:
        return False

    friday_count = sum(
        1 for d in range(1, today.day + 1)
        if datetime(
            today.year,
            today.month,
            d
        ).weekday() == 4
    )

    return friday_count in (1, 3)


def retry(func, *args, **kwargs):

    times = CONFIG["RETRY_TIMES"]
    sleep = CONFIG["RETRY_SLEEP"]

    last_exc = None

    for attempt in range(times):

        try:
            return func(*args, **kwargs)

        except Exception as e:

            last_exc = e

            logging.warning(
                f"Attempt {attempt + 1}/{times} failed: {e}"
            )

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

    if (
        datetime.now().date() - df.index[-1].date()
    ).days > 5:
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

    today = datetime.now().strftime("%Y%m%d")

    try:

        df = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date="20200101",
            end_date=today,
            adjust="qfq"
        )

    except Exception:

        df = ak.fund_etf_hist_em(
            symbol=code
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

        amt = pd.to_numeric(
            df["amount"],
            errors="coerce"
        )

        if amt.tail(20).mean() < CONFIG["MIN_AMOUNT"]:

            logging.info(
                f"ETF {code} liquidity too low"
            )

            return None

    df["close"] = pd.to_numeric(
        df["close"],
        errors="coerce"
    )

    logging.info(
        f"ETF {code} rows={len(df)} "
        f"last={df.index[-1].date()}"
    )

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

    df["close"] = pd.to_numeric(
        df["close"],
        errors="coerce"
    )

    df["volume"] = pd.to_numeric(
        df["volume"],
        errors="coerce"
    )

    df = df.dropna().set_index("date")

    df["amount"] = (
        df["close"] * df["volume"]
    )

    if (
        df["amount"].tail(20).mean()
        < CONFIG["MIN_AMOUNT"]
    ):

        logging.info(
            f"ETF {code} liquidity too low"
        )

        return None

    return df[["close"]] if validate_data(df) else None


def get_etf(code, bs_session=None):

    if AKSHARE_AVAILABLE:

        try:

            df = retry(
                _fetch_etf_akshare,
                code
            )

            if df is not None:
                return df

        except Exception as e:

            logging.warning(
                f"AKShare ETF failed {code}: {e}"
            )

    if bs_session is not None:

        try:

            df = retry(
                _fetch_etf_baostock,
                code,
                bs_session
            )

            if df is not None:
                return df

        except Exception as e:

            logging.warning(
                f"Baostock ETF failed {code}: {e}"
            )

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

    return df[["close"]] if validate_data(df) else None


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

        df = get_etf(
            "510300",
            bs_session
        )

        if df is not None:
            return df, "ETF(510300)"

    except Exception as e:

        logging.warning(
            f"Index ETF source failed: {e}"
        )

    if AKSHARE_AVAILABLE:

        try:

            df = retry(_fetch_index_akshare)

            if df is not None:
                return df, "AKSHARE"

        except Exception as e:

            logging.warning(
                f"AKShare index failed: {e}"
            )

    if bs_session is not None:

        try:

            df = retry(
                _fetch_index_baostock,
                bs_session
            )

            if df is not None:
                return df, "BAOSTOCK"

        except Exception as e:

            logging.warning(
                f"Baostock index failed: {e}"
            )

    push(
        "❌ Index Data Failed",
        "All sources unavailable"
    )

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

        # 修复：60/60 周期统一
        vol = (
            df["close"]
            .pct_change()
            .rolling(CONFIG["VOL"])
            .std()
            .iloc[-1]
        )

        return ret / max(vol, 0.001)

    except Exception:

        return None


def market_coef(series):

    available = len(series)

    if available < 60:

        logging.warning(
            f"Index only {available} rows, "
            f"coef=0.5 defensive"
        )

        return 0.5

    ma_period = min(
        CONFIG["MA"],
        available
    )

    ma_val = (
        series
        .rolling(ma_period)
        .mean()
        .iloc[-1]
    )

    if pd.isna(ma_val):

        logging.warning(
            "MA is NaN, coef=0.5 defensive"
        )

        return 0.5

    dev = (
        series.iloc[-1] - ma_val
    ) / ma_val

    logging.info(
        f"MA{ma_period}={ma_val:.4f} "
        f"last={series.iloc[-1]:.4f} "
        f"dev={dev:.2%}"
    )

    # 保留阶梯结构（趋势系统更果断）

    if dev < -0.10:
        return 0.0

    if dev < -0.06:
        return 0.3

    if dev < -0.03:
        return 0.6

    return 1.0

# ======================== Risk ========================

def run_risk_check(idx_series):

    mcoef = market_coef(idx_series)

    if mcoef == 0.0:

        push(
            "🚨 Risk Alert",
            "Suggested: CLEAR ALL"
        )

    elif mcoef == 0.3:

        push(
            "⚠️ Risk Alert",
            "Suggested exposure: 30%"
        )

    elif mcoef == 0.6:

        push(
            "📉 Risk Alert",
            "Suggested exposure: 60%"
        )

    elif mcoef == 0.5:

        push(
            "❓ Data Warning",
            "Index data abnormal, defensive mode"
        )

    else:

        logging.info(
            "Risk check OK"
        )

    return mcoef

# ======================== Health Check ========================

def run_health_check(bs_session=None):

    valid = 0
    total = len(ETF_POOL)

    for code in ETF_POOL:

        try:

            df = get_etf(code, bs_session)

            if df is not None:
                valid += 1

        except Exception:
            pass

    today = datetime.now().strftime("%Y-%m-%d")

    if valid == total:

        push(
            "💚 Strategy OK",
            f"{today}\n"
            f"ETF data OK: {valid}/{total}"
        )

    else:

        push(
            "⚠️ ETF Data Warning",
            f"{today}\n"
            f"ETF valid: {valid}/{total}"
        )

    return valid

# ======================== Rebalance ========================

def run_rebalance(etfs, mcoef, idx_source):

    scores = []

    for code, info in etfs.items():

        mom = momentum(info["df"])

        if mom is not None:

            scores.append(
                (code, mom)
            )

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

    today = datetime.now().strftime("%Y-%m-%d")

    body = (
        f"Date: {today}\n"
        f"Index: {idx_source}\n"
        f"Market coef: {mcoef:.2f}\n\n"
    )

    if not targets:

        body += "Target: CASH 100%"

    else:

        body += "Target weights:\n"

        total_weight = 0.0

        for code, info in targets.items():

            w = info["weight"]

            total_weight += w

            body += (
                f"{info['name']}({code}) "
                f"{w:.0%} "
                f"[score={info['score']}]\n"
            )

        cash = 1.0 - total_weight

        if cash > 0.001:

            body += (
                f"Cash {cash:.0%}"
            )

    push(
        "📊 Rebalance Signal",
        body
    )

    logging.info(body)

# ======================== Main ========================


def main():

    bs_session = None

    if BAOSTOCK_AVAILABLE:

        try:

            result = baostock_lib.login()

            if result.error_code == "0":

                bs_session = baostock_lib

                logging.info(
                    "Baostock login OK"
                )

        except Exception as e:

            logging.warning(
                f"Baostock init failed: {e}"
            )

    try:

        # ========================
        # 获取指数
        # ========================

        idx, idx_source = get_index(bs_session)

        if idx is None:

            logging.error(
                "Cannot fetch index"
            )

            return

        # ========================
        # 真正交易日判断
        # ========================

        if not is_trading_day(idx):

            logging.info(
                "Market closed today"
            )
            push("⏸ Market Closed", str(datetime.now().date()))

            return

        logging.info(
            f"Index rows={len(idx)} "
            f"source={idx_source}"
        )

        # ========================
        # Daily risk
        # ========================

        mcoef = run_risk_check(
            idx["close"]
        )

        # ========================
        # Daily heartbeat
        # ========================

        valid = run_health_check(
            bs_session
        )

        if valid < len(ETF_POOL) * 0.5:

            logging.warning(
                "Too many ETF failures"
            )

            return

        # ========================
        # Rebalance
        # ========================

        if is_rebalance_day():

            logging.info(
                "Rebalance day"
            )

            etfs = {}

            for code, name in ETF_POOL.items():

                df = get_etf(
                    code,
                    bs_session
                )

                if df is not None:

                    etfs[code] = {
                        "name": name,
                        "df": df
                    }

            if len(etfs) < len(ETF_POOL) * 0.5:

                push(
                    "⚠️ Rebalance Skipped",
                    "Too many ETF failures"
                )

                return

            run_rebalance(
                etfs,
                mcoef,
                idx_source
            )

        else:

            logging.info(
                "Non-rebalance day"
            )

    except Exception as e:

        logging.exception(
            "Main loop error"
        )

        push(
            "❌ Strategy Error",
            str(e)[:200]
        )

    finally:

        if bs_session is not None:

            try:
                baostock_lib.logout()
            except Exception:
                pass

if __name__ == "__main__":
    main()