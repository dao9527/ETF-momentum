#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime
import akshare as ak

CONFIG = {
    "INDEX_CODE": "000300",
    "MA": 200,
    "TOP_N": 2,
    "MOM": 60,
    "VOL": 20,
    "MAX_SINGLE": 0.4,
    "MIN_AMOUNT": 5e7,
}

ETF_POOL = {
    "159875": "新能源",
    "512480": "半导体",
    "515980": "人工智能",
    "159928": "消费",
    "512170": "医药",
    "512000": "券商",
}

STATE_FILE = "data/state.json"
LOCK_FILE = "data/lock"

os.makedirs("data", exist_ok=True)

logging.basicConfig(
    filename="data/run.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ========= 工具 =========
def push(msg):
    key = os.getenv("BARK_KEY")
    if not key:
        return
    try:
        requests.get(f"https://api.day.app/{key}/{msg[:200]}", timeout=5)
    except:
        pass

def acquire_lock():
    if os.path.exists(LOCK_FILE):
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
    if df is None or len(df) < 30:
        return False
    df.index = pd.to_datetime(df.index)
    if (datetime.now().date() - df.index[-1].date()).days > 5:
        return False
    if df['close'].iloc[-1] <= 0 or df['close'].isna().any():
        return False
    return True

# ========= ETF（双源） =========
def get_etf(code, bs=None):
    # ---- akshare ----
    try:
        df = ak.fund_etf_hist_em(symbol=code)
        df.rename(columns={
            '日期': 'date',
            '收盘': 'close',
            '成交额': 'amount',
            '成交金额': 'amount'
        }, inplace=True)

        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()

        if 'amount' in df.columns:
            if df['amount'].tail(20).mean() < CONFIG["MIN_AMOUNT"]:
                return None

        if validate_data(df):
            return df[['close']]
    except:
        pass

    # ---- baostock fallback ----
    if bs:
        try:
            bs_code = f"sh.{code}" if code.startswith('51') else f"sz.{code}"
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,close,volume",
                start_date='2015-01-01',
                end_date=datetime.now().strftime('%Y-%m-%d')
            )
            df = rs.get_data()
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
            df = df.dropna().set_index('date')

            df['amount'] = df['close'] * df['volume']

            if df['amount'].tail(20).mean() < CONFIG["MIN_AMOUNT"]:
                return None

            if validate_data(df):
                return df[['close']]
        except:
            pass

    return None

# ========= 指数（三源） =========
def get_index(bs=None):
    # ---- ETF指数 ----
    etf = get_etf("510300", bs)
    if etf is not None:
        return etf, "ETF(510300)"

    # ---- ak ----
    try:
        df = ak.stock_zh_index_daily_em(symbol=CONFIG["INDEX_CODE"])
        df.rename(columns={"日期": "date", "收盘": "close"}, inplace=True)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')[['close']]
        if validate_data(df):
            return df, "AKSHARE"
    except:
        pass

    # ---- baostock ----
    if bs:
        try:
            rs = bs.query_history_k_data_plus(
                f"sh.{CONFIG['INDEX_CODE']}",
                "date,close",
                start_date='2015-01-01',
                end_date=datetime.now().strftime('%Y-%m-%d')
            )
            df = rs.get_data()
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna().set_index('date')
            if validate_data(df):
                return df, "BAOSTOCK"
        except:
            pass

    push("❌ 指数三源全部失败")
    return None, "FAIL"

# ========= 策略 =========
def momentum(df):
    if len(df) < CONFIG["MOM"]:
        return None
    ret = df['close'].iloc[-1] / df['close'].iloc[-CONFIG["MOM"]] - 1
    vol = df['close'].pct_change().tail(CONFIG["VOL"]).std()
    return ret / max(vol, 1e-6)

def market_coef(series):
    if len(series) < CONFIG["MA"]:
        return 0.5
    ma = series.rolling(CONFIG["MA"]).mean().iloc[-1]
    dev = (series.iloc[-1] - ma) / ma
    if dev < -0.05: return 0
    if dev < -0.03: return 0.3
    if dev < -0.01: return 0.6
    return 1

# ========= 主程序 =========
def main():
    if not acquire_lock():
        return

    bs = None
    try:
        import baostock as bs
        bs.login()
    except:
        bs = None

    try:
        idx, source = get_index(bs)
        if idx is None:
            return

        total = len(ETF_POOL)
        etfs = {}

        for code, name in ETF_POOL.items():
            df = get_etf(code, bs)
            if df is not None:
                etfs[name] = df

        valid = len(etfs)

        msg = f"指数来源:{source}\n扫描ETF:{total}/有效:{valid}\n"

        # 数据保护
        if valid < total * 0.3:
            push(msg + "⚠️ 数据异常（已跳过）")
            return

        scores = [(n, momentum(df)) for n, df in etfs.items()]
        scores = [x for x in scores if x[1] is not None]
        scores.sort(key=lambda x: x[1], reverse=True)

        selected = [n for n, _ in scores[:CONFIG["TOP_N"]]]

        target = {}
        if selected:
            mcoef = market_coef(idx['close'])
            w = mcoef / len(selected)
            for n in selected:
                target[n] = min(w, CONFIG["MAX_SINGLE"])

        msg += "空仓" if not target else "持仓:" + ",".join([f"{k}({v:.0%})" for k, v in target.items()])

        push(msg)
        logging.info(msg)

    finally:
        if bs:
            try:
                bs.logout()
            except:
                pass
        release_lock()

if __name__ == "__main__":
    main()
