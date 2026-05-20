#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ETF 轮动策略 · 最终稳定版（权重信号版）

特点：
- 不再计算买多少股
- 不再跟踪 holdings/cash
- 不再累计仓位误差
- 只输出“目标仓位百分比”
- 更适合手动交易
- GitHub Actions 长期运行更稳定

你只需要：
1. 替换原 py 文件
2. 保留 workflow / requirements / secrets
3. 每天看 Bark 推送手动调仓

"""

import os
import logging
import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime
import akshare as ak

# ======================== 用户配置 ========================

BARK_KEY = os.getenv("BARK_KEY", "")

CONFIG = {
    "INDEX_CODE": "000300",

    # 趋势参数
    "MA": 200,
    "MOM": 60,
    "VOL": 20,

    # 持仓
    "TOP_N": 2,
    "MAX_SINGLE": 0.4,

    # 流动性过滤
    "MIN_AMOUNT": 5e7,
}

# ======================== ETF池（最终版） ========================

ETF_POOL = {
    "159875": "新能源",
    "512480": "半导体",
    "515980": "人工智能",
    "588000": "科创50",
    "512000": "券商",
    "512170": "医药",
    "159928": "消费",
    "515080": "红利",
    "512010": "军工",
    "159866": "有色金属",
}

# ======================== 文件 ========================

LOCK_FILE = "lock"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ======================== 工具 ========================

def push(msg):
    if not BARK_KEY:
        return

    try:
        # Bark 单条不要太长
        parts = [msg[i:i+180] for i in range(0, len(msg), 180)]

        for p in parts:
            requests.get(
                f"https://api.day.app/{BARK_KEY}/{p}",
                timeout=10
            )
            time.sleep(0.5)

    except Exception as e:
        logging.warning(f"Bark失败: {e}")

def acquire_lock():
    if os.path.exists(LOCK_FILE):

        # 超过1小时自动清理
        if time.time() - os.path.getmtime(LOCK_FILE) > 3600:
            os.remove(LOCK_FILE)
        else:
            return False

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    return True

def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

def validate_data(df):

    if df is None:
        return False

    if len(df) < 30:
        return False

    try:
        df.index = pd.to_datetime(df.index)
    except:
        return False

    # 最多允许5天没更新（周末/长假）
    if (datetime.now().date() - df.index[-1].date()).days > 5:
        return False

    if 'close' not in df.columns:
        return False

    if df['close'].isna().any():
        return False

    if df['close'].iloc[-1] <= 0:
        return False

    return True

# ======================== ETF数据 ========================

def get_etf(code, bs=None):

    # ---------- AKShare ----------
    try:

        df = ak.fund_etf_hist_em(symbol=code)

        if df is not None and not df.empty:

            df.rename(columns={
                '日期': 'date',
                '收盘': 'close',
                '成交额': 'amount',
                '成交金额': 'amount'
            }, inplace=True)

            df['date'] = pd.to_datetime(df['date'])

            df = df.set_index('date').sort_index()

            # 流动性过滤
            if 'amount' in df.columns:

                amt = pd.to_numeric(df['amount'], errors='coerce')

                if amt.tail(20).mean() < CONFIG["MIN_AMOUNT"]:
                    return None

            df['close'] = pd.to_numeric(df['close'], errors='coerce')

            if validate_data(df):
                return df[['close']]

    except Exception as e:
        logging.warning(f"AK ETF失败 {code}: {e}")

    # ---------- Baostock ----------
    if bs:

        try:

            bs_code = (
                f"sh.{code}"
                if code.startswith("51") or code.startswith("58")
                else f"sz.{code}"
            )

            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,close,volume",
                start_date="2015-01-01",
                end_date=datetime.now().strftime("%Y-%m-%d")
            )

            df = rs.get_data()

            if not df.empty:

                df['date'] = pd.to_datetime(df['date'])

                df['close'] = pd.to_numeric(
                    df['close'],
                    errors='coerce'
                )

                df['volume'] = pd.to_numeric(
                    df['volume'],
                    errors='coerce'
                )

                df = df.dropna().set_index('date')

                df['amount'] = df['close'] * df['volume']

                if df['amount'].tail(20).mean() < CONFIG["MIN_AMOUNT"]:
                    return None

                if validate_data(df):
                    return df[['close']]

        except Exception as e:
            logging.warning(f"BS ETF失败 {code}: {e}")

    return None

# ======================== 指数（三源） ========================

def get_index(bs=None):

    # ---------- 第一优先：510300 ----------
    etf = get_etf("510300", bs)

    if etf is not None:
        return etf, "ETF(510300)"

    # ---------- 第二优先：AKShare指数 ----------
    try:

        df = ak.stock_zh_index_daily_em(
            symbol=CONFIG["INDEX_CODE"]
        )

        if df is not None and not df.empty:

            df.rename(columns={
                "日期": "date",
                "收盘": "close"
            }, inplace=True)

            df['date'] = pd.to_datetime(df['date'])

            df = df.set_index('date')

            df['close'] = pd.to_numeric(
                df['close'],
                errors='coerce'
            )

            df = df[['close']]

            if validate_data(df):
                return df, "AKSHARE"

    except Exception as e:
        logging.warning(f"AK指数失败: {e}")

    # ---------- 第三优先：Baostock ----------
    if bs:

        try:

            rs = bs.query_history_k_data_plus(
                f"sh.{CONFIG['INDEX_CODE']}",
                "date,close",
                start_date="2015-01-01",
                end_date=datetime.now().strftime("%Y-%m-%d")
            )

            df = rs.get_data()

            if not df.empty:

                df['date'] = pd.to_datetime(df['date'])

                df['close'] = pd.to_numeric(
                    df['close'],
                    errors='coerce'
                )

                df = df.dropna().set_index('date')

                if validate_data(df):
                    return df[['close']], "BAOSTOCK"

        except Exception as e:
            logging.warning(f"BS指数失败: {e}")

    push("❌ 指数三源全部失败")

    return None, "FAIL"

# ======================== 动量 ========================

def momentum(df):

    if len(df) < CONFIG["MOM"]:
        return None

    try:

        ret = (
            df['close'].iloc[-1]
            / df['close'].iloc[-CONFIG["MOM"]]
            - 1
        )

        vol = (
            df['close']
            .pct_change()
            .tail(CONFIG["VOL"])
            .std()
        )

        return ret / max(vol, 1e-6)

    except:
        return None

# ======================== 大盘风控 ========================

def market_coef(series):

    if len(series) < CONFIG["MA"]:
        return 0.5

    ma = (
        series
        .rolling(CONFIG["MA"])
        .mean()
        .iloc[-1]
    )

    dev = (series.iloc[-1] - ma) / ma

    if dev < -0.05:
        return 0

    if dev < -0.03:
        return 0.3

    if dev < -0.01:
        return 0.6

    return 1

# ======================== 主程序 ========================

def main():

    if not acquire_lock():
        logging.info("已有实例运行")
        return

    bs = None

    try:
        import baostock as bs
        bs.login()
    except Exception as e:
        logging.warning(f"Baostock登录失败: {e}")
        bs = None

    try:

        # ---------- 获取指数 ----------
        idx, source = get_index(bs)

        if idx is None:
            return

        # ---------- 市场系数 ----------
        mcoef = market_coef(idx['close'])

        # ---------- 获取ETF ----------
        etfs = {}

        for code, name in ETF_POOL.items():

            df = get_etf(code, bs)

            if df is not None:
                etfs[code] = {
                    "name": name,
                    "df": df
                }

        total = len(ETF_POOL)
        valid = len(etfs)

        # ---------- 数据冻结 ----------
        if valid < total * 0.5:

            push(
                f"⚠️ 数据异常\n"
                f"有效ETF: {valid}/{total}"
            )

            return

        # ---------- 动量排名 ----------
        scores = []

        for code, info in etfs.items():

            mom = momentum(info['df'])

            if mom is not None:
                scores.append((code, mom))

        scores.sort(
            key=lambda x: x[1],
            reverse=True
        )

        selected = scores[:CONFIG["TOP_N"]]

        # ---------- 目标仓位 ----------
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
                    "score": round(score, 2)
                }

        # ---------- 推送 ----------
        msg = ""

        msg += f"📊 指数来源: {source}\n"

        msg += (
            f"扫描ETF: {valid}/{total}\n"
        )

        msg += (
            f"市场系数: {mcoef:.2f}\n\n"
        )

        if not targets:

            msg += "建议空仓"

        else:

            msg += "目标仓位:\n"

            total_weight = 0

            for code, info in targets.items():

                w = info['weight']

                total_weight += w

                msg += (
                    f"{info['name']} "
                    f"{w:.0%}\n"
                )

            cash = 1 - total_weight

            if cash > 0:
                msg += f"现金 {cash:.0%}"

        push(msg)

        logging.info(msg)

    except Exception as e:

        logging.exception("主程序异常")

        push(f"❌ 异常: {str(e)[:120]}")

    finally:

        if bs:
            try:
                bs.logout()
            except:
                pass

        release_lock()

# ======================== 启动 ========================

if __name__ == "__main__":
    main()
