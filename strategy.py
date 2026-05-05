#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
import akshare as ak

CONFIG = {
    "INDEX_CODE": "000300",
    "MA": 200,
    "TOP_N": 2,
    "MOM": 60,
    "VOL": 20,
    "MAX_SINGLE": 0.4,
    "STOP_LOSS": -0.10,
    "MAX_DRAWDOWN": -0.15,
    "DAILY_STOP": -0.05,
    "MIN_HOLD": 10,
    "SLIPPAGE": 0.001,
    "COMMISSION": 0.0005,
    "MIN_AMOUNT": 5e7,
    "INDEX_STOP": 0.08,
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
SIGNAL_DIR = "signals"

os.makedirs("data", exist_ok=True)
os.makedirs(SIGNAL_DIR, exist_ok=True)

logging.basicConfig(
    filename="data/run.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def push(msg):
    key = os.getenv("BARK_KEY")
    if not key:
        return
    try:
        requests.get(f"https://api.day.app/{key}/{msg[:200]}", timeout=5)
    except:
        pass

def safe_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

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

# ================= ETF =================
def get_etf(code):
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
        return None

# ================= 指数（三重数据源） =================
def get_index(bs=None):
    # 🥇 第一层：ETF（最稳）
    etf_df = get_etf("510300")
    if etf_df is not None:
        return etf_df, "ETF(510300)"

    # 🥈 第二层：akshare
    try:
        df = ak.stock_zh_index_daily_em(symbol=CONFIG["INDEX_CODE"])
        df.rename(columns={"日期": "date", "收盘": "close"}, inplace=True)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')[['close']]
        if validate_data(df):
            return df, "AKSHARE"
    except Exception as e:
        logging.warning(f"ak失败: {e}")

    # 🥉 第三层：baostock
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
        except Exception as e:
            logging.warning(f"bs失败: {e}")

    push("❌ 三数据源全部失败")
    return None, "FAIL"

# ================= 策略 =================
def momentum(df):
    if len(df) < 60:
        return None
    ret = df['close'].iloc[-1] / df['close'].iloc[-60] - 1
    vol = df['close'].pct_change().tail(20).std()
    return ret / max(vol, 1e-6)

def market_coef(series):
    if len(series) < 200:
        return 0.5
    ma = series.rolling(200).mean().iloc[-1]
    dev = (series.iloc[-1] - ma) / ma
    if dev < -0.05: return 0
    if dev < -0.03: return 0.3
    if dev < -0.01: return 0.6
    return 1

# ================= 状态 =================
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except:
        pass
    return {"nav": 1.0, "peak": 1.0, "pos": {}}

def save_state(s):
    safe_write_json(STATE_FILE, s)

# ================= 主程序 =================
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
            df = get_etf(code)
            if df is not None:
                etfs[name] = df

        valid = len(etfs)

        # ===== 推送 =====
        msg = f"指数来源:{source}\n扫描ETF:{total}/有效:{valid}\n"

        if valid < total * 0.5:
            push(msg + "⚠️ 数据异常")
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
