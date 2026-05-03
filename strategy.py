#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V6.2.2 最终挂机版（防周末/长假崩溃 + 工程稳定补丁）
- 双数据源，baostock 全局只登录一次
- 指数获取失败不崩溃，推送并返回
- validate_data 允许数据滞后最多 5 天
- 数据冻结 & 指数异常保护
"""
import os
import json
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import akshare as ak

# ================= 配置 =================
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
    "INDEX_STOP": 0.08,          # 指数异常波动阈值
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
logger = logging.getLogger()

# ================= 工具 =================
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
    # 允许周末 + 长假（最多 5 天）
    if (datetime.now().date() - df.index[-1].date()).days > 5:
        return False
    if df['close'].iloc[-1] <= 0 or df['close'].isna().any():
        return False
    return True

# ================= 数据（传入全局 bs） =================
def get_index(bs=None):
    # 优先 akshare
    try:
        df = ak.stock_zh_index_daily_em(symbol=CONFIG["INDEX_CODE"])
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')[['close']]
        if validate_data(df):
            return df
    except Exception as e:
        logger.warning(f"akshare 指数失败: {e}")
    # 备用 baostock
    if bs is not None:
        try:
            rs = bs.query_history_k_data_plus(
                f"sh.{CONFIG['INDEX_CODE']}",
                "date,close",
                start_date='2015-01-01',
                end_date=datetime.now().strftime('%Y-%m-%d')
            )
            df = rs.get_data()
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')[['close']].astype(float)
                if validate_data(df):
                    return df
        except Exception as e:
            logger.warning(f"baostock 指数失败: {e}")
    # 两个数据源都失败
    push("❌ 指数数据获取失败，跳过今日")
    return None

def get_etf(code, bs=None):
    # 优先 akshare
    try:
        df = ak.fund_etf_hist_em(symbol=code)
        if df is not None and not df.empty:
            df['date'] = pd.to_datetime(df['日期'])
            df = df.set_index('date').sort_index()
            df.rename(columns={'收盘': 'close', '成交额': 'amount'}, inplace=True)
            if 'amount' in df.columns:
                if df['amount'].tail(20).mean() < CONFIG["MIN_AMOUNT"]:
                    return None
            if validate_data(df):
                return df[['close']]
    except:
        pass
    # 备用 baostock
    if bs is not None:
        try:
            bs_code = f"sh.{code}" if code.startswith('51') else f"sz.{code}"
            rs = bs.query_history_k_data_plus(
                bs_code, "date,close,volume",
                start_date='2015-01-01',
                end_date=datetime.now().strftime('%Y-%m-%d'),
                frequency="d", adjustflag="2"
            )
            df = rs.get_data()
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
                df['close'] = df['close'].astype(float)
                df['volume'] = df['volume'].astype(float)
                df['amount'] = df['volume'] * df['close']   # 元
                if df['amount'].tail(20).mean() < CONFIG["MIN_AMOUNT"]:
                    return None
                if validate_data(df):
                    return df[['close']]
        except:
            pass
    return None

# ================= 策略 =================
def market_coef(series):
    if len(series) < CONFIG["MA"]:
        return 0.5
    ma = series.rolling(CONFIG["MA"]).mean().iloc[-1]
    dev = (series.iloc[-1] - ma) / ma
    if dev < -0.05: return 0
    if dev < -0.03: return 0.3
    if dev < -0.01: return 0.6
    return 1

def momentum(df):
    if len(df) < max(CONFIG["MOM"], CONFIG["VOL"]):
        return None
    ret = df['close'].iloc[-1] / df['close'].iloc[-CONFIG["MOM"]] - 1
    vol = df['close'].pct_change().tail(CONFIG["VOL"]).std()
    return ret / max(vol, 1e-6)

# ================= 状态 =================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"nav": 1.0, "peak": 1.0, "pos": {}, "pause_until": None}

def save_state(s):
    safe_write_json(STATE_FILE, s)

# ================= 主逻辑 =================
def main():
    if not acquire_lock():
        logger.warning("已有实例运行，跳过")
        return

    # 全局登录 baostock（一次）
    bs = None
    try:
        import baostock as bs
        bs.login()
    except:
        bs = None

    try:
        idx = get_index(bs=bs)
        if idx is None:
            return   # 已经推送过错误消息

        etfs = {name: get_etf(code, bs=bs) for code, name in ETF_POOL.items()}
        etfs = {k: v for k, v in etfs.items() if v is not None}

        today = datetime.now().date()

        # 数据冻结保护
        if len(etfs) < len(ETF_POOL) * 0.7:
            msg = "⚠️ 数据异常，今日不调仓"
            push(msg)
            safe_write_json(f"{SIGNAL_DIR}/{today}.json", {
                "date": today.isoformat(),
                "status": "data_freeze",
                "etf_count": len(etfs)
            })
            return

        # 指数异常保护
        if idx['close'].pct_change().abs().iloc[-1] > CONFIG["INDEX_STOP"]:
            push(f"⚠️ 指数异常波动 > {CONFIG['INDEX_STOP']:.0%}，暂停交易")
            return

        state = load_state()

        if state["pause_until"]:
            if datetime.now() < datetime.fromisoformat(state["pause_until"]):
                return
            state["pause_until"] = None

        scores = [(n, momentum(df)) for n, df in etfs.items()]
        scores = [x for x in scores if x[1] is not None]
        scores.sort(key=lambda x: x[1], reverse=True)
        selected = [n for n, _ in scores[:CONFIG["TOP_N"]]]

        force_sell, must_keep = [], []
        for n, info in state["pos"].items():
            if n not in etfs:
                continue
            price = etfs[n]['close'].iloc[-1]
            buy = info.get("price", price)
            if price / buy - 1 < CONFIG["STOP_LOSS"]:
                force_sell.append(n)
                continue
            if len(etfs[n]) >= 2:
                if etfs[n]['close'].iloc[-1] / etfs[n]['close'].iloc[-2] - 1 < CONFIG["DAILY_STOP"]:
                    force_sell.append(n)
                    continue
            ed = info.get("entry_date")
            if ed:
                if (today - datetime.strptime(ed, "%Y-%m-%d").date()).days < CONFIG["MIN_HOLD"]:
                    must_keep.append(n)

        final_set = list(set(must_keep + [n for n in selected if n not in force_sell]))

        mcoef = market_coef(idx['close'])
        target = {}
        if final_set and mcoef > 0:
            base = mcoef / len(final_set)
            for n in final_set:
                target[n] = min(base, CONFIG["MAX_SINGLE"])
            s = sum(target.values())
            for n in target:
                target[n] = target[n] / s * mcoef

        old_w = {k: v["weight"] for k, v in state["pos"].items()}
        nav_change = 0
        for n, w in old_w.items():
            if n in etfs and len(etfs[n]) >= 2:
                r = etfs[n]['close'].iloc[-1] / etfs[n]['close'].iloc[-2] - 1
                nav_change += w * r
        turnover = sum(abs(old_w.get(k, 0) - target.get(k, 0))
                       for k in set(old_w) | set(target)) / 2
        cost = turnover * (CONFIG["SLIPPAGE"] + CONFIG["COMMISSION"])
        new_nav = state["nav"] * (1 + nav_change - cost)
        state["nav"] = new_nav
        state["peak"] = max(state["peak"], new_nav)
        dd = new_nav / state["peak"] - 1

        if dd < CONFIG["MAX_DRAWDOWN"]:
            push(f"⚠️ 熔断 {dd:.2%} 清仓")
            force_sell = list(old_w.keys())
            target = {}
            state["pause_until"] = (datetime.now() + timedelta(days=5)).isoformat()

        new_pos = {}
        for n, w in target.items():
            new_pos[n] = {
                "weight": w,
                "price": etfs[n]['close'].iloc[-1],
                "entry_date": state["pos"].get(n, {}).get("entry_date", today.isoformat())
            }
        state["pos"] = new_pos
        save_state(state)

        signal = {
            "date": today.isoformat(),
            "target": target,
            "force_sell": force_sell,
            "nav": new_nav,
            "drawdown": dd
        }
        safe_write_json(f"{SIGNAL_DIR}/{today}.json", signal)

        msg = f"净值:{new_nav:.3f} 回撤:{dd:.2%}\n"
        msg += "空仓" if not target else "持仓:" + ",".join([f"{k}({v:.0%})" for k, v in target.items()])
        if force_sell:
            msg += f"\n卖出:{','.join(force_sell)}"
        push(msg)
        logger.info(msg)

    finally:
        if bs is not None:
            try:
                bs.logout()
            except:
                pass
        release_lock()

if __name__ == "__main__":
    main()
