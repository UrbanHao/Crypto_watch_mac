#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance Futures — 高勝率低風險策略（Rich 面板 + LIVE/SIM 雙軌）
- 面板 Markets 顯示：Pos1/ROI1=真實、Pos2/ROI2=模擬
- 下單(LIVE)：市價進場 + 自動掛止損/停利（closePosition=true）
- 槓桿：每次下單前強制逐倉 + 指定槓桿（避免沿用手動 90x）
- ROI：以 aggTrade 即時價估算，含槓桿並扣雙邊 taker 費
- 掃描池：單幣 / 固定清單 / 自動隨機（可開關）
"""

import os, hmac, hashlib, json, time, math, threading, ssl, random, uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import deque

import requests
try:
    import websocket  # pip install websocket-client
except Exception:
    websocket = None

from decimal import Decimal, ROUND_DOWN

# --- PATCH: 將最上面的極簡 logger 改名，避免覆蓋 ---
def add_log_min(msg, level="info"):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[{ts}][{level.upper()}] {msg}")
    except Exception:
        try:
            print(str(msg))
        except Exception:
            pass
            
# === 一致化：集中記錄開倉被拒原因 ===
def log_open_reject(symbol: str, which: str, reason: str, **kv):
    """
    統一把「沒開倉成功」的原因寫入 Logs 面板。
    reason：簡短代碼（如 ml_threshold / gate_block / max_reached / blocked ...）
    kv：可選的詳細欄位（p、th、n_seen、pos、neg、why 等）
    """
    try:
        detail = ", ".join(f"{k}={v}" for k, v in kv.items())
        add_log(f"OPEN REJECT {which} {symbol} | {reason}"
                + (f" | {detail}" if detail else ""), "yellow")
    except Exception:
        pass
        
# ========== 交易規則快取 ==========
_SYMBOL_RULES_CACHE = {}  # { "ETHUSDT": {"stepSize":0.001,"minQty":0.001,"tickSize":0.01, "minNotional":10.0 or None}, ... }
# —— ML 訓練去重（每個部位只 close 訓練一次）——
_TRAINED_POS_UIDS = set()
_TRAINED_LOCK = threading.Lock()
# 交易入帳去重（避免同一筆平倉重複累計 total_pnl / balance）
_BOOKED_PNL_UIDS = set()
_BOOKED_LOCK = threading.Lock()

def _to_decimal(x):
    return Decimal(str(x))

def _floor_to_step(value: float, step: float) -> float:
    """向下對齊到允許步長（適用 qty/price）。"""
    v = _to_decimal(value)
    s = _to_decimal(step)
    # 量化到指定精度；ROUND_DOWN = 向下
    q = (v / s).quantize(Decimal('1'), rounding=ROUND_DOWN) * s
    return float(q)

def _get_symbol_rules(symbol: str):
    """從 exchangeInfo 擷取指定 symbol 的 LOT_SIZE / PRICE_FILTER / NOTIONAL 規則並快取。"""
    sym = symbol.upper()
    if sym in _SYMBOL_RULES_CACHE:
        return _SYMBOL_RULES_CACHE[sym]

    info = binance_get("/fapi/v1/exchangeInfo")
    # 找到該 symbol 的 filters
    target = None
    for s in info.get("symbols", []):
        if s.get("symbol") == sym:
            target = s
            break
    if not target:
        raise RuntimeError(f"symbol {sym} not found in exchangeInfo")

    stepSize = None
    minQty = None
    tickSize = None
    minNotional = None  # 期貨有時是 NOTIONAL 濾器

    for f in target.get("filters", []):
        ftype = f.get("filterType")
        if ftype == "LOT_SIZE":
            stepSize = float(f.get("stepSize", "0"))
            minQty   = float(f.get("minQty",   "0"))
        elif ftype == "PRICE_FILTER":
            tickSize = float(f.get("tickSize", "0"))
        elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
            mn1 = f.get("minNotional")
            mn2 = f.get("notional")
            vals = [float(x) for x in (mn1, mn2) if x is not None]
            if vals:
                minNotional = max(vals)

    if not stepSize or not minQty or not tickSize:
        raise RuntimeError(f"symbol {sym} missing filters: stepSize/minQty/tickSize")

    _SYMBOL_RULES_CACHE[sym] = {
        "stepSize": stepSize,
        "minQty": minQty,
        "tickSize": tickSize,
        "minNotional": minNotional
    }
    return _SYMBOL_RULES_CACHE[sym]

def _round_price_to_tick(price: float, tick: float, direction: int) -> float:
    v = _to_decimal(price); t = _to_decimal(tick)
    steps = (v / t).quantize(Decimal('1'), rounding=ROUND_DOWN)
    if direction > 0 and v != steps * t:
        steps += 1
    return float(steps * t)

def _fmt_to_tick(x: float, tick: float) -> str:
    d = Decimal(str(tick)).normalize()
    places = abs(d.as_tuple().exponent)
    return f"{x:.{places}f}"
    
# ==== Rich ====
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.align import Align
from rich import box
from rich.text import Text

# ===================== 基本設定 =====================
TZ = ZoneInfo("Asia/Taipei")
    
console = Console()
# ===== 全域補償（新增） =====
SERVER_TIME_OFFSET_MS = 0
# === Trades CSV 絕對路徑與寫檔鎖（更穩健版） ===
TRADES_CSV_PATH = os.path.abspath(os.path.expanduser(os.getenv("TRADES_CSV_PATH", "trades.csv")))
_CSV_LOCK = threading.Lock()
LOG_TRADES_ON = os.getenv("LOG_TRADES_CSV", "1").strip().lower() in ("1", "true", "yes", "y")

# 啟動時印出設定，方便排錯
try:
    console.print(f"[dim]TRADES_CSV_PATH = {TRADES_CSV_PATH} | LOG_TRADES_CSV={os.getenv('LOG_TRADES_CSV','1')}[/dim]")
except Exception:
    pass

def _sync_server_time_offset():
    global SERVER_TIME_OFFSET_MS
    try:
        srv = int(binance_get("/fapi/v1/time").get("serverTime", 0))
        local = utc_ms()
        SERVER_TIME_OFFSET_MS = srv - local
    except Exception:
        SERVER_TIME_OFFSET_MS = 0

# ======== DYNAMIC EXIT 調整參數（新增）========
DYN_EXIT_ON = True               # 總開關
DYN_ATR_LEN = 14                 # ATR 期數
DYN_MIN_SEC_BETWEEN_ADJ = 20     # 同一筆倉位兩次調整的最少間隔秒數
DYN_MIN_TICK_CHANGE_SL = 2       # SL 至少收緊 2 個 tick 才下單更換
DYN_MIN_TICK_CHANGE_TP = 4       # TP 變動最少 4 tick 才下單更換（避免頻繁改單）
DYN_ONLY_TIGHTEN_SL = False       # 僅允許「收緊」SL（不放鬆）

# regime→倍數對照（你可微調）
# RANGE：保守（窄距離），UP/DOWN 趨勢：拉大 TP、適度放寬 SL 目標（但若 ONLY_TIGHTEN_SL=True，實作仍不會放鬆）
DYN_MULT = {
    "RANGE": {"k_sl": 1.8, "k_tp": 2.6},
    "UP":    {"k_sl": 2.5, "k_tp": 4.5},
    "DOWN":  {"k_sl": 2.5, "k_tp": 4.5},
}

# 最小百分比距離（避免 ATR 極小）
DYN_MIN_SL_PCT = 0.0020   # 0.20%
DYN_MIN_TP_PCT = 0.0040   # 0.40%

# 內部節流：每筆倉位最後一次成功調整的時間戳（ms）
_LAST_DYN_ADJ_MS = {}   # key=(which, symbol) -> ms
# --- Debounce / Throttle flags ---
_SIM_STATE_DONE = False
_LAST_AI_FALLBACK_TS = 0.0
_LAST_POOL_FPRINT = ""   # 紀錄上次印出的 pool 簽章，避免重複

# ==== ML/AI 啟動門檻（新）====
ML_TRAIN_AFTER_SEEN   = int(os.getenv("ML_TRAIN_AFTER_SEEN", "80"))  # 訓練至少看到 200 筆關閉樣本再開始
ML_FILTER_AFTER_SEEN  = int(os.getenv("ML_FILTER_AFTER_SEEN","80"))  # 進場過濾至少 200 筆後才啟用
AI_ENABLE         = bool(int(os.getenv("AI_ENABLE","0")))         # 預設關閉 AI 決策
AI_MIN_SEEN_FOR_ACTION= int(os.getenv("AI_MIN_SEEN_FOR_ACTION","80"))# AI 也要資料夠才動
ML_THRESHOLD = float(os.getenv("ML_THRESHOLD", "0.55"))
ML_AUTO_ADJUST = bool(int(os.getenv("ML_AUTO_ADJUST", "1")))  # 需要時可關閉自動調整

# --- 併發與開倉中狀態 ---
ORDER_LOCK = threading.Lock()
OPEN_INFLIGHT = set()  # 記錄正在開倉的 symbol（尚未確認持倉）

# ======== 執行 / 檢視模式 ========
# SIM  = 只模擬
# LIVE = 只真實
# BOTH = 同步：真實下單 + 建一筆模擬單對照
EXECUTION_MODE = "BOTH"
# Markets/Perf 顯示主視角帳戶（"LIVE" 或 "SIM"）
SHOW_ACCOUNT = "LIVE"
# 只讓哪些帳戶的樣本參與訓練（與資料落地）
ML_TRAIN_SOURCES = {"LIVE"}   # 想同時訓練兩邊就設為 {"LIVE","SIM"}

# --- 方向反轉（全域） ---
# 1=全域反向下單；0=正常
INVERT_SIGNALS = bool(int(os.getenv("INVERT_SIGNALS", "0")))

# --- 交易/來源設定 ---
TESTNET           = False
USE_WEBSOCKET     = True
INTERVAL          = "1m"     # 測試用 1m；掃盤穩健 15m/1h
USE_SINGLE_MODE   = False    # True=單幣（用 SINGLE_SYMBOL）
USE_AUTO_REFRESH  = True    # True=每 30 分鐘隨機刷新幣池
SINGLE_SYMBOL     = "ASTERUSDT"
SCAN_SYMBOLS      = []

# --- 面板比例/更新頻率（可調） ---
HEADER_ROWS   = 3
FOOTER_ROWS   = 11
LEFT_RATIO    = 4
RIGHT_RATIO   = 2
REFRESH_FPS   = 8

# == 放在全域變數區 ==
TRADE_OFFSET = 0   # 從0開始，正數表示往「更舊」的交易看
LOGS_OFFSET  = 0
TRADE_PAGE   = 20  # 每頁顯示幾列
LOGS_PAGE    = 12

# === Net Backoff（LIVE 同步用）===
_NET_BACKOFF_UNTIL = 0.0
_NET_BACKOFF_STEP  = 60.0   # 初始 60 秒
_NET_BACKOFF_MAX   = 5 * 60 # 最長 5 分鐘

def _slice_with_offset(rows, page, offset):
    if not rows: return []
    start = max(0, len(rows) - page - offset)
    end   = max(0, len(rows) - offset)
    return rows[start:end]
    
def _key_listener():
    import sys, threading, time
    is_win = (sys.platform.startswith("win"))
    if is_win:
        import msvcrt
        getch = lambda: msvcrt.getch().decode(errors="ignore") if msvcrt.kbhit() else None
        restore = lambda: None
        setup = lambda: None
    else:
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        def setup():
            tty.setcbreak(fd)
        def restore():
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        def getch():
            r,_,_ = select.select([sys.stdin],[],[],0.05)
            return sys.stdin.read(1) if r else None

    global TRADE_OFFSET, LOGS_OFFSET
    try:
        setup()
        while True:
            ch = getch()
            if not ch:
                time.sleep(0.02); continue
            if ch in ("q", "\x03"):  # q 或 Ctrl-C 退出監聽（不終止主程式）
                break
            elif ch == "j":   # trades 向下看舊的
                TRADE_OFFSET = min(999999, TRADE_OFFSET + 1)
            elif ch == "k":   # trades 回到新的
                TRADE_OFFSET = max(0, TRADE_OFFSET - 1)
            elif ch == "J":   # trades 下一頁
                TRADE_OFFSET = min(999999, TRADE_OFFSET + TRADE_PAGE)
            elif ch == "K":   # trades 上一頁
                TRADE_OFFSET = max(0, TRADE_OFFSET - TRADE_PAGE)
            elif ch == "h":   # logs 上一列
                LOGS_OFFSET = max(0, LOGS_OFFSET - 1)
            elif ch == "l":   # logs 下一列
                LOGS_OFFSET = min(999999, LOGS_OFFSET + 1)
            # 可再加：g/G 跳到最上/最下
    finally:
        restore()

# ----- 倉位 sizing 模式 -----
POSITION_SIZING = "RISK"      # ← 統一用 RISK 模式
ALLOC_PCT = 15.0              # （保留參數但不再使用；避免其他區塊引用報錯）

TP_MARGIN_PCT = 0.12          # 僅作為 ATR 失效時的 fallback
SL_MARGIN_PCT = 0.07

RISK_PER_TRADE_PCT      = 2   # ← 單筆最大風險 0.7%（可微調 0.5~1.0）
LEVERAGE                 = 10
TAKER_FEE_PCT            = 0.04

USE_TRAILING             = True  # ← 開啟追蹤（SIM 端已實作保本→ATR 單邊收緊）
TRAIL_TRIGGER_PCT        = 0.02  # （保留：LIVE 若未交由交易所出場可用）
TRAIL_STEP_PCT           = 0.01

DAILY_TARGET_PCT         = 0.2  # ← 日停利 +3%
DAILY_MAX_LOSS_PCT       = 0.1  # ← 日停損 -2%
EXCHANGE_MANAGE_EXIT     = True  # LIVE 仍由交易所管理 TP/SL

MAX_CONCURRENT_POS       = 3
ONE_POS_PER_SYMBOL       = True

BLACKLIST                = set()
WHITELIST                = set()

# === ROI Fallback Exit (干擾 TP/SL 的硬控) ===
ROI_EXIT_ON          = True
ROI_EXIT_LOSS_PCT    = float(os.getenv("ROI_EXIT_LOSS_PCT", "-5.0"))   # ROI<= -5% 觸發
ROI_EXIT_PROFIT_PCT  = float(os.getenv("ROI_EXIT_PROFIT_PCT", "10.0"))  # ROI>=+10% 觸發
ROI_MIN_AGE_SEC      = int(os.getenv("ROI_MIN_AGE_SEC", "5"))          # 最短持倉秒數，避免剛進場就被抖掉
ROI_CONFIRM_SEC      = int(os.getenv("ROI_CONFIRM_SEC", "5"))           # 門檻需連續達成秒數(去抖)
# 每筆部位的 ROI 連續達標起始時間
_ROI_HIT_SINCE = {}


# --- 指標參數 ---
EMA_FAST = 50
EMA_SLOW = 200
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
RSI_LEN = 14
BB_LEN, BB_STD = 20, 2.0
MIN_BB_WIDTH = 0.003

VOL_CONFIRM  = True
VOL_MA       = 20
VOL_K        = 1.0

# ===================== API / 網址 =====================
if TESTNET:
    BASE_URL = "https://testnet.binancefuture.com"
    WS_URL   = "wss://stream.binancefuture.com"
else:
    BASE_URL = "https://fapi.binance.com"
    WS_URL   = "wss://fstream.binance.com"

WS_COMBINED_PATH = "/stream?streams="
API_KEY    = os.getenv("BINANCE_FUTURES_KEY", "")
API_SECRET = os.getenv("BINANCE_FUTURES_SECRET", "")

# ========= Grid Advisor 參數 =========
GRID_DEFAULT_COUNT = 10
TREND_RET_TH = 0.005
RANGE_MIN_BB_W = 0.008

# 讓面板可配置
GRID_SYMBOL = SINGLE_SYMBOL   # 也可換成你想觀察的 symbol
GRID_HOURS  = 1.0             # 觀察視窗（小時）
GRID_COUNT  = 10              # 網格數


def _pick_candles_for_window(st: "SymbolState", hours: float = 1.0):
    if not st.candles:
        return []
    if INTERVAL.endswith("m"):
        m = int(INTERVAL[:-1]); bar_sec = m * 60
    elif INTERVAL.endswith("h"):
        h = int(INTERVAL[:-1]); bar_sec = h * 3600
    else:
        bar_sec = 60
    need = max(1, int(hours * 3600 / bar_sec))
    return list(st.candles)[-need:]

def classify_trend_and_range(symbol: str, hours: float = 1.0):
    st = SYMAP[symbol]
    st.update_indicators()
    window = _pick_candles_for_window(st, hours)
    if len(window) < 2:
        return None
    closes = [c["close"] for c in window]
    hi = max(c["high"] for c in window)
    lo = min(c["low"]  for c in window)
    ret = (closes[-1] - closes[0]) / closes[0]
    ema_fast = st.ema_fast; ema_slow = st.ema_slow
    bb_w = st.bb_width() or 0.0
    if ret >= TREND_RET_TH and (ema_fast and ema_slow and ema_fast > ema_slow):
        trend = "UP"
    elif ret <= -TREND_RET_TH and (ema_fast and ema_slow and ema_fast < ema_slow):
        trend = "DOWN"
    else:
        trend = "RANGE" if (abs(ret) < TREND_RET_TH and bb_w < RANGE_MIN_BB_W) else ("UP" if ret >= 0 else "DOWN")
    return {"symbol": symbol, "hours": hours, "trend": trend,
            "upper": hi, "lower": lo, "ret": ret,
            "ema_fast": ema_fast, "ema_slow": ema_slow, "bb_width": bb_w}

def build_equally_spaced_grid(lower: float, upper: float, grid_count: int):
    if upper <= lower:
        raise ValueError("upper 必須大於 lower")
    grid_count = max(2, int(grid_count))
    step = (upper - lower) / (grid_count - 1)
    return [lower + i * step for i in range(grid_count)]

def suggest_grid(symbol: str, hours: float = 1.0, grid_count: int = GRID_DEFAULT_COUNT):
    info = classify_trend_and_range(symbol, hours)
    if not info:
        return None
    upper = info["upper"]; lower = info["lower"]
    prices = build_equally_spaced_grid(lower, upper, grid_count)
    rules = _get_symbol_rules(symbol); tick = rules["tickSize"]
    prices = [_round_price_to_tick(p, tick, +1) for p in prices]
    return {"symbol": symbol, "trend": info["trend"], "hours": info["hours"],
            "upper": max(prices), "lower": min(prices),
            "grid_count": len(prices), "grid_prices": prices,
            "ret": info["ret"], "ema_pair": (info["ema_fast"], info["ema_slow"]),
            "bb_width": info["bb_width"]}

def print_grid_suggestion(symbol: str, hours: float = 1.0, grid_count: int = GRID_DEFAULT_COUNT):
    s = suggest_grid(symbol, hours, grid_count)
    if not s:
        console.log(f"[red]Grid 建議失敗（資料不足）: {symbol}[/red]")
        return
    console.log(
        f"[bold]{symbol}[/bold] 近{s['hours']}h | 趨勢: [cyan]{s['trend']}[/cyan] | "
        f"區間: {s['lower']:.4f} ~ {s['upper']:.4f} | "
        f"EMA: {s['ema_pair'][0]:.2f}/{s['ema_pair'][1]:.2f} | "
        f"BB寬度: {s['bb_width']*100:.2f}% | Ret: {s['ret']*100:.2f}%"
    )

def _server_time_sync_worker(period=60):
    while True:
        try:
            _sync_server_time_offset()
        except Exception:
            pass
        time.sleep(period)
        
# ===== Runtime Logs（顯示在面板 + 寫入檔案）=====
LOGS = deque(maxlen=80)
_LOG_FILE_PATH = os.path.abspath("runtime_logs.txt")  # ← 可改檔名或完整路徑
_LOG_LOCK = threading.Lock()

def add_log(msg, style="white"):
    """
    同時寫入面板（Rich Logs）與檔案。
    """
    try:
        ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"{ts} [{style}] {msg}"
        LOGS.append((ts, style, str(msg)))

        # 寫入文字檔
        with _LOG_LOCK:
            with open(_LOG_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
    except Exception as e:
        # 任何例外只影響檔案，不讓面板中斷
        try:
            print(f"add_log error: {e}")
        except Exception:
            pass

def logs_panel(max_rows=12, offset=0):
    t = Table(box=box.MINIMAL, expand=True)
    t.add_column("Time", style="dim")
    t.add_column("Msg")

    rows_all = list(LOGS)
    start = max(0, len(rows_all) - max_rows - offset)
    end   = max(0, len(rows_all) - offset)
    rows  = rows_all[start:end]

    # 正確解包順序：ts, style, msg
    for ts, style, msg in rows:
        t.add_row(ts, f"[{style}]{msg}[/{style}]")

    return Panel(t, title=f"Logs (offset={offset})", border_style="red")


def _bar(p: float, width: int = 20) -> str:
    fill = int(round(p * width))
    return "█" * fill + "░" * (width - fill)

def market_confidence_panel(top_n=12):
    from rich.table import Table
    from rich.panel import Panel
    from rich import box

    rows = []
    for s in preferred_scan_order():
        st = SYMAP.get(s)
        if not st:
            continue
        if st.ml_p is None:
            continue
        rows.append((s, st.ml_p, st.ml_p_ts))
    rows.sort(key=lambda x: x[1], reverse=True)
    rows = rows[:top_n]

    t = Table(box=box.MINIMAL, expand=True)
    t.add_column("Sym", style="cyan", no_wrap=True)
    t.add_column("p", justify="right")
    t.add_column("Bar")

    th = ML.threshold
    for s, p, _ts in rows:
        col = "green" if p >= th else "red"
        t.add_row(s, f"[{col}]{p:.3f}[/{col}]", f"[{col}]{_bar(p)}[/{col}]")

    subtitle = f"th={th:.3f} • n_seen={ML.model.n_seen} • pos/neg={ML.pos_seen}/{ML.neg_seen}"
    return Panel(t, title="AI 市場信心 (p)", subtitle=subtitle, border_style="magenta")

def trades_panel(which: str = "LIVE"):
    """
    以全域 TRADE_PAGE/TRADE_OFFSET 切片顯示交易清單。
    which: "LIVE" 或 "SIM"
    """
    acct = ACCOUNT_LIVE if which.upper()=="LIVE" else ACCOUNT_SIM
    rows_all = acct.trades
    rows = _slice_with_offset(rows_all, TRADE_PAGE, TRADE_OFFSET)

    t = Table(box=box.MINIMAL, expand=True)
    t.add_column("Time", style="dim")
    t.add_column("Acct", style="cyan")
    t.add_column("Sym")
    t.add_column("Side")
    t.add_column("Entry", justify="right")
    t.add_column("Exit",  justify="right")
    t.add_column("PnL$", justify="right")
    t.add_column("Net%", justify="right")
    t.add_column("Reason", overflow="fold")

    for r in rows:
        t.add_row(
            str(r.get("ts","")),
            which.upper(),
            str(r.get("symbol","")),
            str(r.get("side","")),
            f'{float(r.get("entry",0)):g}',
            f'{float(r.get("exit",0)):g}',
            f'{float(r.get("pnl_cash",0)):+.2f}',
            f'{float(r.get("net_pct",0)):+.3f}',
            str(r.get("reason","")),
        )

    title = f"Trades • {which.upper()} (W/L={sum(1 for x in rows_all if x.get('pnl_cash',0)>0)}/{sum(1 for x in rows_all if x.get('pnl_cash',0)<=0)})"
    subtitle = f"(offset={TRADE_OFFSET}, page={TRADE_PAGE}, total={len(rows_all)})"
    return Panel(t, title=title, subtitle=subtitle, border_style="green")
    
# ====== 輕量快取，避免每次重繪都打 REST ======
_VOL_CACHE = {"ts": 0.0, "map": {}}
_FR_CACHE  = {"ts": 0.0, "map": {}}
_RANK_CACHE = {"ts": 0.0, "list": []}

VOL_TTL_SEC  = 600      # 24h量能每60秒更新一次就夠
FR_TTL_SEC   = 60      # 資金費率每60秒更新一次
RANK_TTL_SEC = 30      # 排名每30秒更新一次

def _get_24h_quote_volume_map_cached():
    now = time.time()
    if now - _VOL_CACHE["ts"] >= VOL_TTL_SEC or not _VOL_CACHE["map"]:
        _VOL_CACHE["map"] = _get_24h_quote_volume_map()
        _VOL_CACHE["ts"] = now
    return _VOL_CACHE["map"]

def _get_funding_rate_map_cached(symbols):
    now = time.time()
    # 這裡簡化成整包更新（依需求也可做逐檔）
    if now - _FR_CACHE["ts"] >= FR_TTL_SEC or not _FR_CACHE["map"]:
        _FR_CACHE["map"] = _get_funding_rate_map(symbols)
        _FR_CACHE["ts"] = now
    return _FR_CACHE["map"]

def rank_grid_candidates_cached(top_n=8, hours: float = None):
    now = time.time()
    if now - _RANK_CACHE["ts"] < RANK_TTL_SEC and _RANK_CACHE["list"]:
        return _RANK_CACHE["list"][:top_n]

    h = hours if hours is not None else GRID_HOURS
    vol_map = _get_24h_quote_volume_map_cached()
    fr_map  = _get_funding_rate_map_cached(SYMBOLS)

    vols = [vol_map.get(s, 0.0) for s in SYMBOLS]
    vmin, vmax = (min(vols), max(vols)) if vols else (0.0, 1.0)

    results = []
    for s in SYMBOLS:
        try:
            _score, d = compute_grid_fitness(s, h)
            if d.get("reason") == "no_info":
                continue

            v = vol_map.get(s, 0.0)
            liq = 0.0 if vmax <= vmin else (v - vmin) / (vmax - vmin)

            fr = abs(fr_map.get(s, 0.0))
            fr_pen = 0.0
            if fr > 0.0003:
                fr_pen = min(1.0, (fr - 0.0003) / 0.0005)

            score = (
                0.35 * d["bb_sweet"]
              + 0.25 * d["flat_combo"]
              + 0.25 * liq
              + 0.10 * d["tick_score"]
              - 0.10 * fr_pen
            ) * 100.0

            results.append({
                "symbol": s, "score": round(score, 2), "hours": h,
                "bb_width": d["bb_width"], "ret_abs": d["ret_abs"],
                "ema_gap": d["ema_gap"], "vwap_dev": d["vwap_dev"],
                "liq_norm": round(liq,3), "funding_abs": fr, "tick_rel": d["tick_rel"]
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    _RANK_CACHE["list"] = results
    _RANK_CACHE["ts"] = now
    return results[:top_n]
    
def _safe_div(a, b, default=0.0):
    try:
        return (a / b) if b else default
    except Exception:
        return default

def _get_24h_quote_volume_map():
    try:
        arr = binance_get("/fapi/v1/ticker/24hr")
        m = {}
        for t in arr:
            s = t.get("symbol")
            if s in SYMAP:
                m[s] = float(t.get("quoteVolume") or 0.0)
        return m
    except Exception:
        return {}

def _get_funding_rate_map(symbols):
    out = {}
    try:
        # premiumIndex 可查單一；批量就逐檔（簡化：失敗忽略）
        for s in symbols:
            try:
                j = binance_get("/fapi/v1/premiumIndex", f"symbol={s}")
                fr = float(j.get("lastFundingRate") or 0.0)
                out[s] = fr
            except Exception:
                out[s] = 0.0
    except Exception:
        pass
    return out

def compute_grid_fitness(symbol: str, hours: float = None):
    """回傳 (score, detail)；需要 st 指標已更新 & 有近窗資料。"""
    st = SYMAP[symbol]
    h = hours if hours is not None else GRID_HOURS
    info = classify_trend_and_range(symbol, h)
    if not info:
        return 0.0, {"reason":"no_info"}

    # 基礎特徵
    bw = max(0.0, float(info["bb_width"] or 0.0))          # 布林寬（相對）
    ret_abs = abs(float(info["ret"] or 0.0))               # 近窗報酬絕對值
    ema_gap = abs(_safe_div((st.ema_fast or 0) - (st.ema_slow or 0), (st.candles[-1]["close"] if st.candles else 1)))
    vwap_dev = abs(_safe_div((st.candles[-1]["close"] - (st.vwap or st.candles[-1]["close"])), st.candles[-1]["close"])) if st.candles else 0.0

    # 甜蜜區評分（中心在 1.2%，兩側線性衰減到 0）
    sweet_mid = 0.012
    sweet_half = 0.006   # 0.6% 偏離打到 0 分
    bb_sweet = max(0.0, 1.0 - (abs(bw - sweet_mid) / sweet_half))
    bb_sweet = min(bb_sweet, 1.0)

    # 盤整分：ret / ema_gap / vwap 偏離都小才高
    flat_ret = max(0.0, 1.0 - (ret_abs / 0.01))            # 1% 內幾乎滿分
    flat_ema = max(0.0, 1.0 - (ema_gap / 0.004))           # 0.4% 內滿分
    flat_vwap= max(0.0, 1.0 - (vwap_dev / 0.004))          # 0.4% 內滿分
    flat_combo = 0.4*flat_ret + 0.3*flat_ema + 0.3*flat_vwap

    # 交易成本：tick 粒度（越細越好）
    rules = _get_symbol_rules(symbol)
    last_px = st.candles[-1]["close"] if st.candles else (st.last_price or 0.0)
    tick_rel = _safe_div(float(rules["tickSize"]), last_px, 0.0)         # 相對 tick
    tick_pen = min(1.0, tick_rel / 0.001)                                # >0.1% 就很糟
    tick_score = 1.0 - tick_pen

    detail = {
        "bb_width": bw, "ret_abs": ret_abs, "ema_gap": ema_gap, "vwap_dev": vwap_dev,
        "tick_rel": tick_rel, "tick_score": tick_score, "bb_sweet": bb_sweet, "flat_combo": flat_combo
    }

    return None, detail  # 先回傳 detail，批量時一起加上量能/資金費率再算總分
# ===================== 工具函式 =====================

def utc_ms(): return int(time.time()*1000)
_LAST_CLOSE_TS = {}
_LAST_CLOSE_LOCK = threading.Lock()

def _dyn_target_tp_sl(st: "SymbolState", which: str) -> tuple[float, float] | None:
    """
    依 ATR + 5m regime 動態產生『目標』TP/SL（連續值），不直接落單。
    回傳 (tp, sl)，若資料不足回 None。
    """
    p = _get_pos(st, which)
    if not p or st.last_price is None:
        return None
    entry = float(p["entry"])
    side  = p["side"]

    # 1) 取得 ATR & regime
    candles = list(st.candles)
    atr_val = atr_wilder(candles, n=DYN_ATR_LEN)
    if not atr_val or atr_val <= 0:
        return None
    regime = regime_on_5m(st) if callable(globals().get("regime_on_5m")) else "RANGE"
    m = DYN_MULT.get(regime, DYN_MULT["RANGE"])
    k_sl = float(m["k_sl"]); k_tp = float(m["k_tp"])

    # 2) 用 ATR 倍數 + 最小百分比距離，生成連續值 TP/SL
    sl_dist = max(k_sl * atr_val, entry * DYN_MIN_SL_PCT)
    tp_dist = max(k_tp * atr_val, entry * DYN_MIN_TP_PCT)

    if side == "LONG":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist

    # 3) 對齊到 tick & 保持與 entry 的最小距離（沿用你現有工具）
    try:
        rules = _get_symbol_rules(st.symbol)
        tick  = float(rules["tickSize"])
    except Exception:
        tick = 0.0

    if tick > 0:
        if side == "LONG":
            tp = _round_price_to_tick(_apply_min_gap(entry, tp, tick, True),  tick, +1)
            sl = _round_price_to_tick(_apply_min_gap(entry, sl, tick, False), tick, -1)
        else:
            tp = _round_price_to_tick(_apply_min_gap(entry, tp, tick, False), tick, -1)
            sl = _round_price_to_tick(_apply_min_gap(entry, sl, tick, True),  tick, +1)

    return (tp, sl)


def _enforce_tighten_only(cur_sl: float, new_sl: float, side: str) -> float:
    """只允許 SL 收緊；若新 SL 反而寬鬆，就保留原值。"""
    if not DYN_ONLY_TIGHTEN_SL:
        return new_sl
    if side == "LONG":
        return max(cur_sl, new_sl)
    else:
        return min(cur_sl, new_sl)
        
def _roi_fallback_should_exit(st: "SymbolState", which: str) -> tuple[bool, float]:
    """達 ROI 門檻就 True；含最短持倉時間與連續達標去抖。"""
    if not ROI_EXIT_ON:
        return (False, 0.0)
    p = _get_pos(st, which)
    if not p:
        return (False, 0.0)

    # 最短持倉時間
    age_sec = max(0, int((utc_ms() - int(p.get("opened_ts") or utc_ms())) / 1000))
    if age_sec < ROI_MIN_AGE_SEC:
        return (False, 0.0)

    roi = current_roi_pct(st, which) or 0.0  # 已含槓桿/費用(%)
    hit = (roi <= ROI_EXIT_LOSS_PCT) or (roi >= ROI_EXIT_PROFIT_PCT)
    if not hit:
        _ROI_HIT_SINCE.pop((which.upper(), st.symbol), None)
        return (False, roi)

    key = (which.upper(), st.symbol)
    now = utc_ms()
    since = _ROI_HIT_SINCE.get(key)
    if since is None:
        _ROI_HIT_SINCE[key] = now
        return (False, roi)

    # 需連續達標 ROI_CONFIRM_SEC 才觸發
    if (now - since) >= ROI_CONFIRM_SEC * 1000:
        # 一次性：交給外層 close 後，清掉時間戳
        _ROI_HIT_SINCE.pop(key, None)
        return (True, roi)

    return (False, roi)
    
def _close_too_soon(which, symbol, min_ms=600):
    """避免同一 symbol / 帳戶在極短時間內重複 close（防止重複訓練與重複紀錄）"""
    now = utc_ms()
    W = (which or "").upper()
    S = (symbol or "").upper()
    key = (W, S)
    with _LAST_CLOSE_LOCK:
        last = _LAST_CLOSE_TS.get(key, 0)
        if now - last < min_ms:
            return True
        _LAST_CLOSE_TS[key] = now
    return False
    
# 新增全域快取：前一輪的持倉狀態
_LAST_LIVE_POS_STATE = {}

def housekeeping_worker():
    global _LAST_LIVE_POS_STATE
    while True:
        try:
            live_positions = get_all_positions("LIVE")  # 你現有的函式
            now_symbols = {p["symbol"]: p for p in live_positions if abs(float(p["positionAmt"])) > 0}
            prev_symbols = _LAST_LIVE_POS_STATE

            # 🟢 檢測「上次有、這次沒」的幣 = 被交易所 TP/SL 平掉
            closed_symbols = [s for s in prev_symbols if s not in now_symbols]
            for sym in closed_symbols:
                p = prev_symbols[sym]
                entry_price = float(p.get("entryPrice") or 0)
                qty = abs(float(p.get("positionAmt") or 0))
                side = "LONG" if float(p.get("positionAmt") or 0) > 0 else "SHORT"
                last_px = float(p.get("markPrice") or entry_price)

                # ROI 計算
                move = (last_px - entry_price) / entry_price * (1 if side == "LONG" else -1)
                ret_pct = move * LEVERAGE * 100.0 - (TAKER_FEE_PCT * 2)

                pnl_cash = qty * (last_px - entry_price) * (1 if side == "LONG" else -1)
                add_log(f"[Housekeep detect TP/SL] {sym} {side} ROI={ret_pct:.2f}%", "dim")

                # ✅ 統一走 close_position_one（涵蓋 CSV、清殘單、ML 訓練與去重）
                try:
                    close_position_one(sym, "Exchange TP/SL filled", last_px, "LIVE", skip_exchange=True)
                except Exception as e:
                    add_log(f"housekeeping close fail {sym}: {e}", "red")

            _LAST_LIVE_POS_STATE = now_symbols
        except Exception as e:
            add_log(f"housekeeping_worker error: {e}", "red")

        time.sleep(3)  # 3 秒檢查一次

def get_all_positions(which="LIVE"):
    """
    回傳 /fapi/v2/positionRisk 的原始陣列（幣安格式）。
    只在 LIVE + 有 API key 時才會打；否則回空陣列。
    """
    if which.upper() != "LIVE" or TESTNET or not API_KEY or not API_SECRET:
        return []
    try:
        return binance_signed("GET", "/fapi/v2/positionRisk", {})
    except Exception:
        return []
        
def sign_params(query: str) -> str:
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def binance_get(path, params=""):
    url = f"{BASE_URL}{path}"
    if params: url += f"?{params}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()
    
def live_sanity_market_buy(symbol="ETHUSDT"):
    if not API_KEY or not API_SECRET:
        console.print("[red]No API key[/red]"); return
    rules = _get_symbol_rules(symbol)
    px = float(binance_get("/fapi/v1/ticker/price", f"symbol={symbol}")["price"])
    qty = max(rules["minQty"], (rules.get("minNotional",10)/px)*1.05)
    qty = _floor_to_step(qty, rules["stepSize"])
    res = binance_signed("POST","/fapi/v1/order",{
        "symbol":symbol,"side":"BUY","type":"MARKET","quantity":qty,
        "recvWindow":10000,"newOrderRespType":"RESULT"
    })
    console.print(f"[green]BUY ok id={res.get('orderId')} qty={qty}[/green]")
    binance_signed("POST","/fapi/v1/order",{
        "symbol":symbol,"side":"SELL","type":"MARKET","quantity":qty,
        "reduceOnly":"true","recvWindow":10000
    })
    console.print("[green]SELL reduceOnly ok[/green]")
    
from urllib.parse import urlencode
# === PATCH: 取得真實手續費（USDT） ===
def fetch_commission_usdt(symbol: str, start_ms: int | None = None, end_ms: int | None = None, limit: int = 80) -> float:
    """
    從 /fapi/v1/userTrades 讀取成交，彙總 commission 為 USDT。
    - symbol: 例如 "ETHUSDT"
    - start_ms/end_ms: 建議用部位 opened_ts 到 close 時間（可各放寬幾秒）
    - limit: 讀取上限（1~1000），預設 80
    回傳：手續費總額（USDT；抓不到回 0.0）
    """
    try:
        if TESTNET or not API_KEY or not API_SECRET:
            return 0.0

        params = {"symbol": symbol, "limit": max(1, min(int(limit), 1000))}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)

        trades = binance_signed("GET", "/fapi/v1/userTrades", params) or []
        fee_usdt = 0.0
        for t in trades[-limit:]:
            c = float(t.get("commission") or 0.0)
            asset = str(t.get("commissionAsset") or "").upper()
            if c <= 0:
                continue
            if asset == "USDT":
                fee_usdt += c
            else:
                # 兜底：極少見不是 USDT，拿 symbol 最新價近似轉 USDT（USDT-M 幾乎用不到）
                try:
                    px = float(binance_get("/fapi/v1/ticker/price", f"symbol={symbol}")["price"])
                    fee_usdt += c * px
                except Exception:
                    fee_usdt += c
        return float(fee_usdt)
    except Exception as e:
        add_log(f"fetch_commission_usdt fail {symbol}: {type(e).__name__}: {e}", "yellow")
        return 0.0
def binance_signed(method, path, params: dict):
    ts = utc_ms() + SERVER_TIME_OFFSET_MS
    params.setdefault("recvWindow", 20000)
    params["timestamp"] = ts
    query = urlencode(dict(sorted(params.items())), doseq=True, safe="")
    sig = sign_params(query)
    headers = {"X-MBX-APIKEY": API_KEY}
    url = f"{BASE_URL}{path}?{query}&signature={sig}"
    func = {"POST": requests.post, "DELETE": requests.delete, "GET": requests.get}.get(method, requests.get)
    r = func(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()
    
# === Key-only endpoints (listenKey 需要簽名不用，但要 API-KEY header) ===
def binance_keyonly(method, path, params: dict | None = None):
    url = f"{BASE_URL}{path}"
    headers = {"X-MBX-APIKEY": API_KEY}
    if params:
        from urllib.parse import urlencode
        url += f"?{urlencode(params, doseq=True, safe='')}"
    if method == "POST":
        r = requests.post(url, headers=headers, timeout=10)
    elif method == "PUT":
        r = requests.put(url, headers=headers, timeout=10)
    elif method == "DELETE":
        r = requests.delete(url, headers=headers, timeout=10)
    else:
        r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json() if r.text else {}
    
def daily_reset_if_needed():
    today = datetime.now(TZ).date()

    # --- SIM 照舊（開機或跨日都直接重設）---
    if ACCOUNT_SIM._last_reset_date is None or ACCOUNT_SIM._last_reset_date != today:
        ACCOUNT_SIM.reset_daily()

    # --- LIVE：必須「先有真實權益」才重設 ---
    # 這裡用 _wallet 是否為 None 當作「已同步過一次」的旗標
    if ACCOUNT_LIVE._wallet is not None:
        if ACCOUNT_LIVE._last_reset_date is None or ACCOUNT_LIVE._last_reset_date != today:
            ACCOUNT_LIVE.daily_start_equity = ACCOUNT_LIVE.balance  # 此時 balance 已是真實 equity
            ACCOUNT_LIVE.daily_pnl = 0.0
            ACCOUNT_LIVE._last_reset_date = today
    # 若還沒同步成功（_wallet is None），就先不動 LIVE 的日基準
    
def pos_count_including_inflight(which: str):
    cnt = pos_count_active(which)
    if which.upper()=="LIVE":
        # 只在 LIVE 管制名額（SIM 也要就把條件拿掉）
        cnt += len(OPEN_INFLIGHT)
    return cnt
    
def confident_symbols(min_p=None, top_k=None, fresh_sec=90):
    th = float(min_p) if (min_p is not None) else float(ML.threshold)
    rows = []
    now_ms = utc_ms()
    for s in SYMBOLS:
        st = SYMAP.get(s)
        if not st:
            continue
        p, ts = getattr(st, "ml_p", None), getattr(st, "ml_p_ts", 0)
        if (p is None) or (now_ms - ts > fresh_sec*1000):
            continue
        if p >= th:
            rows.append((s, float(p)))
    rows.sort(key=lambda x: x[1], reverse=True)
    if top_k:
        rows = rows[:int(top_k)]
    return [s for s, _ in rows]
    
def preferred_scan_order():
    head = confident_symbols(min_p=ML.threshold, top_k=len(SYMBOLS), fresh_sec=90)
    tail = [s for s in SYMBOLS if s not in head]
    return head + tail
    
def symbol_is_blocked(symbol: str, which: str):
    st = SYMAP.get(symbol)
    if not st:
        return False
    has_pos = _get_pos(st, which) is not None
    in_flight = (symbol in OPEN_INFLIGHT)
    return has_pos or in_flight
    
def _snapshot_indicators(st: "SymbolState"):
    """備援：從目前狀態萃取一組穩定特徵，給 ML.record_open 使用。"""
    try:
        last = st.candles[-1] if st.candles else None
        close_px = last["close"] if last else (st.last_price or 0.0)
        bbw = st.bb_width() or 0.0
        # MACD/RSI（與 generate_signal 一致）
        r = None; m = None
        if st.candles and len(st.candles) >= MACD_SLOW + MACD_SIG + 5:
            closes = [c["close"] for c in st.candles]
            r = rsi_calc(closes, RSI_LEN)
            m = macd_calc(closes, MACD_FAST, MACD_SLOW, MACD_SIG)
        macd_val, macd_sig, macd_hist = (m if m else (None, None, None))
        return {
            "price": close_px,
            "ema_fast": st.ema_fast, "ema_slow": st.ema_slow,
            "bb_mid": st.bb_mid, "bb_up": st.bb_up, "bb_dn": st.bb_dn,
            "bb_width": bbw,
            "vwap": st.vwap,
            "rsi": r,
            "macd": macd_val, "macd_sig": macd_sig, "macd_hist": macd_hist,
        }
    except Exception:
        return {}
        
# ===== PATCH D1: ATR utilities =====
def _true_range(prev_close, high, low):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))

def atr_wilder(candles, n=14):
    """
    Wilder's ATR (EMA-like smoothing).
    candles: list of dicts with keys 'high','low','close'
    """
    if len(candles) < n + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        pc = candles[i-1]["close"]
        h  = candles[i]["high"]; l = candles[i]["low"]
        trs.append(_true_range(pc, h, l))
    # 初始 ATR = 前 n 個 TR 的 SMA
    init = sum(trs[:n]) / n
    atr = init
    alpha = 1.0 / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n  # Wilder smoothing
        # 等價：atr = atr + alpha*(tr - atr)
    return atr

def tp_sl_by_atr(entry: float, side: str, atr_val: float,
                 k_sl: float = 2.0, k_tp: float = 3.5,
                 min_pct: float = 0.0025):
    """
    以 ATR 設定 SL/TP，並加上最小百分比距離下限：
    - k_sl/k_tp：ATR 倍數
    - min_pct：最低距離（例如 0.0025 = 0.25%）
    """
    if not atr_val or atr_val <= 0:
        return tp_sl_by_margin(entry, side, TP_MARGIN_PCT, SL_MARGIN_PCT, LEVERAGE)

    # 距離 = max(ATR 倍數, 最小百分比)
    sl_dist = max(k_sl * atr_val, entry * min_pct)
    tp_dist = max(k_tp * atr_val, entry * (min_pct * 2.0))  # TP 給更寬一點

    if side == "LONG":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist
    return (tp, sl)
    

# === User Data Stream (Futures) ===
LISTENKEY_TTL_SEC = 60*60         # 官方 60 分鐘過期
LISTENKEY_KEEPALIVE_SEC = 30*60   # 30 分鐘保活一次

def keepalive_listen_key(listen_key: str):
    try:
        requests.put(
            f"{BASE_URL}/fapi/v1/listenKey?listenKey={listen_key}",
            headers={"X-MBX-APIKEY": API_KEY},
            timeout=10
        )
    except Exception as e:
        console.print(f"[red]listenKey keepalive error: {e}[/red]")
        
def create_listen_key():
    if not API_KEY:
        raise RuntimeError("API_KEY missing for listenKey")
    j = binance_keyonly("POST", "/fapi/v1/listenKey", {})
    return j["listenKey"]

def cleanup_orders(symbol: str):
    """平倉後清掉 reduceOnly/closePosition 的 TP/SL 委託，避免殘單。"""
    if TESTNET or not API_KEY or not API_SECRET:
        return
    try:
        orders = binance_signed("GET", "/fapi/v1/openOrders", {"symbol": symbol})
        to_cancel = []
        for o in orders:
            if str(o.get("reduceOnly", "false")).lower() == "true" or str(o.get("closePosition", "false")).lower() == "true":
                to_cancel.append(o["orderId"])
        for oid in to_cancel:
            try:
                binance_signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": oid})
                console.print(f"[yellow]Cleanup: 已取消 {symbol} 委託單 {oid}[/yellow]")
                add_log(f"Cleanup cancel {symbol} oid={oid}", "yellow")
            except Exception as e:
                console.print(f"[red]取消委託失敗 {symbol} {oid}: {e}[/red]")
        if to_cancel:
            console.print(f"[green]Cleanup 完成: {symbol} 共清掉 {len(to_cancel)} 張委託[/green]")
    except Exception as e:
        console.print(f"[red]cleanup_orders error ({symbol}): {e}[/red]")

def cancel_all_open_orders(symbol: str):
    """直接用官方一鍵取消該幣別所有開放委託（最保險）。"""
    if TESTNET or not API_KEY or not API_SECRET:
        return
    try:
        binance_signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol, "recvWindow": 5000})
        console.print(f"[green]All open orders canceled for {symbol}[/green]")
    except Exception as e:
        console.print(f"[red]cancel_all_open_orders error ({symbol}): {e}[/red]")


    
# ===== PATCH E: Regime filter =====
REGIME_FILTER_ON = True
MIN_LIQ_QUANTILE = 0.25     # 24h quoteVolume 至少在樣本第 25 百分位
MAX_ABS_FUNDING  = 0.0006   # |資金費率| 過大則跳過
BBW_MIN          = 0.003    # 太窄不打
BBW_MAX          = 0.030    # 太寬不追

def _regime_pass(symbol: str) -> bool:
    if not REGIME_FILTER_ON:
        return True
    try:
        vol_map = _get_24h_quote_volume_map_cached()
        vols = [vol_map.get(s, 0.0) for s in SYMBOLS if s in vol_map]
        if not vols:
            return True
        v = vol_map.get(symbol, 0.0)
        q25 = sorted(vols)[max(0, int(0.25 * (len(vols)-1)))]
        if v < q25:
            return False
    except Exception:
        pass
    try:
        fr = abs(_get_funding_rate_map_cached([symbol]).get(symbol, 0.0))
        if fr > MAX_ABS_FUNDING:
            return False
    except Exception:
        pass
    try:
        st = SYMAP[symbol]
        w = st.bb_width() or 0.0
        if w < BBW_MIN or w > BBW_MAX:
            return False
    except Exception:
        pass
    return True

def _live_reduce_only_market_close(symbol: str, side: str, qty: float):
    """
    強化版：先以交易所實際持倉為準，對齊 stepSize，必要時重試與 fallback。
    side: 現有部位方向（"LONG"/"SHORT"），會自動下相反邊。
    """
    rules = _get_symbol_rules(symbol)
    step  = float(rules["stepSize"])
    opp   = "SELL" if side == "LONG" else "BUY"

    # 1) 讀交易所實際倉位（以免本地數量不準）
    try:
        risks = binance_signed("GET", "/fapi/v2/positionRisk", {})
        exch_amt = 0.0
        for r in risks:
            if r.get("symbol") == symbol:
                exch_amt = float(r.get("positionAmt") or 0.0)
                break
    except Exception as e:
        exch_amt = 0.0
        add_log(f"read positionRisk fail, will still try close: {e}", "yellow")

    pos_qty = abs(exch_amt)
    # 如果交易所顯示無倉，直接返回（視為已平）
    if pos_qty <= 0:
        return {"status": "already_closed", "qty": 0.0}

    # 2) 以「實際倉位」優先，其次用呼叫者傳入 qty，最後對齊步長
    q0 = min(max(1e-18, float(qty or 0.0)), pos_qty) if qty else pos_qty
    q  = _floor_to_step(q0, step)
    if q <= 0:
        # 最少平掉 1 個 step（若仍 0 就放棄）
        q = step
        if q > pos_qty:
            return {"status": "already_closed", "qty": 0.0}

    # 3) 先嘗試 reduceOnly 市價單（重試 2 次，失敗就 fallback）
    last_err = None
    for attempt in range(3):
        try:
            res = binance_signed("POST", "/fapi/v1/order", {
                "symbol": symbol,
                "side": opp,
                "type": "MARKET",
                "quantity": _fmt_to_tick(q, step),  # 雖然是 qty，但沿用同一格式器避免小數誤差
                "reduceOnly": "true",
                "newOrderRespType": "RESULT",
                "recvWindow": 10000,
            })
            return res
        except Exception as e:
            last_err = e
            # 常見：ReduceOnly 被拒（數量略大於實際可平），就減 1 個 step 再試
            q = max(step, _floor_to_step(q - step, step))

    # 4) Fallback：掛 closePosition=true 的 STOP_MARKET（全數平倉）
    try:
        res2 = binance_signed("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": opp,
            "type": "STOP_MARKET",
            "closePosition": "true",
            "stopPrice": _fmt_to_tick(1e-8, step),  # 值無所謂，closePosition=true 會忽略實際數量
            "workingType": "CONTRACT_PRICE",
            "priceProtect": "true",
            "recvWindow": 10000,
        })
        return res2
    except Exception as e2:
        # 最後回報錯誤給上層處理（上層會做本地結算 + 對帳補平）
        raise RuntimeError(f"reduceOnly+fallback failed: {last_err} / {e2}")
    
# ===================== 幣池（固定/隨機） =====================
def fetch_all_symbols():
    try:
        info = binance_get("/fapi/v1/exchangeInfo")
        return [s["symbol"] for s in info["symbols"]
                if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    except Exception as e:
        console.print(f"[red]fetch_all_symbols error: {e}[/red]")
        return []

def pick_random_symbols(n=27, top_volume=True):
    all_syms = fetch_all_symbols()
    if not all_syms: return []
    syms = [s for s in all_syms if s not in BLACKLIST]
    if top_volume:
        tickers = binance_get("/fapi/v1/ticker/24hr")
        vol_map = {t["symbol"]: float(t["quoteVolume"]) for t in tickers if t["symbol"] in syms}
        syms = sorted(syms, key=lambda x: vol_map.get(x,0), reverse=True)[:100]
    return random.sample(syms, min(n, len(syms)))

def refresh_symbol_pool(n=27, top_volume=True):
    """
    用 AI p 值自動挑選掃描池，並保留持倉。
    自動偵測多空方向：
      - 平均 p >= 0.55 → 掃多頭 (LONG)
      - 平均 p <= 0.45 → 掃空頭 (SHORT)
      - 介於中間 → 掃高信心 (CONFIDENCE)
    若 p 不足或模型尚未穩定，回退至原 pick_random_symbols。
    """
    import time
    global SYMBOLS, SYMAP, _LAST_AI_FALLBACK_TS, _LAST_POOL_FPRINT

    base_candidates = pick_random_symbols(max(n * 3, n), top_volume=top_volume)
    conf = confident_symbols(min_p=ML.threshold, top_k=n, fresh_sec=120)
    held_symbols = []
    MIN_CONF_FRACTION = 0.4  # 至少 40% 名額由高信心保送
    selected = []

    if len(conf) >= int(n * MIN_CONF_FRACTION):
        selected.extend([s for s in conf if s not in held_symbols])
        for s in base_candidates:
            if s in held_symbols or s in selected:
                continue
            selected.append(s)
            if len(held_symbols) + len(selected) >= n:
                break
    else:
        # 信心不足 → 完全走你原先 scored/mode/selected 的那一段（保留）
        pass
    # === 保留有倉的 symbol（原樣保留） ===
    held_symbols = []
    for sym, st in SYMAP.items():
        if (st.position_live and abs(float(st.position_live.get("qty", 0) or 0)) > 0) \
           or (st.position_sim  and abs(float(st.position_sim.get("qty", 0)  or 0)) > 0):
            held_symbols.append(sym)

    # === 確保 state 並算出 p 值（原樣保留） ===
    def _ensure_state(s):
        if s not in SYMAP:
            SYMAP[s] = SymbolState(s)
        return SYMAP[s]

    def _infer_p(st):
        try:
            x = _features_from_state(st)
            if not x or ML.model.n_seen < 10:
                return None
            return float(ML.model.predict_proba(x))
        except Exception:
            return None

    scored = []
    for s in base_candidates:
        st = _ensure_state(s)
        p = _infer_p(st)
        if p is not None:
            scored.append((s, p))

    # === 回退路徑：節流＋無變更不重印 ===
    if not scored:
        new_pool = base_candidates[:n]
        sig = ",".join(sorted(new_pool))
        if sig != _LAST_POOL_FPRINT:
            # 20 秒節流一下 fallback 訊息，避免刷屏
            now = time.time()
            if now - _LAST_AI_FALLBACK_TS >= 20:
                console.print("[yellow]AI score insufficient, fallback to random pool[/yellow]")
                _LAST_AI_FALLBACK_TS = now
            SYMBOLS = new_pool
            for s in SYMBOLS:
                if s not in SYMAP:
                    SYMAP[s] = SymbolState(s)
            console.print(f"[cyan]Symbol pool refreshed: {len(SYMBOLS)} symbols (保留持倉 {len(held_symbols)})[/cyan]")
            _LAST_POOL_FPRINT = sig
        return  # ← 無論有沒有變更都結束

    # === 自動判斷市場方向（原樣保留） ===
    avg_p = sum(p for _, p in scored) / len(scored)
    if avg_p >= 0.53:
        mode = "LONG"
    elif avg_p <= 0.47:
        mode = "SHORT"
    else:
        mode = "CONFIDENCE"

    # === 根據模式排序（原樣保留） ===
    if mode == "LONG":
        scored.sort(key=lambda x: x[1], reverse=True)
    elif mode == "SHORT":
        scored.sort(key=lambda x: x[1])  # 越低越偏空
    else:
        scored.sort(key=lambda x: abs(x[1] - 0.5), reverse=True)

    # === 挑前 N 檔（扣掉持倉）（原樣保留） ===
    selected = []
    for s, _ in scored:
        if s in held_symbols:
            continue
        selected.append(s)
        if len(held_symbols) + len(selected) >= n:
            break

    # === 合併並確保有 state（原樣保留） ===
    new_pool = list(dict.fromkeys(held_symbols + selected))[:n]

    # === 無變更不重印（新增） ===
    sig = ",".join(sorted(new_pool))
    if sig == _LAST_POOL_FPRINT:
        # 幣池沒變就靜默返回；補確保 state（避免畫面漏顯示）
        for s in new_pool:
            if s not in SYMAP:
                SYMAP[s] = SymbolState(s)
        return

    SYMBOLS = new_pool
    for s in SYMBOLS:
        if s not in SYMAP:
            SYMAP[s] = SymbolState(s)

    console.print(f"[cyan]Symbol pool refreshed: {len(SYMBOLS)} symbols (保留持倉 {len(held_symbols)}) | avg_p={avg_p:.3f} → 模式 {mode}[/cyan]")
    add_log(f"AI pool refreshed mode={mode} avg_p={avg_p:.3f} held={len(held_symbols)}", "dim")
    _LAST_POOL_FPRINT = sig

# ===================== 槓桿/逐倉設定 =====================
def ensure_isolated_and_leverage(symbols, leverage: int):
    if TESTNET or not API_KEY or not API_SECRET:
        return
    for sym in symbols:
        try:
            binance_signed("POST", "/fapi/v1/marginType", {
                "symbol": sym, "marginType": "ISOLATED", "recvWindow": 5000
            })
        except Exception:
            pass
        try:
            binance_signed("POST", "/fapi/v1/leverage", {
                "symbol": sym, "leverage": leverage, "recvWindow": 5000
            })
        except Exception:
            pass

# ===================== 帳戶/持倉 =====================
BAL_SYNC_SECONDS = 5
_last_bal_sync = 0.0
_last_pos_sync = 0.0
POS_SYNC_SECONDS = 5   # 與交易所同步持倉的週期（秒）

def sync_live_positions_periodic():
    """定期與交易所同步真實倉位；網路故障時會退避。"""
    global _last_pos_sync, _NET_BACKOFF_UNTIL, _NET_BACKOFF_STEP
    if TESTNET or not API_KEY or not API_SECRET:
        return

    now = time.time()
    if now < _NET_BACKOFF_UNTIL:
        return

    if now - _last_pos_sync < POS_SYNC_SECONDS:
        return
    _last_pos_sync = now

    try:
        risks = binance_signed("GET", "/fapi/v2/positionRisk", {})
        # 成功 → 清除退避
        _NET_BACKOFF_UNTIL = 0.0
        _NET_BACKOFF_STEP  = 60.0
    except requests.exceptions.RequestException as e:
        msg = str(e)
        # DNS/連線類錯誤：進入退避
        _NET_BACKOFF_UNTIL = now + _NET_BACKOFF_STEP
        _NET_BACKOFF_STEP  = min(_NET_BACKOFF_MAX, _NET_BACKOFF_STEP * 1.7)
        add_log(f"pos sync backoff {int(_NET_BACKOFF_STEP)}s: {e.__class__.__name__}", "yellow")
        return
    except Exception as e:
        # 其他未知錯誤：保持原頻率，但記錄
        add_log(f"pos sync error: {e}", "red")
        return

    # ====== 正常邏輯（你原本的 on_exch 對比）======
    on_exch = {}
    for r in risks:
        try:
            sym   = r.get("symbol", "")
            amt   = float(r.get("positionAmt", "0"))
            entry = float(r.get("entryPrice", "0"))
            mark  = float(r.get("markPrice", "0") or 0)
            on_exch[sym] = {"amt": amt, "entry": entry, "mark": mark}
        except:
            continue

    to_check = set(SYMBOLS)
    for s, st0 in SYMAP.items():
        if st0.position_live:  # 本地顯示有倉，就一定要同步
            to_check.add(s)

    for s in to_check:
        st = SYMAP.get(s)
        if not st:
            continue
        exch = on_exch.get(s, {"amt": 0.0, "entry": 0.0, "mark": st.last_price or 0.0})
        amt  = exch["amt"]
        mark = exch["mark"] or (st.last_price or 0.0)

        local_pos = st.position_live

        if abs(amt) <= 0.0:
            # 交易所無倉，但本地有 → 視為被 TP/SL 出場
            if local_pos:
                px = mark if mark > 0 else local_pos["entry"]
                close_position_one(s, "Exchange TP/SL filled", px, "LIVE", skip_exchange=True)
        else:
            # 交易所有倉
            side  = "LONG" if amt > 0 else "SHORT"
            qty   = abs(amt)
            entry = exch["entry"]

            # 判斷是「新建」或「覆寫」
            created_or_changed = False
            if not local_pos:
                st.position_live = {"side": side, "qty": qty, "entry": entry, "trail": None}
                created_or_changed = True
            else:
                # 若方向/數量/均價不同就覆寫
                if (local_pos["side"] != side or
                    abs(float(local_pos["qty"]) - qty) > 1e-9 or
                    abs(float(local_pos["entry"]) - entry) > 1e-9):
                    st.position_live = {"side": side, "qty": qty, "entry": entry, "trail": None}
                    created_or_changed = True

            # >>> PATCH: 只要本地剛建立/覆寫了 LIVE 倉位，就補註冊一筆開倉特徵給 ML
            if created_or_changed:
                st.position_live.setdefault("pos_uid", f"LIVE:{s}:{utc_ms()}:{uuid.uuid4().hex[:8]}")
                try:
                    # 避免重複註冊：若已經有 open sample 就略過
                    if not ML.has_open_sample("LIVE", s):
                        feat = _features_from_state(st)
                        if feat is None:
                            # 與 place_order_one() 的 fallback 邏輯一致
                            snap = _snapshot_indicators(st) or {}
                            rsi = (snap.get("rsi") or 50.0) / 100.0
                            macd_hist = snap.get("macd_hist") or 0.0
                            ema_gap = ((st.ema_fast - st.ema_slow)/st.last_price) if (st.ema_fast and st.ema_slow and st.last_price) else 0.0
                            bbw = snap.get("bb_width") or 0.0
                            vwap_dev = ((st.last_price - st.vwap)/st.last_price) if (st.vwap and st.last_price) else 0.0
                            # 安全裁切
                            def clip(val, lo, hi): return max(lo, min(hi, val))
                            feat = [
                                clip(rsi, 0, 1),
                                clip(macd_hist, -1, 1),
                                clip(ema_gap, -0.05, 0.05),
                                clip(bbw, 0, 0.05),
                                clip(vwap_dev, -0.05, 0.05),
                                0.0,  # vol_z
                                0.0,  # atr_rel
                                0.0,  # ema_slope
                            ]
                        ML.record_open("LIVE", s, feat, p=None)
                        # 也把特徵存進本地倉，之後平倉找不到時還能回收
                        try:
                            st.position_live["ml_feat"] = feat
                            st.position_live.setdefault("opened_ts", utc_ms())
                        except Exception:
                            pass
                        console.print(f"[dim]LIVE open-feat registered on sync: {s}[/dim]")
                except Exception as _e:
                    console.print(f"[dim]ML record_open on sync skipped: {_e}[/dim]")
            # <<< PATCH END

            # 交易所有倉已寫回本地 → 釋放 inflight
            with ORDER_LOCK:
                OPEN_INFLIGHT.discard(s)
                
class Account:
    def __init__(self, name=""):
        self.name = name
        self.balance = 10_000.0
        self.daily_start_equity = None   # ← 改成 None，等第一次 reset_daily 設定
        self.daily_pnl = 0.0
        self.total_pnl = 0.0
        self.trades = []
        # 以下僅 LIVE 會填
        self._wallet = None
        self._available = None
        self._unrealized = None
        # 新增：紀錄哪一天已重設
        self._last_reset_date = None

    def reset_daily(self):
        self.daily_start_equity = self.balance
        self.daily_pnl = 0.0
        self._last_reset_date = datetime.now(TZ).date()

ACCOUNT_LIVE = Account("LIVE")
ACCOUNT_SIM  = Account("SIM")
# ======== SIM 狀態持久化 ========
STATE_PATH = os.getenv("SIM_STATE_PATH", "sim_state.json")
AUTOSAVE_SEC = 15
_last_state_save = 0.0
_state_lock = threading.Lock()

def _ensure_sym_exists(sym: str):
    if sym not in SYMAP:
        SYMAP[sym] = SymbolState(sym)
        # 可選：把它加回掃描池，避免面板漏顯示
        if sym not in SYMBOLS:
            SYMBOLS.append(sym)

def snapshot_sim_state():
    """擷取 SIM 帳戶 + SIM 持倉（每個 symbol）+ 交易紀錄。"""
    with _state_lock:
        positions = {}
        for s, st in SYMAP.items():
            if st.position_sim:
                # 只存必要欄位，避免寫入非 JSON 可序列化的東西
                p = st.position_sim
                positions[s] = {
                    "side":  p.get("side"),
                    "qty":   float(p.get("qty", 0.0)),
                    "entry": float(p.get("entry", 0.0)),
                    "sl":    float(p.get("sl", 0.0)) if p.get("sl") else None,
                    "tp":    float(p.get("tp", 0.0)) if p.get("tp") else None,
                }
        data = {
            "ver": 1,
            "ts": int(time.time()),
            "account": {
                "balance": float(ACCOUNT_SIM.balance),
                "daily_start_equity": float(ACCOUNT_SIM.daily_start_equity or ACCOUNT_SIM.balance),
                "daily_pnl": float(ACCOUNT_SIM.daily_pnl),
                "total_pnl": float(ACCOUNT_SIM.total_pnl),
            },
            "trades": ACCOUNT_SIM.trades[-500:],  # 避免無限成長，保留最近 500 筆即可
            "positions": positions,
            # 方便還原畫面（可選）
            "interval": INTERVAL,
        }
        return data

def persist_sim_state(force=False):
    global _last_state_save
    now = time.time()
    if (not force) and (now - _last_state_save < AUTOSAVE_SEC):
        return

    try:
        data = snapshot_sim_state()
        dir_ = os.path.dirname(STATE_PATH)
        if dir_:
            os.makedirs(dir_, exist_ok=True)

        # —— 以同目錄 NamedTemporaryFile 確保同檔系統原子替換 ——
        import tempfile, json    # ❌ 這裡刪掉 os，只保留 tempfile, json
        with _state_lock:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=(dir_ if dir_ else "."),
                prefix=os.path.basename(STATE_PATH) + ".tmp."
            ) as tf:
                json.dump(data, tf, indent=2, ensure_ascii=False)
                tf.flush()
                os.fsync(tf.fileno())
                tmp_path = tf.name

            try:
                os.replace(tmp_path, STATE_PATH)  # 原子替換
            except Exception as e:
                # 若 replace 失敗，最後手段：直接覆寫
                try:
                    with open(STATE_PATH, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                    # 清掉可能殘留的 tmp
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                except Exception as e2:
                    add_log(f"persist_sim_state hard-fail: {type(e2).__name__}: {e2}", "red")

        _last_state_save = now

    except Exception as e:
        add_log(f"persist_sim_state error: {type(e).__name__}: {e}", "red")
        
# 若還沒在全域宣告，請在上方全域區加：
# _SIM_STATE_DONE = False

def restore_sim_state():
    """啟動時還原 SIM 錢包與持倉（只執行一次）。"""
    global _SIM_STATE_DONE
    if _SIM_STATE_DONE:
        return

    try:
        if not os.path.exists(STATE_PATH):
            _SIM_STATE_DONE = True
            return

        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        console.print(f"[red]restore_sim_state read error: {e}[/red]")
        _SIM_STATE_DONE = True
        return

    try:
        acc = data.get("account", {})
        ACCOUNT_SIM.balance = float(acc.get("balance", ACCOUNT_SIM.balance))
        ACCOUNT_SIM.daily_start_equity = float(acc.get("daily_start_equity", ACCOUNT_SIM.balance))
        ACCOUNT_SIM.daily_pnl = float(acc.get("daily_pnl", 0.0))
        ACCOUNT_SIM.total_pnl = float(acc.get("total_pnl", 0.0))

        # 交易紀錄
        tr = data.get("trades", [])
        if isinstance(tr, list):
            ACCOUNT_SIM.trades = tr

        # 還原 SIM 持倉
        positions = data.get("positions", {}) or {}
        for sym, p in positions.items():
            _ensure_sym_exists(sym)
            st = SYMAP[sym]
            st.position_sim = {
                "side":  p.get("side"),
                "qty":   float(p.get("qty", 0.0)),
                "entry": float(p.get("entry", 0.0)),
                "sl":    float(p.get("sl", 0.0)) if p.get("sl") else None,
                "tp":    float(p.get("tp", 0.0)) if p.get("tp") else None,
                "trail": None,
            }

        cnt = len(positions)
        if cnt > 0:
            console.print(f"[green]SIM state restored: {cnt} position(s)[/green]")
        else:
            console.print("[dim]SIM state restored: 0 position(s)[/dim]")

    except Exception as e:
        console.print(f"[red]restore_sim_state parse error: {e}[/red]")
    finally:
        # 無論成功或失敗，都標記已執行，避免重複輸出
        _SIM_STATE_DONE = True

def autosave_state_worker():
    while True:
        try:
            persist_sim_state(force=False)
            time.sleep(AUTOSAVE_SEC)
        except Exception as e:
            console.print(f"[red]autosave_state_worker error: {e}[/red]")
            time.sleep(AUTOSAVE_SEC)
            
def get_account(which: str) -> Account:
    return ACCOUNT_LIVE if which.upper() == "LIVE" else ACCOUNT_SIM

def _append_trade_and_realize_pnl(which: str, symbol: str, side: str,
                                  entry: float, exit_px: float, qty: float,
                                  reason: str, fee_usdt: float = 0.0,
                                  extra: dict | None = None):
    acc = get_account(which)
    sign = +1 if side == "LONG" else -1
    pnl_cash = (float(exit_px) - float(entry)) * float(qty) * sign
    net_cash = pnl_cash - float(fee_usdt)

    # ✅ 僅在「平倉」時計入 total_pnl（實現）
    acc.total_pnl += float(net_cash)

    # SIM 才要把現金結算入餘額；LIVE 用交易所權益，不從這裡推算
    if which.upper() == "SIM":
        acc.balance += float(net_cash)

    row = {
        "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol, "side": side,
        "entry": float(entry), "exit": float(exit_px), "qty": float(qty),
        "pnl_cash": float(net_cash), "fee_usdt": float(fee_usdt),
        "net_pct": 0.0,  # 你若已有淨百分比算法，可補；沒有就留 0 或計算後覆寫
        "reason": reason, "which": which.upper()
    }
    if extra: row.update(extra)
    acc.trades.append(row)

    # （可選）CSV
    if LOG_TRADES_ON:
        try:
            with _CSV_LOCK:
                import csv
                need_header = not os.path.exists(TRADES_CSV_PATH)
                with open(TRADES_CSV_PATH, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=row.keys())
                    if need_header: w.writeheader()
                    w.writerow(row)
        except Exception as e:
            add_log(f"write trades.csv fail: {e}", "yellow")
            
def sync_live_balance():
    global _last_bal_sync
    if TESTNET or not API_KEY or not API_SECRET:
        return
    now = time.time()
    if now - _last_bal_sync < BAL_SYNC_SECONDS:
        return

    try:
        acc = binance_signed("GET", "/fapi/v2/account", {})
        wallet = float(acc.get("totalWalletBalance", "0"))
        unreal = float(acc.get("totalUnrealizedProfit", "0"))
        avail  = float(acc.get("availableBalance", "0"))
        equity = wallet + unreal

        ACCOUNT_LIVE.balance     = equity
        ACCOUNT_LIVE._wallet     = wallet
        ACCOUNT_LIVE._available  = avail
        ACCOUNT_LIVE._unrealized = unreal

        today = datetime.now(TZ).date()
        if ACCOUNT_LIVE._last_reset_date != today or ACCOUNT_LIVE.daily_start_equity in (None, 0):
            # 第一次拿到今天的真實權益 → 在這裡設為今日基準
            ACCOUNT_LIVE.daily_start_equity = equity
            ACCOUNT_LIVE.daily_pnl = 0.0
            ACCOUNT_LIVE._last_reset_date = today
        else:
            ACCOUNT_LIVE.daily_pnl = equity - ACCOUNT_LIVE.daily_start_equity

        _last_bal_sync = now

    except Exception as e:
        add_log(f"sync_live_balance error: {type(e).__name__}: {e}", "red")
        _last_bal_sync = now
        
def restore_live_positions():
    """啟動時從交易所恢復真實倉位 -> 寫入 position_live；不動 position_sim。"""
    if TESTNET or not API_KEY or not API_SECRET:
        return
    try:
        risks = binance_signed("GET", "/fapi/v2/positionRisk", {})
    except Exception as e:
        console.print(f"[red]restore_live_positions error: {e}[/red]")
        return

    # 清空所有 symbol 的 LIVE 部位
    for s in SYMAP:
        SYMAP[s].position_live = None

    count = 0
    for r in risks:
        try:
            sym   = r.get("symbol", "")
            amt   = float(r.get("positionAmt", "0"))
            entry = float(r.get("entryPrice", "0"))
            mark  = float(r.get("markPrice", "0") or 0)
            if abs(amt) <= 0 or entry <= 0:
                continue
            side = "LONG" if amt > 0 else "SHORT"
            qty  = abs(amt)
            if sym not in SYMAP:
                SYMAP[sym] = SymbolState(sym)
            st = SYMAP[sym]
            st.position_live = {
                "side": side, "qty": qty, "entry": entry, "trail": None,
                "pos_uid": f"LIVE:{sym}:{utc_ms()}:{uuid.uuid4().hex[:8]}"  # ← 加這行
            }
            if st.last_price is None and mark > 0:
                st.last_price = mark
            count += 1
            # 🟢 啟動時自動註冊一份 ML 開倉樣本（避免 record_close 時無 open_feats）
            try:
                feat = _features_from_state(st)
                if feat:
                    ML.record_open("LIVE", sym, feat, p=None)
            except Exception:
                pass
        except Exception:
            continue
    if count > 0:
        console.print(f"[green]Restored {count} live position(s) from Binance[/green]")

# ===================== 技術指標 =====================
def ema(prev, price, length):
    if prev is None: return price
    k = 2/(length+1)
    return price*k + prev*(1-k)

def sma(arr): return sum(arr)/len(arr) if arr else None

def std(arr):
    m = sma(arr)
    return math.sqrt(sum((x-m)**2 for x in arr)/len(arr)) if arr else None

def rsi_calc(closes, length=14):
    if len(closes) < length+1: return None
    gains, losses = 0.0, 0.0
    for i in range(-length,0):
        diff = closes[i] - closes[i-1]
        if diff >= 0: gains += diff
        else: losses -= diff
    rs = (gains/length) / ((losses/length) if losses>0 else 1e-9)
    return 100 - 100/(1+rs)

def macd_calc(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig: return None
    ef, es = None, None
    macds = []
    for px in closes:
        ef = ema(ef, px, fast)
        es = ema(es, px, slow)
        macds.append(ef - es)
    if len(macds) < sig: return None
    signal = None
    for m in macds:
        signal = ema(signal, m, sig)
    return macds[-1], signal, macds[-1]-signal

def vwap_calc(candles):
    vol_sum = sum(c["volume"] for c in candles)
    if vol_sum <= 0: return None
    pv = sum(c["typical"]*c["volume"] for c in candles)
    return pv/vol_sum

# ====== ML: Online Logistic Filter（無第三方依賴） ======
class OnlineLogit:
    def __init__(self, n_features, lr=0.05, l2=1e-5):
        self.w = [0.0]* (n_features+1)  # +1 for bias
        self.lr = lr
        self.l2 = l2
        self.n_seen = 0

    @staticmethod
    def _sigmoid(z):
        # 防溢位
        if z >= 0:
            ez = math.exp(-z); return 1.0/(1.0+ez)
        else:
            ez = math.exp(z);  return ez/(1.0+ez)

    def predict_proba(self, x):
        z = self.w[0] + sum(wi*xi for wi,xi in zip(self.w[1:], x))
        return self._sigmoid(z)

    def partial_fit(self, x, y, sample_weight: float = 1.0):
        # y ∈ {0,1}
        p = self.predict_proba(x)
        # 權重放大梯度
        g0 = (p - y) * sample_weight
        self.w[0] -= self.lr * (g0 + self.l2 * self.w[0])
        for i, xi in enumerate(x, start=1):
            g = ((p - y) * xi + self.l2 * self.w[i]) * sample_weight
            self.w[i] -= self.lr * g
        self.n_seen += 1


# === 機器學習 特徵抽取（用你現有指標組成向量） ===
def _features_from_state(st: "SymbolState"):
    if not st.candles:
        return None
    closes = [c["close"] for c in st.candles]
    vols   = [c["volume"] for c in st.candles]
    need = max(MACD_SLOW+MACD_SIG+5, RSI_LEN+1, BB_LEN+1, VOL_MA)
    if len(closes) < need:
        return None

    # 指標
    r = rsi_calc(closes, RSI_LEN) or 50.0
    m = macd_calc(closes, MACD_FAST, MACD_SLOW, MACD_SIG)
    macd_hist = (m[2] if m else 0.0) or 0.0

    ema_gap = 0.0
    if st.ema_fast and st.ema_slow and st.last_price:
        ema_gap = (st.ema_fast - st.ema_slow) / st.last_price

    bbw = st.bb_width() or 0.0

    vwap_dev = 0.0
    if st.vwap and st.last_price:
        vwap_dev = (st.last_price - st.vwap) / st.last_price

    v_ma = sum(vols[-VOL_MA:]) / VOL_MA
    v_sd = (sum((x - v_ma)**2 for x in vols[-VOL_MA:]) / VOL_MA) ** 0.5 if VOL_MA>0 else 0.0
    vol_z = ((vols[-1] - v_ma) / v_sd) if v_sd > 0 else 0.0

    def clip(val, lo, hi):
        return max(lo, min(hi, val))

    # 你打算新增的兩個特徵
    try:
        atr = max(st.candles[-1]["high"], st.candles[-2]["high"]) - min(st.candles[-1]["low"], st.candles[-2]["low"])
        atr_rel = atr / max(1e-9, st.candles[-1]["close"])
    except Exception:
        atr_rel = 0.0

    ema_slope = 0.0
    if st.bb_mid:
        ema_slope = (st.ema_fast - st.ema_slow) / (st.bb_mid or 1.0)

    x = [
        clip(r/100.0, 0, 1),            # 1: RSI (0~1)
        clip(macd_hist, -1, 1),         # 2: MACD hist
        clip(ema_gap, -0.05, 0.05),     # 3: EMA gap%
        clip(bbw, 0, 0.05),             # 4: BB width
        clip(vwap_dev, -0.05, 0.05),    # 5: VWAP dev%
        clip(vol_z/3.0, -2.0, 2.0),     # 6: volume z-score (縮放)
        clip(atr_rel*1.5, 0, 0.15),          # 7: ATR 相對值
        clip(ema_slope, -0.05, 0.05),   # 8: EMA 斜率
    ]
    return x

# === ML 即時信心評分（p 值） ===
def eval_ml_confidence_for_symbol(st: "SymbolState") -> float | None:
    """回傳 0~1 的 p 值；資料不足回 None。"""
    try:
        x = _features_from_state(st)
        if x is None:
            # fallback：用快照拼一組特徵
            snap = _snapshot_indicators(st) or {}
            rsi = (snap.get("rsi") or 50.0) / 100.0
            macd_hist = snap.get("macd_hist") or 0.0
            ema_gap = 0.0
            if st.ema_fast and st.ema_slow and st.last_price:
                ema_gap = (st.ema_fast - st.ema_slow) / st.last_price
            bbw = snap.get("bb_width") or 0.0
            vwap_dev = 0.0
            if st.vwap and st.last_price:
                vwap_dev = (st.last_price - st.vwap) / st.last_price
            x = [rsi, macd_hist,
                 max(-0.05, min(0.05, ema_gap)),
                 max(0.0,   min(0.05, bbw)),
                 max(-0.05, min(0.05, vwap_dev)),
                 0.0, 0.0, 0.0]
        if ML.model.n_seen < 10:
            return None
        return float(ML.model.predict_proba(x))
    except Exception:
        return None

# === 全域 ML 管理器 ===
class MLManager:
    def __init__(self, threshold=0.55, train_sources=None):
        self.threshold = threshold
        self.model = OnlineLogit(n_features=8, lr=0.05, l2=1e-6)
        self.recent = deque(maxlen=200)
        self.open_feats = {}
        self.open_probs = {}
        self._last_auto_adj = 0.0
        self.pos_seen = 0
        self.neg_seen = 0
        self.target_precision = 0.58
        # 以傳入值決定可訓練來源（LIVE/SIM），並做大寫正規化
        self.train_sources = set((train_sources or {"LIVE"}))
        self.train_sources = {str(s).upper() for s in self.train_sources}
        self.train_after_seen  = ML_TRAIN_AFTER_SEEN
        self.filter_after_seen = ML_FILTER_AFTER_SEEN

    def _ok(self, which: str) -> bool:
        # 面板映射：POS1→LIVE、POS2→SIM；其餘維持原樣
        w = (which or "").upper()
        if w == "POS1":
            w = "LIVE"
        elif w == "POS2":
            w = "SIM"
        return w in self.train_sources

    # ========== 實用輔助 ==========
    def _recent_precision(self, th: float, k: int = 80):
        arr = list(self.recent)[-k:]
        cand = [(p, y) for p, y in arr if p is not None and p >= th]
        if not cand:
            return None
        tp = sum(1 for _, y in cand if y == 1)
        return tp / len(cand)

    def _brier_recent(self):
        if len(self.recent) < 30:
            return None
        s = 0.0
        n = 0
        for p, y in self.recent:
            if p is None:
                continue
            s += (p - y) ** 2
            n += 1
        return (s / n) if n else None

    def _auto_adjust_threshold(self):
        # >>> PATCH: 可用 ML_AUTO_ADJUST 關閉自動調整
        if not ML_AUTO_ADJUST:
            return
        now = time.time()
        if now - self._last_auto_adj < 60:
            return
        self._last_auto_adj = now

        b = self._brier_recent()
        if b is not None:
            if b > 0.20:
                self.threshold = min(0.85, self.threshold + 0.02)
            elif b < 0.12:
                self.threshold = max(0.50, self.threshold - 0.01)

        pr = self._recent_precision(self.threshold, k=80)
        if pr is not None and pr < self.target_precision:
            self.threshold = min(0.90, self.threshold + 0.02)

    # ========== 主要接口 ==========
    def should_take(self, which, st):
        self._auto_adjust_threshold()
        x = _features_from_state(st)

        # AI 關閉 → 不干預
        if not AI_ENABLE:
            return True, None, x

        # 樣本數不足 → 不干預（行為門檻）
        if (self.pos_seen + self.neg_seen) < AI_MIN_SEEN_FOR_ACTION:
            return True, None, x

        # 直到累計樣本夠多才啟用過濾（你原本的 filter_after_seen）
        if (self.pos_seen + self.neg_seen) < self.filter_after_seen:
            return True, None, x

        if not x or self.model.n_seen < 30:   # 你原本的底線，也保留
            return True, None, x

        p = self.model.predict_proba(x)
        if len(self.recent) >= 50:
            ps = sorted(p0 for p0, _ in self.recent if p0 is not None)
            dyn_th = ps[int(0.60 * (len(ps) - 1))] if ps else self.threshold
            th = max(self.threshold, dyn_th)
        else:
            th = self.threshold
        return (p >= th), p, x
        
    def has_open_sample(self, which, symbol) -> bool:
        return (which.upper(), symbol) in self.open_feats

    def peek_open_sample(self, which, symbol):
        return self.open_feats.get((which.upper(), symbol))

    def record_open(self, which: str, symbol: str, x, p: float | None = None):
        # --- 正規化來源與商品代號 ---
        W = (which or "").upper()
        # 面板映射：Pos1=LIVE, Pos2=SIM（其餘維持原樣）
        if W == "POS1":
            Wn = "LIVE"
        elif W == "POS2":
            Wn = "SIM"
        else:
            Wn = W
        S = (symbol or "").upper()

        # ★ 只紀錄/保留可訓練來源的 open 樣本（其餘直接跳過）
        if not self._ok(Wn):
            return

        key = (Wn, S)

        # 保留你原本的 x/p 行為（不動你的資料結構與模型介面）
        if x is not None:
            self.open_feats[key] = x
        if p is not None:
            self.open_probs[key] = float(p)


    # ========== 修正版 record_close ==========
    def record_close(self, which, symbol, pnl_cash: float, ret_pct_on_notional: float | None = None):
        # --- 正規化來源與商品代號 ---
        W = (which or "").upper()
        if W == "POS1":
            Wn = "LIVE"
        elif W == "POS2":
            Wn = "SIM"
        else:
            Wn = W
        S = (symbol or "").upper()

        # ★ 非白名單來源 → 直接不訓練、不寫 recent、不落地
        if not self._ok(Wn):
            return

        key = (Wn, S)
        x = self.open_feats.pop(key, None)
        p = self.open_probs.pop(key, None) if hasattr(self, "open_probs") else None

        # 沒開倉特徵就僅記錄，不訓練
        if x is None:
            try:
                add_log(f"ML skip train (no open_feat): {Wn} {S}", "dim")
                self.recent.append((0.5, 1 if pnl_cash > 0 else 0))
            except Exception:
                pass
            return

        # —— R-like 標籤 ——（保留原演算法）
        try:
            if ret_pct_on_notional is not None:
                move_pct = float(ret_pct_on_notional)
                r_like = move_pct / 0.0012
            else:
                r_like = 1.0 if pnl_cash > 0 else -1.0
        except Exception:
            r_like = 1.0 if pnl_cash > 0 else -1.0

        if r_like >= 1.0:
            y = 1
        elif r_like <= -1.0:
            y = 0
        else:
            y = 1 if pnl_cash > 0 else 0

        # 統計（保留）
        if y == 1:
            self.pos_seen += 1
        else:
            self.neg_seen += 1

        # —— 是否允許訓練 ——：
        # 1) 若關閉 AI（AI_ENABLE=0）→ 只記 recent/CSV，不做 partial_fit
        # 2) 若樣本未達門檻（self.train_after_seen）→ 同樣不訓練（你原本就有）
        can_train = AI_ENABLE and ((self.pos_seen + self.neg_seen) >= self.train_after_seen)

        # recent（保留）
        if p is not None:
            self.recent.append((float(p), y))

        # 訓練（僅在 can_train=True）
        if can_train:
            total = max(1, self.pos_seen + self.neg_seen)
            pos_ratio = self.pos_seen / total
            neg_ratio = self.neg_seen / total
            w = 1.0
            if y == 1 and pos_ratio < 0.35:
                w = min(3.0, 0.35 / max(1e-6, pos_ratio))
            if y == 0 and neg_ratio < 0.35:
                w = min(3.0, 0.35 / max(1e-6, neg_ratio))
            if y == 1:
                w *= 2.0
            for _ in range(3):
                self.model.partial_fit(x, y, sample_weight=w)
        else:
            try:
                add_log(f"ML skip partial_fit (AI_ENABLE={int(AI_ENABLE)}, seen={self.pos_seen + self.neg_seen}/{self.train_after_seen})", "dim")
            except Exception:
                pass

        # CSV（保留）
        try:
            import csv, time
            with open("ml_samples.csv", "a", newline="", encoding="utf-8") as f:
                wcsv = csv.writer(f)
                wcsv.writerow([
                    int(time.time() * 1000), Wn, S,
                    *[round(float(v), 6) for v in x],
                    y, round(float(p or -1), 6),
                    "R_label", round(float(r_like), 6)
                ])
        except Exception:
            pass

        return

    # ========== 儲存與載入 ==========
    def save(self, path="ml_state.json"):
        try:
            import json
            state = {
                "w": self.model.w,
                "threshold": self.threshold,
                "n_seen": self.model.n_seen,
                "pos_seen": self.pos_seen,
                "neg_seen": self.neg_seen
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass

    def load(self, path="ml_state.json"):
        try:
            import json, os
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
            self.model.w = s.get("w", self.model.w)
            self.threshold = float(s.get("threshold", self.threshold))
            self.model.n_seen = int(s.get("n_seen", self.model.n_seen))
            self.pos_seen = int(s.get("pos_seen", self.pos_seen))
            self.neg_seen = int(s.get("neg_seen", self.neg_seen))
        except Exception:
            pass
ML_TRAIN_SOURCES = {"LIVE", "SIM"}

# >>> ML 初始化與啟動訊息（強化）
ML = MLManager(threshold=ML_THRESHOLD, train_sources=ML_TRAIN_SOURCES)
ML.load("ml_state.json")
if "ML_THRESHOLD" in os.environ:
    ML.threshold = ML_THRESHOLD

try:
    add_log(
        f"ML init -> th={ML.threshold:.2f}, model_seen={ML.model.n_seen}, "
        f"pos/neg={ML.pos_seen}/{ML.neg_seen}, "
        f"train_after={ML.train_after_seen}, filter_after={ML.filter_after_seen}, "
        f"action_min={AI_MIN_SEEN_FOR_ACTION}, AI_ENABLE={int(AI_ENABLE)}, "
        f"AUTO_ADJ={int(ML_AUTO_ADJUST)}",
        "dim"
    )
except Exception:
    pass

# ===================== 狀態 =====================
class SymbolState:
    def __init__(self, symbol):
        self.symbol = symbol
        self.candles = deque(maxlen=270)
        self.ema_fast = None
        self.ema_slow = None
        self.bb_mid = None
        self.bb_up = None
        self.bb_dn = None
        self.vwap = None
        self.last_price = None
        self.last_signal_ts = 0
        self.cooldown_sec = 30
        # 分開記錄 LIVE/SIM
        self.position_live = None   # {"side","qty","entry","sl","tp","trail":...}
        self.position_sim  = None
        # 統計（共用顯示）
        self.win = 0; self.loss = 0; self.pnl_sum = 0.0
        self.candles_5m = deque(maxlen=270)
        self._cur_5m_bucket = None  # (start_ts, open, high, low, close, volume)
        self.ml_p = None        # 0~1
        self.ml_p_ts = 0        # 毫秒

    def _flush_5m_bar(self):
        if self._cur_5m_bucket:
            st, o, h, l, c, v = self._cur_5m_bucket
            self.candles_5m.append({
                "open": o, "high": h, "low": l, "close": c,
                "volume": v, "typical": (h+l+c)/3, "ts": st + 5*60*1000 - 1
            })
            self._cur_5m_bucket = None

    def ingest_1m_to_5m(self, bar_1m: dict):
        """把 1m bar 聚合成 5m bar；bar_1m = {'ts','open','high','low','close','volume','typical'}"""
        ts = int(bar_1m["ts"])
        bucket_start = ts - (ts % (5*60*1000))
        o = float(bar_1m["open"]); h = float(bar_1m["high"]); l = float(bar_1m["low"]); c = float(bar_1m["close"]); v = float(bar_1m["volume"])
        if self._cur_5m_bucket is None:
            self._cur_5m_bucket = [bucket_start, o, h, l, c, v]
        else:
            st, o0, h0, l0, c0, v0 = self._cur_5m_bucket
            if bucket_start != st:
                # 新5分鐘開始 → 先沖掉上一根
                self._flush_5m_bar()
                self._cur_5m_bucket = [bucket_start, o, h, l, c, v]
            else:
                # 同一5分鐘內 → 更新高低收/量
                self._cur_5m_bucket[2] = max(h0, h)
                self._cur_5m_bucket[3] = min(l0, l)
                self._cur_5m_bucket[4] = c
                self._cur_5m_bucket[5] = v0 + v
                
    def update_indicators(self):
        need = max(EMA_SLOW, BB_LEN, MACD_SLOW)+5
        if len(self.candles) < need:
            if self.candles:
                self.last_price = self.candles[-1]["close"]
            return
        closes = [c["close"] for c in self.candles]
        self.ema_fast = None
        self.ema_slow = None
        for px in closes[-(EMA_SLOW+5):]:
            self.ema_fast = ema(self.ema_fast, px, EMA_FAST)
            self.ema_slow = ema(self.ema_slow, px, EMA_SLOW)
        mid = sma(closes[-BB_LEN:])
        sd  = std(closes[-BB_LEN:])
        self.bb_mid = mid
        self.bb_up  = mid + BB_STD*sd
        self.bb_dn  = mid - BB_STD*sd
        # VWAP 視窗
        if INTERVAL.endswith("h"):
            window = 24
        elif INTERVAL.endswith("m"):
            m = int(INTERVAL[:-1])
            window = max(96//m, 96)
        else:
            window = 96
        pool = list(self.candles)[-window:]
        self.vwap = vwap_calc(pool)
        self.last_price = self.candles[-1]["close"]

    def bb_width(self):
        if not self.bb_mid or not self.bb_up or not self.bb_dn: return None
        return (self.bb_up - self.bb_dn) / (2*self.bb_mid)
def regime_on_5m(st: SymbolState):
    """回傳 'RANGE' / 'UP' / 'DOWN'，依 5m 的 EMA / 布林寬 / 近窗報酬。"""
    arr = list(st.candles_5m)
    if len(arr) < max(EMA_SLOW, BB_LEN)+5:
        return "RANGE"
    closes = [c["close"] for c in arr]
    # 簡版EMA
    ef = es = None
    for px in closes[-(EMA_SLOW+5):]:
        ef = ema(ef, px, EMA_FAST)
        es = ema(es, px, EMA_SLOW)
    mid = sma(closes[-BB_LEN:])
    sd  = std(closes[-BB_LEN:])
    bbw = ((mid + BB_STD*sd) - (mid - BB_STD*sd)) / (2*mid) if (mid and sd) else 0.0
    ret = (closes[-1] - closes[-BB_LEN]) / closes[-BB_LEN] if len(closes) >= BB_LEN+1 else 0.0

    # 👇 門檻可用你前面 GRID 的 TREND_RET_TH / RANGE_MIN_BB_W
    if ef and es and ef > es and (ret > 0.004 or bbw > 0.010):
        return "UP"
    if ef and es and ef < es and (ret < -0.004 or bbw > 0.010):
        return "DOWN"
    # 否則視為 RANGE
    return "RANGE"
# 初始化幣池
SYMBOLS = []
SYMAP = {}

# ===================== 輔助：取/設部位 =====================
def _get_pos(st: SymbolState, which: str):
    return st.position_live if which.upper()=="LIVE" else st.position_sim

def _set_pos(st: SymbolState, which: str, pos):
    if which.upper()=="LIVE":
        st.position_live = pos
    else:
        st.position_sim = pos

# ===================== 交易輔助 =====================
def can_trade_now(which: str):
    acc = get_account(which)
    if acc.daily_pnl >= (acc.daily_start_equity or acc.balance)*DAILY_TARGET_PCT:
        return False, "Daily target hit"
    if acc.daily_pnl <= -(acc.daily_start_equity or acc.balance)*DAILY_MAX_LOSS_PCT:
        return False, "Daily max loss hit"
    return True, ""

def pos_count_active(which: str):
    cnt = 0
    for s in SYMBOLS:
        st = SYMAP[s]
        if _get_pos(st, which):
            cnt += 1
    return cnt

# ===== PATCH C2: compute_qty 支援覆寫 risk_pct =====
def compute_qty(entry, sl, which: str, risk_pct_override: float | None = None):
    acc = get_account(which)
    entry = float(entry or 0.0)
    sl    = float(sl or 0.0)
    if entry <= 0:
        return 0.0

    # 可用保證金推估
    if which.upper() == "LIVE" and ACCOUNT_LIVE._available is not None:
        avail_margin = float(ACCOUNT_LIVE._available)
    else:
        avail_margin = float(acc.balance)

    max_notional_by_margin = max(0.0, avail_margin) * max(LEVERAGE, 0.0) * 0.95

    if POSITION_SIZING.upper() == "ALLOC":
        alloc_margin = acc.balance * max(ALLOC_PCT, 0.0) / 100.0
        target_notional = alloc_margin * max(LEVERAGE, 0.0)
        notional = min(target_notional, max_notional_by_margin)
        return max(notional / entry, 0.0)

    # === RISK 模式 ===
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.0
    # 使用覆寫風險％（若無則回退到全域基準）
    risk_pct = RISK_PER_TRADE_PCT if risk_pct_override is None else float(risk_pct_override)
    risk_cap = acc.balance * max(risk_pct, 0.0) / 100.0

    notional_risk = risk_cap * entry / dist
    notional = min(notional_risk, max_notional_by_margin)
    return max(notional / entry, 0.0)

import uuid

def _local_open(st, which, side, qty, entry, sl, tp, ml_features=None):
    _set_pos(st, which, {
        "side": side,
        "qty": qty,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "trail": None,
        "ml_feat": ml_features,
        "opened_ts": utc_ms(),
        "risk_R": abs(float(entry) - float(sl)) if sl is not None else None,
        "pos_uid": f"{which}:{st.symbol}:{utc_ms()}:{uuid.uuid4().hex[:8]}"   # ← 新增
    })
    console.print(f"[yellow]{which} LOCAL order {st.symbol} {side} qty={qty} entry={entry}[/yellow]")

    # 更新 opened_ts（保留，但不再覆蓋 ml_feat）
    pos = _get_pos(st, which)
    if pos and isinstance(pos, dict):
        pos["opened_ts"] = utc_ms()

    # 🟢 立刻存檔（只在 SIM）
    try:
        if which.upper() == "SIM":
            persist_sim_state(force=True)
    except Exception:
        pass

    return {"orderId": int(time.time() * 1000), "price": entry, "origQty": qty}, "local"

def _live_open_and_exit_orders(symbol, side, qty, entry, sl, tp):
    ensure_isolated_and_leverage([symbol], LEVERAGE)
    ord_side = "BUY" if side=="LONG" else "SELL"
    res_open = binance_signed("POST", "/fapi/v1/order", {
        "symbol": symbol, "side": ord_side, "type": "MARKET",
        "quantity": qty, "recvWindow": 5000, "newOrderRespType": "RESULT",
    })
    rules = _get_symbol_rules(symbol)
    sl_s = _fmt_to_tick(sl, rules["tickSize"])
    tp_s = _fmt_to_tick(tp, rules["tickSize"])

    if EXCHANGE_MANAGE_EXIT:
        def _place_exits_later():
            try:
                time.sleep(2.0)  # 新倉保護 2 秒
                binance_signed("POST", "/fapi/v1/order", {
                    "symbol": symbol, "side": ("SELL" if side=="LONG" else "BUY"),
                    "type": "STOP_MARKET", "stopPrice": sl_s,
                    "closePosition": "true", "workingType": "CONTRACT_PRICE",
                    "priceProtect": "true", "recvWindow": 5000,
                })
                binance_signed("POST", "/fapi/v1/order", {
                    "symbol": symbol, "side": ("SELL" if side=="LONG" else "BUY"),
                    "type": "TAKE_PROFIT_MARKET", "stopPrice": tp_s,
                    "closePosition": "true", "workingType": "CONTRACT_PRICE",
                    "priceProtect": "true", "recvWindow": 5000,
                })
            except Exception as e:
                console.print(f"[red]place_exits_later error {symbol}: {e}[/red]")

        threading.Thread(target=_place_exits_later, daemon=True).start()

    return res_open

def _apply_min_gap(entry: float, price: float, tick: float, is_above: bool) -> float:
    """
    先在連續值空間把 price 與 entry 拉出最小距離。
    is_above=True  表示 price 應該 > entry（例如 LONG 的 TP、SHORT 的 SL）
    is_above=False 表示 price 應該 < entry（例如 LONG 的 SL、SHORT 的 TP）
    """
    # 至少 5 個 tick 或 0.05% 價差（可視需求調整）
    min_gap = max(tick * 12.0, entry * 0.0015)  # 至少 12 tick 或 0.15%

    if is_above:
        if price <= entry + min_gap:
            price = entry + min_gap
    else:
        if price >= entry - min_gap:
            price = entry - min_gap
    return price


def _reaffirm_after_tick(entry: float, price: float, tick: float, is_above: bool) -> float:
    min_gap = max(tick * 12.0, entry * 0.0015)
    if is_above:
        if price - entry < min_gap:
            price = entry + min_gap
        # 目標是位於 entry 上方 → 用向上靠 tick
        return _round_price_to_tick(price, tick, +1)
    else:
        if entry - price < min_gap:
            price = entry - min_gap
        # 目標是位於 entry 下方 → 用向下靠 tick
        return _round_price_to_tick(price, tick, -1)
# ===== PATCH D3: Simple SIM trailing stop =====
TRAIL_ENABLE = True
TRAIL_R_MULT_TRIGGER = 1.0   # 浮盈達 1R 先保本
TRAIL_STEP_ATR       = 0.75  # 之後用 0.75 ATR 追蹤

def _update_sim_trailing(st: SymbolState, which: str):
    if which.upper() != "SIM" or not TRAIL_ENABLE:
        return
    p = _get_pos(st, which)
    if not p or st.last_price is None:
        return

    entry = float(p["entry"])
    side  = p["side"]
    cur_sl = float(p.get("sl") or 0.0)
    if cur_sl == 0.0:
        return

    atr_val = atr_wilder(list(st.candles), n=14)
    if not atr_val or atr_val <= 0:
        return

    # 初始風險 R
    R = abs(entry - cur_sl)
    if R <= 0:
        return

    px = float(st.last_price)
    sign = 1 if side == "LONG" else -1
    move = (px - entry) * sign

    # 預設維持不變
    new_sl = cur_sl

    # 浮盈 >= 1R → 先保本；> 2R → 再用 ATR 追蹤
    if move >= TRAIL_R_MULT_TRIGGER * R:
        if side == "LONG":
            new_sl = max(cur_sl, entry)  # 先推到保本
            if move >= 2 * R:
                new_sl = max(new_sl, px - TRAIL_STEP_ATR * atr_val)
        else:
            new_sl = min(cur_sl, entry)
            if move >= 2 * R:
                new_sl = min(new_sl, px + TRAIL_STEP_ATR * atr_val)

    # —— 只收緊不放鬆（LONG 只能上移；SHORT 只能下移）——
    if side == "LONG" and new_sl <= cur_sl:
        return
    if side == "SHORT" and new_sl >= cur_sl:
        return

    # —— 對齊 tick，並與現價保持最小距離（>= 1 tick）——
    try:
        rules = _get_symbol_rules(st.symbol)
        tick  = float(rules["tickSize"])
    except Exception:
        tick = 0.0

    if tick > 0:
        if side == "LONG":
            # SL 應位於現價下方：向下靠 tick，再確保 < 現價 - 1tick
            new_sl = _round_price_to_tick(new_sl, tick, -1)
            if new_sl >= px - tick:
                new_sl = px - tick
                new_sl = _round_price_to_tick(new_sl, tick, -1)
        else:
            # SL 應位於現價上方：向上靠 tick，再確保 > 現價 + 1tick
            new_sl = _round_price_to_tick(new_sl, tick, +1)
            if new_sl <= px + tick:
                new_sl = px + tick
                new_sl = _round_price_to_tick(new_sl, tick, +1)

    # 再次確保仍然是收緊
    if (side == "LONG" and new_sl <= cur_sl) or (side == "SHORT" and new_sl >= cur_sl):
        return

    p["sl"] = float(new_sl)
    try:
        add_log(f"SIM trail {st.symbol} {side}: SL {cur_sl:.6f} -> {new_sl:.6f}", "dim")
    except Exception:
        pass
        
def _check_tp_sl_and_close_if_hit(st: SymbolState, which: str):
    """檢查部位是否觸發 TP/SL；僅 SIM 需要，LIVE 由交易所處理。"""
    p = _get_pos(st, which)
    if not p or st.last_price is None:
        return
    side = p["side"]; entry = float(p["entry"])
    tp = float(p.get("tp") or 0); sl = float(p.get("sl") or 0)
    px = float(st.last_price)
    # ---- Quick-train by R-multiple（推薦）----
    try:
        now_ms = utc_ms()
        last_qt = p.get("ml_quick_train_ts") or 0
        if now_ms - last_qt >= 30_000:  # 去抖：30 秒一次
            R = float(p.get("risk_R") or 0.0)
            if R > 0:
                sign = 1 if side == "LONG" else -1
                move = (px - entry) * sign         # 朝有利方向為正
                r_mult = move / R                   # 幾個 R

                if abs(r_mult) >= 1.0:             # |1R| 即觸發快速學習（可調）
                    # 取開倉特徵 → fallback
                    x = ML.peek_open_sample("SIM", st.symbol)
                    if x is None:
                        x = p.get("ml_feat") or _features_from_state(st) or _snapshot_indicators(st)
                        if isinstance(x, dict):
                            x = _features_from_state(st) or [0.0]*8

                    if x is not None:
                        # 標籤：>=+1.5R 當正樣本；<=-1R 當負樣本；中間用 move 正負
                        if r_mult >= 1.5:
                            y = 1
                        elif r_mult <= -1.0:
                            y = 0
                        else:
                            y = 1 if move > 0 else 0

                        # 強化學習：重複 2~3 次，或調高 sample_weight
                        for _ in range(3):
                            ML.model.partial_fit(x, y, sample_weight=1.5)

                        p["ml_quick_train_ts"] = now_ms
                        add_log(f"ML quick-train SIM {st.symbol} r={r_mult:.2f}", "dim")
    except Exception:
        pass
    hit = None
    if side == "LONG":
        if sl and px <= sl: hit = ("SL", sl)
        elif tp and px >= tp: hit = ("TP", tp)
    else:  # SHORT
        if sl and px >= sl: hit = ("SL", sl)
        elif tp and px <= tp: hit = ("TP", tp)

    if hit:
        tag, ref = hit
        # 用最新價 px 結算較合理；ref 只做資訊
        close_position_one(st.symbol, f"{which} {tag} hit@{ref}", px, which)

def pre_trade_gate(symbol: str, side: str, entry: float, sl: float, tp: float, which: str):
    """
    針對下單前的合規：tick 對齊、步長、minQty、minNotional、可用保證金上限。
    通過回傳 (True, None, (entry, sl, tp))
    不通過回傳 (False, reason, None)
    """
    try:
        rules = _get_symbol_rules(symbol)
        tick  = float(rules["tickSize"]); step = float(rules["stepSize"])
        min_qty = float(rules["minQty"]); min_notional = float(rules.get("minNotional") or 0.0)
        # 1) 價格與 tick 的最小距離與對齊（沿用你既有的規則）
        if side == "LONG":
            if not (tp > entry > sl):
                return False, f"price order invalid (LONG) tp>{entry}>sl not satisfied", None
        else:
            if not (tp < entry < sl):
                return False, f"price order invalid (SHORT) tp<{entry}<sl not satisfied", None
        # 2) 可用保證金估算（LIVE 優先用 _available）
        acc = get_account(which)
        if which.upper()=="LIVE" and ACCOUNT_LIVE._available is not None:
            avail_margin = float(ACCOUNT_LIVE._available)
        else:
            avail_margin = float(acc.balance)
        if avail_margin <= 0:
            return False, "no available margin", None

        # 3) 依據最終 SL 計算數量（先用你的 compute_qty，但暫時不落單）
        #    在外層仍會用 compute_qty 再計一次；這裡只做 gate 驗證
        qty_preview = compute_qty(entry, sl, which)
        qty_preview = _floor_to_step(qty_preview, step)
        if qty_preview <= 0:
            return False, "qty_preview=0 (risk too tight or no margin)", None
        if qty_preview < min_qty:
            return False, f"qty {qty_preview} < minQty {min_qty}", None
        if min_notional and qty_preview * float(entry) < min_notional:
            return False, f"notional {qty_preview*float(entry):.4f} < minNotional {min_notional}", None

        # 4) 對齊 tp/sl 到 tick（不改 entry；entry 為市價成交近似）
        tp_aligned = _round_price_to_tick(tp, tick, (+1 if side=="LONG" else -1))
        sl_aligned = _round_price_to_tick(sl, tick, (-1 if side=="LONG" else +1))
        return True, None, (entry, sl_aligned, tp_aligned)
    except Exception as e:
        return False, f"pre_trade_gate error: {e}", None
        
# ===== PATCH C3 (final): p -> dynamic risk, and ALWAYS record_open with fallback =====
def place_order_one(symbol, side, entry, sl, tp, which: str, ml_features=None, ml_p: float | None = None):
    st = SYMAP[symbol]
    rules = _get_symbol_rules(symbol)

    # === 決策級 AI 門檻（僅影響過濾與動態風險，資料照記） ===
    try:
        total_seen = ML.pos_seen + ML.neg_seen
        use_ai_decision = (AI_ENABLE and (ML.model.n_seen >= 30) and (total_seen >= AI_MIN_SEEN_FOR_ACTION))
    except Exception:
        use_ai_decision = False
    if not use_ai_decision:
        ml_p = None  # 關閉 p 過濾與動態風險調整

    # === 下單合規守門員 ===
    ok, why, adj = pre_trade_gate(symbol, side, entry, sl, tp, which)
    if not ok:
        log_open_reject(symbol, which, "gate_block", why=why)
        return None, f"{which}: gate_block: {why}"
    entry, sl, tp = adj

    # ---- 以 ML 機率 p 轉風險％（僅在 use_ai_decision=True 且有 ml_p 時）----
    per_trade_risk = RISK_PER_TRADE_PCT
    if (ml_p is not None) and use_ai_decision:
        th = ML.threshold
        if ml_p <= th:
            log_open_reject(
                symbol, which, "ml_threshold",
                p=f"{ml_p:.4f}", th=f"{th:.4f}",
                n_seen=ML.model.n_seen, pos=ML.pos_seen, neg=ML.neg_seen
            )
            return None, f"{which}: filtered_by_p ({ml_p:.4f} <= {th:.4f})"
        gain = (ml_p - th) / max(1e-9, (1.0 - th))
        per_trade_risk = RISK_PER_TRADE_PCT * (1.0 + min(1.0, 1.5 * gain))
    elif not use_ai_decision:
        # 明確記錄 AI 為何沒介入（關閉或資料不足）
        log_open_reject(
            symbol, which, "ai_bypass",
            AI_ENABLE=int(AI_ENABLE),
            model_seen=ML.model.n_seen,
            seen_total=ML.pos_seen + ML.neg_seen,
            need=AI_MIN_SEEN_FOR_ACTION
        )

    # === 以 tick + 最小距離重新對齊 SL/TP ===
    if side == "LONG":
        tp = _apply_min_gap(entry, tp, rules["tickSize"], is_above=True)
        sl = _apply_min_gap(entry, sl, rules["tickSize"], is_above=False)
        tp = _round_price_to_tick(_reaffirm_after_tick(entry, tp, rules["tickSize"], True), rules["tickSize"], +1)
        sl = _round_price_to_tick(_reaffirm_after_tick(entry, sl, rules["tickSize"], False), rules["tickSize"], -1)
        if not (tp > entry > sl):
            tp = _round_price_to_tick(max(tp, entry + rules["tickSize"]), rules["tickSize"], +1)
            sl = _round_price_to_tick(min(sl, entry - rules["tickSize"]), rules["tickSize"], -1)
    else:
        tp = _apply_min_gap(entry, tp, rules["tickSize"], is_above=False)
        sl = _apply_min_gap(entry, sl, rules["tickSize"], is_above=True)
        tp = _round_price_to_tick(_reaffirm_after_tick(entry, tp, rules["tickSize"], False), rules["tickSize"], -1)
        sl = _round_price_to_tick(_reaffirm_after_tick(entry, sl, rules["tickSize"], True), rules["tickSize"], +1)
        if not (tp < entry < sl):
            tp = _round_price_to_tick(min(tp, entry - rules["tickSize"]), rules["tickSize"], -1)
            sl = _round_price_to_tick(max(sl, entry + rules["tickSize"]), rules["tickSize"], +1)

    # === 用「最終」SL 再計數量，套用 per-trade 風險％ ===
    qty = compute_qty(entry, sl, which, risk_pct_override=per_trade_risk)
    qty = _floor_to_step(qty, rules["stepSize"])

    # === 準備 record_open 的特徵（保留你的 fallback） ===
    def _fallback_feat_from_snapshot():
        try:
            snap = _snapshot_indicators(st) or {}
            rsi = snap.get("rsi", 50.0) or 50.0
            macd_hist = snap.get("macd_hist", 0.0) or 0.0
            ema_gap = 0.0
            if st.ema_fast and st.ema_slow and st.last_price:
                ema_gap = (st.ema_fast - st.ema_slow) / st.last_price
            bbw = snap.get("bb_width") or 0.0
            vwap_dev = 0.0
            if st.vwap and st.last_price:
                vwap_dev = (st.last_price - st.vwap) / st.last_price
            feat = [
                max(0, min(1, rsi/100.0)),
                max(-1, min(1, macd_hist)),
                max(-0.05, min(0.05, ema_gap)),
                max(0, min(0.05, bbw)),
                max(-0.05, min(0.05, vwap_dev)),
                0.0,  # vol_z
                0.0,  # atr_rel
                0.0,  # ema_slope
            ]
            return feat
        except Exception:
            return None

    try:
        feat_for_open = ml_features if ml_features is not None else _features_from_state(st)
        if feat_for_open is None:
            feat_for_open = _fallback_feat_from_snapshot()
    except Exception:
        feat_for_open = _fallback_feat_from_snapshot()

    if feat_for_open is None:
        feat_for_open = [0.0] * 8
    elif len(feat_for_open) != 8:
        try:
            feat_for_open = (list(feat_for_open) + [0.0]*8)[:8]
        except Exception:
            feat_for_open = [0.0] * 8

    # === 模擬 / 無金鑰 ===
    if which.upper() == "SIM" or (which.upper() == "LIVE" and (not API_KEY or not API_SECRET)):
        res = _local_open(st, which, side, qty, entry, sl, tp, ml_features=feat_for_open)
        try:
            ML.record_open(which, st.symbol, feat_for_open, p=ml_p)
        except Exception as e:
            console.print(f"[dim]ML.record_open skip: {e}[/dim]")
        return res

    # === LIVE 下單 ===（保留原流程）
    try:
        if which.upper() == "LIVE":
            cancel_all_open_orders(symbol)
            cleanup_orders(symbol)

        res_open = _live_open_and_exit_orders(symbol, side, qty, entry, sl, tp)
        _set_pos(st, "LIVE", {
            "side": side,
            "qty": float(qty),
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "trail": None,
            "ml_feat": feat_for_open,
            "risk_R": abs(float(entry) - float(sl)),
            "pos_uid": f"LIVE:{st.symbol}:{utc_ms()}:{uuid.uuid4().hex[:8]}"
        })
        console.print(f"[green]LIVE order {symbol} {side} qty={qty} entry≈{entry}（已掛 SL/TP）[/green]")
        try:
            ML.record_open(which, st.symbol, feat_for_open, p=ml_p)
        except Exception as e:
            console.print(f"[dim]ML.record_open skip: {e}[/dim]")
        return res_open, "live"

    except requests.HTTPError as e:
        try:
            err_json = e.response.json()
            console.print(f"[red]LIVE order error {symbol}: {err_json}[/red]")
        except Exception:
            console.print(f"[red]LIVE order error {symbol}: {e}[/red]")
        return None, f"order error {which}: {e}"
    except Exception as e:
        console.print(f"[red]LIVE order error {symbol}: {e}[/red]")
        return None, f"order error {which}: {e}"
        
def place_order(symbol, side, entry, sl, tp, ml_features=None, ml_p: float | None = None):
    mode = EXECUTION_MODE.upper()
    res_live = res_sim = None

    # === 原子性名額檢查（含「開倉中」） ===
    with ORDER_LOCK:
        try:
            # 先同步，降低舊狀態造成的誤判
            sync_live_positions_periodic()
        except Exception:
            pass

        if ONE_POS_PER_SYMBOL and symbol_is_blocked(symbol, SHOW_ACCOUNT):
            log_open_reject(symbol, SHOW_ACCOUNT, "blocked", note="has_position_or_inflight")
            console.print(f"[yellow]Skip {symbol}: already has position or inflight ({SHOW_ACCOUNT})[/yellow]")
            return (None, "blocked"), (None, "blocked")

        if MAX_CONCURRENT_POS and pos_count_including_inflight(SHOW_ACCOUNT) >= MAX_CONCURRENT_POS:
            log_open_reject(symbol, SHOW_ACCOUNT, "max_reached", max=MAX_CONCURRENT_POS)
            console.print(f"[yellow]Skip {symbol}: max concurrent positions reached ({SHOW_ACCOUNT})[/yellow]")
            return (None, "max_reached"), (None, "max_reached")

        # 通過檢查 → 佔位（以 LIVE 為主）
        if mode in ("LIVE", "BOTH"):
            OPEN_INFLIGHT.add(symbol)
    # === 新增：取用右側面板的最新信心值 ===
    p = None
    st = SYMAP.get(symbol)
    if st and getattr(st, "ml_p", None) is not None and (utc_ms() - getattr(st, "ml_p_ts", 0) <= 90_000):
        p = float(st.ml_p)
    ml_p = p
    try:
        if mode in ("LIVE", "BOTH"):
            res_live = place_order_one(symbol, side, entry, sl, tp, "LIVE",
                                       ml_features=ml_features, ml_p=ml_p)
        if mode in ("SIM", "BOTH"):
            res_sim = place_order_one(symbol, side, entry, sl, tp, "SIM",
                                      ml_features=ml_features, ml_p=ml_p)
        return res_live, res_sim
    finally:
        # 若 LIVE 端沒有成功建立本地倉，就釋放 inflight
        if mode in ("LIVE", "BOTH"):
            st = SYMAP.get(symbol)
            live_ok = st and (st.position_live is not None)
            if not live_ok:
                with ORDER_LOCK:
                    OPEN_INFLIGHT.discard(symbol)
        # （已移除：清除 globals 的程式碼）
def _ticks_between(a: float, b: float, tick: float) -> float:
    return abs(a - b) / max(tick, 1e-12)

def _live_replace_exits(symbol: str, side: str, new_sl: float, new_tp: float):
    """
    LIVE: 以 closePosition=true 方式重掛新 SL/TP（止損/停利市價單）。
    先一鍵取消，再補兩張；沿用你現有 workingType/priceProtect。
    """
    if TESTNET or not API_KEY or not API_SECRET:
        return
    try:
        # 取消舊單
        cancel_all_open_orders(symbol)
        # 補新單（方向相反）
        opp = "SELL" if side == "LONG" else "BUY"
        binance_signed("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": opp, "type": "STOP_MARKET",
            "stopPrice": new_sl, "closePosition": "true",
            "workingType": "CONTRACT_PRICE", "priceProtect": "true",
            "recvWindow": 5000,
        })
        binance_signed("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": opp, "type": "TAKE_PROFIT_MARKET",
            "stopPrice": new_tp, "closePosition": "true",
            "workingType": "CONTRACT_PRICE", "priceProtect": "true",
            "recvWindow": 5000,
        })
        add_log(f"LIVE exits reset {symbol} SL->{new_sl} TP->{new_tp}", "yellow")
    except Exception as e:
        add_log(f"live replace exits fail {symbol}: {e}", "red")


def dynamic_exit_manager_once():
    """
    主迴圈/worker 每次呼叫一輪。
    規則：
      - 依 ATR+regime 產生『目標』TP/SL
      - SL 只收緊不放鬆（可關掉）
      - 變動 tick 達門檻才真正改單（避免洗單）
      - 同一筆倉位至少 DYN_MIN_SEC_BETWEEN_ADJ 秒才調一次
    """
    if not DYN_EXIT_ON:
        return

    now_ms = utc_ms()
    for sym, st in list(SYMAP.items()):
        for which in ("LIVE", "SIM"):
            p = _get_pos(st, which)
            if not p or st.last_price is None:
                continue

            key = (which, sym)
            last = _LAST_DYN_ADJ_MS.get(key, 0)
            if now_ms - last < int(DYN_MIN_SEC_BETWEEN_ADJ * 1000):
                continue

            # 1) 取得目標值
            tgt = _dyn_target_tp_sl(st, which)
            if not tgt:
                continue
            tp_tgt, sl_tgt = tgt

            # 2) 取當前值
            cur_sl = float(p.get("sl") or 0.0)
            cur_tp = float(p.get("tp") or 0.0)
            side   = p["side"]

            # 3) 僅允許 SL 收緊
            sl_new = _enforce_tighten_only(cur_sl, sl_tgt, side)
            tp_new = tp_tgt  # TP 允許雙向微調（趨勢放大、盤整拉近）

            # 4) tick 門檻與對齊
            try:
                rules = _get_symbol_rules(sym)
                tick  = float(rules["tickSize"])
            except Exception:
                tick = 0.0

            # 變動是否達門檻？
            sl_move_ok = (cur_sl == 0.0) or (_ticks_between(sl_new, cur_sl, tick) >= DYN_MIN_TICK_CHANGE_SL)
            tp_move_ok = (cur_tp == 0.0) or (_ticks_between(tp_new, cur_tp, tick) >= DYN_MIN_TICK_CHANGE_TP)

            if not sl_move_ok and not tp_move_ok:
                continue

            # 5) SIM：本地直接改；LIVE：取消重掛
            if which == "SIM":
                if sl_move_ok:
                    p["sl"] = float(sl_new)
                if tp_move_ok:
                    p["tp"] = float(tp_new)
                add_log(f"SIM exits reset {sym} {side} SL {cur_sl}→{p['sl']} TP {cur_tp}→{p['tp']}", "dim")
                # 追蹤仍保留你原本的 _update_sim_trailing（兩者可共存）
            else:
                # LIVE
                # 安全：再確保新 SL/TP 與 entry 的關係正確
                entry = float(p["entry"])
                if side == "LONG":
                    sl_new = min(sl_new, entry - max(tick, entry*0.0005))
                    tp_new = max(tp_new, entry + max(tick, entry*0.0005))
                else:
                    sl_new = max(sl_new, entry + max(tick, entry*0.0005))
                    tp_new = min(tp_new, entry - max(tick, entry*0.0005))

                # 對齊格式字串（你的 _fmt_to_tick 會依 tick 小數位數格式化）
                if tick > 0:
                    sl_new = _fmt_to_tick(sl_new, tick)
                    tp_new = _fmt_to_tick(tp_new, tick)

                try:
                    _live_replace_exits(sym, side, sl_new, tp_new)
                except Exception as e:
                    add_log(f"live reset exits error {sym}: {e}", "red")

            _LAST_DYN_ADJ_MS[key] = now_ms
            
def _append_trade_record(which, symbol, side, entry, exit_px, qty, pnl_cash, net_pct, reason, **extra):
    row = {
        "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol, "side": side,
        "entry": float(entry), "exit": float(exit_px), "qty": float(qty),
        "pnl_cash": float(pnl_cash), "net_pct": float(net_pct),
        "reason": str(reason),
    }
    row.update(extra or {})
    get_account(which).trades.append(row)
    return row
    
def close_position_one(symbol, reason, px, which: str, skip_exchange: bool=False):
    st = SYMAP[symbol]
    p = _get_pos(st, which)

    if _close_too_soon(which, symbol):
        add_log(f"debounce close {which} {symbol}", "dim")
        return
    if not p:
        return

    # ---- 去重 UID（確保第一次完整結算 + 訓練）----
    pos_uid = p.get("pos_uid") or f"{which}:{symbol}:{p.get('entry')}:{p.get('qty')}"
    with _TRAINED_LOCK:
        if pos_uid in _TRAINED_POS_UIDS:
            if abs(float(p.get("qty",0)))>0 and abs(float(p.get("entry",0)))>0:
                pos_uid = f"{which}:{symbol}:{p['entry']}:{p['qty']}:{utc_ms()}"
            else:
                add_log(f"dup close -> local cleanup {which} {symbol}", "yellow")
                _set_pos(st, which, None)
                return
        _TRAINED_POS_UIDS.add(pos_uid)

    # 先取用到的欄位
    entry = float(p["entry"])
    side  = p["side"]
    qty   = float(p["qty"])
    px    = float(px)

    # —— LIVE 先嘗試 reduceOnly 市價單（可跳過）——
    if which.upper() == "LIVE" and not skip_exchange:
        try:
            res = _live_reduce_only_market_close(symbol, side, qty)
            ap = float(res.get("avgPrice") or res.get("price") or 0.0)
            if ap > 0:
                px = ap
        except Exception as e:
            console.print(f"[red]LIVE market close failed {symbol}: {e}[/red]")
            add_log(f"LIVE close failed, continue local settle {symbol}", "yellow")

    # ---- 本地結算（資金、統計、CSV、訓練）----
    sign = 1 if side == "LONG" else -1
    gross_pnl = qty * (px - entry) * sign

    # === 手續費：LIVE 取 userTrades；SIM 用估值 ===
    fee_usdt = 0.0
    est_fee_usdt = (TAKER_FEE_PCT / 100.0) * (qty * entry * 2.0)
    try:
        if which.upper() == "LIVE":
            opened_ts = None
            try:
                opened_ts = (p.get("opened_ts") or
                             (st.position_live or {}).get("opened_ts") or
                             (st.position_sim or {}).get("opened_ts"))
            except Exception:
                opened_ts = None
            start_ms = int(opened_ts) - 5_000 if opened_ts else None
            end_ms   = utc_ms() + 5_000
            fee_usdt = fetch_commission_usdt(symbol, start_ms=start_ms, end_ms=end_ms, limit=80)
            if fee_usdt > 0:
                add_log(f"FEE {symbol} real commission applied: {fee_usdt:.6f} USDT", "dim")
            else:
                add_log(f"FEE {symbol} no real fee found in window, assume 0", "yellow")
        else:
            fee_usdt = est_fee_usdt
    except Exception as _e:
        add_log(f"apply real fee error {symbol}: {type(_e).__name__}: {_e}", "yellow")
        fee_usdt = 0.0 if which.upper()=="LIVE" else est_fee_usdt

    net_pnl = gross_pnl - float(fee_usdt)
    notional = qty * entry
    net_pct_on_notional = (net_pnl / notional * 100.0) if notional > 0 else 0.0

    acc = get_account(which)
    # ✅ 實現損益：兩邊都計入 total_pnl（唯一入口）
    acc.total_pnl += float(net_pnl)

    # ✅ 現金/日損益：只在 SIM 動帳；LIVE 交給 sync_live_balance() 以權益同步
    if which.upper() == "SIM":
        acc.balance += float(net_pnl)
        acc.daily_pnl += float(net_pnl)

    # ---- 寫入 trades（含 fee_usdt 與 pos_uid）----
    acc.trades.append({
        "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "side": side,
        "entry": round(entry, 6),
        "exit": round(px, 6),
        "qty": round(qty, 8),
        "pnl_cash": round(float(net_pnl), 2),
        "net_pct": round(net_pct_on_notional, 3),
        "fee_usdt": round(float(fee_usdt), 6),
        "risk_R": float(p.get("risk_R") or 0.0),
        "pos_uid": pos_uid,
        "reason": f"{reason} ({which})"
    })

    # ---- 即時統計顯示 ----
    try:
        wins = sum(1 for r in acc.trades if r["pnl_cash"] > 0)
        losses = sum(1 for r in acc.trades if r["pnl_cash"] <= 0)
        winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
        console.print(
            f"[bold cyan]{which}[/bold cyan] [{symbol}] 結算: "
            f"PnL={net_pnl:+.2f}  (淨%={net_pct_on_notional:+.3f}%) | "
            f"W/L={wins}/{losses}  Win%={winrate:.1f}%  | "
            f"Equity={acc.balance:,.2f}"
        )
    except Exception:
        pass

    # ---- CSV 落地（向下相容）----
    try:
        import csv, os
        if LOG_TRADES_ON:
            csv_path = TRADES_CSV_PATH
            dir_ = os.path.dirname(csv_path)
            if dir_:
                os.makedirs(dir_, exist_ok=True)

            row = dict(acc.trades[-1])
            row["account"] = which

            base_fields = ["ts","account","symbol","side","entry","exit","qty",
                           "pnl_cash","net_pct","fee_usdt","risk_R","pos_uid","reason"]

            with _CSV_LOCK:
                is_new = not os.path.exists(csv_path)
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=base_fields)
                    if is_new:
                        w.writeheader()
                    safe_row = {k: row.get(k, "") for k in base_fields}
                    w.writerow(safe_row)
                    f.flush()
                    os.fsync(f.fileno())

            add_log(f"trade saved -> {csv_path} [{which} {symbol} {row['pnl_cash']:+.2f}]", "dim")
        else:
            add_log("CSV logging disabled (LOG_TRADES_CSV)", "dim")
    except Exception as _e:
        add_log(f"CSV write error: {type(_e).__name__}: {_e}", "red")

    # ---- ML：若遺失開倉特徵，先補打一筆 record_open，再 record_close ----
    key = (which.upper(), st.symbol)
    missing = (key not in ML.open_feats) or (ML.open_feats.get(key) is None)
    if missing:
        feat = None
        if isinstance(p, dict):
            feat = p.get("ml_feat")
        if feat is None:
            try:
                feat = _features_from_state(st)
            except Exception:
                feat = None
        if feat is not None:
            try:
                ML.record_open(which, st.symbol, feat, p=None)
                console.print(f"[dim]ML open-feat recovered for {st.symbol} ({which})[/dim]")
            except Exception as e:
                console.print(f"[dim]ML open-feat recovery skipped: {e}[/dim]")

    # ---- 清倉、清除所有掛單 ----
    _set_pos(st, which, None)
    if which.upper() == "LIVE":
        with ORDER_LOCK:
            OPEN_INFLIGHT.discard(symbol)
        cancel_all_open_orders(symbol)
        cleanup_orders(symbol)

    # ---- ML 學習 ----
    try:
        # 傳 fraction（把百分比除以 100）
        ML.record_close(which, st.symbol, float(net_pnl), float(net_pct_on_notional) / 100.0)
        ML.save()
        console.print(f"[green]ML updated: n_seen={ML.model.n_seen} ({which})[/green]")
    except Exception as e:
        console.print(f"[dim]ML record_close skipped: {e}[/dim]")

    # === 背景補救：對帳殘留 ===
    try:
        if which.upper() == "LIVE":
            def _reconcile_and_force_close():
                try:
                    risks = binance_signed("GET", "/fapi/v2/positionRisk", {})
                    amt = 0.0
                    for r in risks:
                        if r.get("symbol") == symbol:
                            amt = float(r.get("positionAmt") or 0.0)
                            break
                    if abs(amt) > 0:
                        side_now = "LONG" if amt > 0 else "SHORT"
                        add_log(f"reconcile: found live leftover {symbol} {amt}, force close...", "yellow")
                        _live_reduce_only_market_close(symbol, side_now, abs(amt))
                        add_log(f"reconcile: force close done {symbol}", "dim")
                except Exception as ee:
                    add_log(f"reconcile force-close fail {symbol}: {ee}", "red")
            threading.Thread(target=_reconcile_and_force_close, daemon=True).start()
    except Exception:
        pass

    # ---- SIM 狀態落地保存 ----
    try:
        if which.upper() == "SIM":
            persist_sim_state(force=True)
    except Exception:
        pass

    # === 維護連虧 / 暫停 ===
    try:
        if net_pnl <= 0:
            _loss_streak_by_sym[symbol] = _loss_streak_by_sym.get(symbol, 0) + 1
            if _loss_streak_by_sym[symbol] >= LOSS_STREAK_LIMIT:
                _suspend_until_ts_by_sym[symbol] = time.time() + LOSS_SUSPEND_SECONDS
                add_log(f"{symbol} loss-streak reached -> suspend {LOSS_SUSPEND_SECONDS//3600}h", "yellow")
        else:
            _loss_streak_by_sym[symbol] = 0
    except Exception:
        pass
        
        
def close_position(symbol, reason, px):
    mode = EXECUTION_MODE.upper()
    if mode in ("LIVE","BOTH"): close_position_one(symbol, reason, px, "LIVE")
    if mode in ("SIM","BOTH"):  close_position_one(symbol, reason, px, "SIM")

# =========== ROI（即時） ==============
def current_roi_pct(st: SymbolState, which: str) -> float | None:
    p = _get_pos(st, which)
    if not p: return None
    entry = p["entry"]; side = p["side"]
    if st.last_price is not None:
        last = st.last_price
    elif st.candles:
        last = st.candles[-1]["close"]
    else:
        return None
    sign  = 1 if side=="LONG" else -1
    gross = (last - entry) / entry * sign * LEVERAGE
    fee   = (TAKER_FEE_PCT/100.0) * 2
    return (gross - fee) * 100.0
# ===========依『保證金百分比』回傳 tp / sl。=============
def tp_sl_by_margin(entry: float, side: str, tp_margin_pct: float, sl_margin_pct: float, lev: float):
    """
    依『保證金百分比』回傳 tp / sl。
    entry: 進場價
    side : "LONG" / "SHORT"
    tp_margin_pct, sl_margin_pct: 例如 0.10, 0.05
    lev  : 槓桿
    """
    # 需要的價格距離 = entry * (pct / lev)
    tp_dist = entry * (tp_margin_pct / lev)
    sl_dist = entry * (sl_margin_pct / lev)
    if side == "LONG":
        tp = entry + tp_dist
        sl = entry - sl_dist
    else:  # SHORT
        tp = entry - tp_dist
        sl = entry + sl_dist
    return tp, sl

# === Loss Streak Guard（連虧暫停）===
LOSS_STREAK_LIMIT       = 3        # 連虧幾筆啟動暫停
LOSS_SUSPEND_SECONDS    = 4 * 3600 # 當日暫停時長（4 小時）
_loss_streak_by_sym     = {}       # {symbol: int}
_suspend_until_ts_by_sym= {}       # {symbol: epoch_seconds}

# ===== 統一 TP/SL 計算（ATR 優先 + 基準底線） =====
USE_ATR_EXITS = False            # True=用 ATR；False=一律 10%/5% 基準
ATR_SL_K = 2.5                  # ATR 停損倍數
ATR_TP_K = 5                  # ATR 停利倍數
ATR_MIN_PCT = 0.004            # ATR 計不到或太小時，最低百分比（0.25%）

def _enforce_base_floor(entry: float, side: str, tp: float, sl: float, lev: float, tick: float):
    """
    把 ATR 算出的 tp/sl 拿來跟『10% 停利 / 5% 停損（以保證金％）』比較。
    - TP 至少 >= 10%（保證金）的價格距離
    - SL 不能 >  5%（保證金）的價格距離（即最多虧 5%）
    最後再對齊 tick 與最小價差。
    """
    # 以保證金百分比換算的「最低/最高」距離
    tp_min = entry * (TP_MARGIN_PCT / lev)
    sl_max = entry * (SL_MARGIN_PCT / lev)

    if side == "LONG":
        # 強制 TP 距離 >= tp_min
        tp = max(tp, entry + tp_min)
        # 強制 SL 距離 <= sl_max（離 entry 不可太遠）
        sl = max(entry - sl_max, sl)
        # 對齊 tick 並保證最小間距
        tp = _round_price_to_tick(max(tp, entry + max(tick*12, entry*0.0015)), tick, +1)
        sl = _round_price_to_tick(min(sl, entry - max(tick*12, entry*0.0015)), tick, -1)
    else:
        tp = min(tp, entry - tp_min)
        sl = min(entry + sl_max, sl)
        tp = _round_price_to_tick(min(tp, entry - max(tick*12, entry*0.0015)), tick, -1)
        sl = _round_price_to_tick(max(sl, entry + max(tick*12, entry*0.0015)), tick, +1)

    return tp, sl

def compute_exits(entry: float, side: str, st: "SymbolState"):
    """
    優先用 ATR -> (tp, sl)，但一定『不低於』 10%/5% 的底線。
    若 ATR 不可用，直接用 10%/5%。
    """
    try:
        rules = _get_symbol_rules(st.symbol)
        tick  = float(rules["tickSize"])
    except Exception:
        tick = 0.0

    if USE_ATR_EXITS:
        # 用 5m ATR，若無則退回 1m
        atr_val = atr_wilder(list(st.candles_5m), n=14) or atr_wilder(list(st.candles), n=14)
        if atr_val and atr_val > 0:
            # 先用 ATR 推一組候選
            tp, sl = tp_sl_by_atr(entry, side, atr_val, k_sl=ATR_SL_K, k_tp=ATR_TP_K, min_pct=ATR_MIN_PCT)
            # 再用 10%/5% 的底線「收緊/拉遠」
            tp, sl = _enforce_base_floor(entry, side, tp, sl, LEVERAGE, tick)
            return tp, sl

    # —— ATR 不可用：直接用 10%/5% 基準 ——
    tp, sl = tp_sl_by_margin(entry, side, TP_MARGIN_PCT, SL_MARGIN_PCT, LEVERAGE)
    if tick > 0:
        if side == "LONG":
            tp = _round_price_to_tick(max(tp, entry + max(tick*12, entry*0.0015)), tick, +1)
            sl = _round_price_to_tick(min(sl, entry - max(tick*12, entry*0.0015)), tick, -1)
        else:
            tp = _round_price_to_tick(min(tp, entry - max(tick*12, entry*0.0015)), tick, -1)
            sl = _round_price_to_tick(max(sl, entry + max(tick*12, entry*0.0015)), tick, +1)
    return tp, sl

# ===================== 訊號（收盤才判斷進場） =====================
def generate_signal(symbol: str):
    global _loss_streak_by_sym, _suspend_until_ts_by_sym
    st = SYMAP[symbol]
    need = max(EMA_SLOW, BB_LEN, VOL_MA) + 5
    def _can_trade_symbol(symbol: str) -> (bool, str):
        now = time.time()
        until_ts = _suspend_until_ts_by_sym.get(symbol, 0)
        if now < until_ts:
            remain = int(until_ts - now)
            return False, f"suspended {remain}s"
        return True, ""
    
    if len(st.candles) < need:
        return None

    if not _regime_pass(symbol):
        return None

    # 當日風控 + 名額
    can, _ = can_trade_now(SHOW_ACCOUNT)
    if not can:
        return None
    # 連虧暫停（針對該 symbol）
    ok_sym, why = _can_trade_symbol(symbol)
    if not ok_sym:
        return None
    if MAX_CONCURRENT_POS and pos_count_active(SHOW_ACCOUNT) >= MAX_CONCURRENT_POS:
        return None
    if ONE_POS_PER_SYMBOL and _get_pos(st, SHOW_ACCOUNT):
        return None

    # 冷卻
    now_ts = st.candles[-1]["ts"]
    if st.last_signal_ts and (now_ts - st.last_signal_ts) / 1000 < st.cooldown_sec:
        return None

    st.update_indicators()
    if not (st.ema_fast and st.ema_slow and st.bb_mid and st.vwap):
        return None

    closes = [c["close"] for c in st.candles]
    vols   = [c["volume"] for c in st.candles]
    m = macd_calc(closes, MACD_FAST, MACD_SLOW, MACD_SIG)
    r = rsi_calc(closes, RSI_LEN)
    if not m or r is None:
        return None
    macd_val, macd_sig, macd_hist = m
    last = st.candles[-1]
    close_px, high_px, low_px = last["close"], last["high"], last["low"]

    bbw = st.bb_width() or 0.0
    up_trend   = (st.ema_fast > st.ema_slow) and (close_px > (st.vwap or close_px))
    down_trend = (st.ema_fast < st.ema_slow) and (close_px < (st.vwap or close_px))

    vol_ok = True
    if VOL_CONFIRM and len(vols) >= VOL_MA:
        v_ma = sum(vols[-VOL_MA:]) / VOL_MA
        vol_ok = (vols[-1] >= VOL_K * v_ma)

    long_std  = vol_ok and up_trend   and macd_hist > 0 and r > 45 and close_px > st.bb_mid and bbw >= MIN_BB_WIDTH*0.6
    short_std = vol_ok and down_trend and macd_hist < 0 and r < 55 and close_px < st.bb_mid and bbw >= MIN_BB_WIDTH*0.6

    long_break  = up_trend   and macd_val > macd_sig and r < 70 and bbw > MIN_BB_WIDTH and close_px > st.bb_up
    short_break = down_trend and macd_val < macd_sig and r > 30 and bbw > MIN_BB_WIDTH and close_px < st.bb_dn
    long_pull   = up_trend   and r <= 35 and low_px  <= st.bb_dn
    short_pull  = down_trend and r >= 65 and high_px >= st.bb_up

    def pack(side, reason):
        entry = close_px
        tp, sl = compute_exits(entry, side, st)
        sig = {"symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp, "reason": reason}
        sig["orig_side"] = side
        if INVERT_SIGNALS:
            sig["side"] = "SHORT" if side == "LONG" else "LONG"
            tp, sl = tp_sl_by_atr(sig["entry"], sig["side"], atr_val, k_sl=1.5, k_tp=2.5)
            sig["tp"], sig["sl"] = tp, sl
        return sig

    # —— 多時框 + regime：5m 定性，1m 觸發 ——
    rg = regime_on_5m(st)  # "RANGE" / "UP" / "DOWN"

    # LONG
    allow_long_break = (rg == "UP")
    allow_long_pull  = (rg == "RANGE")
    if (allow_long_break and long_break) or (allow_long_pull and long_pull):
        st.last_signal_ts = now_ts
        sig = pack("LONG", "breakout" if long_break else "pullback")
        try:
            take, p, x = ML.should_take(SHOW_ACCOUNT, st)
        except NameError:
            take, p, x = True, None, None
        if not take:
            try: add_log(f"ML filter skip {symbol} ({sig['reason']}) p={p:.2f}", "dim")
            except: add_log(f"ML filter skip {symbol} ({sig['reason']})", "dim")
            return None
        sig["ml_features"] = x; sig["ml_p"] = p
        return sig

    # SHORT
    allow_short_break = (rg == "DOWN")
    allow_short_pull  = (rg == "RANGE")
    if (allow_short_break and short_break) or (allow_short_pull and short_pull):
        st.last_signal_ts = now_ts
        sig = pack("SHORT", "breakout" if short_break else "pullback")
        try:
            take, p, x = ML.should_take(SHOW_ACCOUNT, st)
        except NameError:
            take, p, x = True, None, None
        if not take:
            return None
        sig["ml_features"] = x; sig["ml_p"] = p
        return sig

    return None

# ===================== AI Decision Layer =====================
def ai_decision(st: SymbolState):
    """
    讓 AI 自行判斷是否要開倉、平倉、加碼。
    回傳：
      - "ENTER_LONG" / "ENTER_SHORT"：開倉
      - "EXIT"：出場
      - None：不動作
    """
    # --- 1) 持倉檢查：同時看 LIVE / SIM（優先 SHOW_ACCOUNT） ---
    pos_live = _get_pos(st, "LIVE")
    pos_sim  = _get_pos(st, "SIM")
    has_pos  = (pos_live is not None) or (pos_sim is not None)

    # 方便後續：決定用哪個帳戶的部位/ROI 當評估依據
    # 先用 SHOW_ACCOUNT，如果 SHOW_ACCOUNT 沒倉，就用另一個有倉的帳戶
    eval_acct = SHOW_ACCOUNT if _get_pos(st, SHOW_ACCOUNT) else ("LIVE" if pos_live else ("SIM" if pos_sim else None))

    # --- 2) 空手 → 是否進場 ---
    if not has_pos:
        # 用哪個帳戶視角跑 ML（你也可改成固定 "SIM"）
        which_for_ml = SHOW_ACCOUNT
        take, p, x = ML.should_take(which_for_ml, st)

        if take and p is not None and p > max(0.7, ML.threshold):
            # regime：用 EMA50/200 決定方向（也可換成你想要的邏輯）
            if st.ema_fast and st.ema_slow:
                if st.ema_fast > st.ema_slow:
                    console.print(f"[cyan]AI決策：進多 {st.symbol} (p={p:.2f})[/cyan]")
                    return "ENTER_LONG"
                else:
                    console.print(f"[cyan]AI決策：進空 {st.symbol} (p={p:.2f})[/cyan]")
                    return "ENTER_SHORT"
        return None

    # --- 3) 已持倉 → 是否出場 / 加碼 ---
    # eval_acct 此時不可能是 None（因為 has_pos=True），但保底一下
    eval_acct = eval_acct or "SIM"
    roi = current_roi_pct(st, eval_acct) or 0.0

    # 出場條件（你原本的 -1% / +3%）
    if roi < -5.0 or roi > 10.0:
        console.print(f"[yellow]AI決策：平倉 {st.symbol} ROI={roi:+.2f}% ({eval_acct})[/yellow]")
        return "EXIT"

    # （可選）加碼：ROI > 1.5% 且 MACD 同向
    pos = _get_pos(st, eval_acct)
    if roi > 1.5 and pos is not None:
        closes = [c["close"] for c in st.candles]
        m = macd_calc(closes, MACD_FAST, MACD_SLOW, MACD_SIG)
        if m and ((pos["side"] == "LONG" and m[2] > 0) or (pos["side"] == "SHORT" and m[2] < 0)):
            console.print(f"[green]AI決策：考慮加碼 {st.symbol} (ROI={roi:.2f}% • {eval_acct})[/green]")
            # 之後可 return "ADD" 並在 on_kline 實作加碼邏輯
            pass

    return None
    
# ===================== 行情處理 =====================
def on_kline(symbol, k):
    st = SYMAP[symbol]
    ts = int(k["t"])
    closed = k.get("x", False)

    o = float(k["o"]); h = float(k["h"]); l = float(k["l"]); c = float(k["c"]); v = float(k["v"])
    typical = (h + l + c)/3
    bar = {"open": o, "high": h, "low": l, "close": c,
           "volume": v, "typical": typical, "ts": ts}
    # —— 1m→5m 聚合：每次更新 1m bar 都持續聚合 ——
    try:
        st.ingest_1m_to_5m({
            "ts": ts, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "typical": typical
        })
    except Exception:
        pass
        
    if st.candles and st.candles[-1]["ts"] == ts:
        st.candles[-1] = bar
    else:
        st.candles.append(bar)

    st.update_indicators()

    if closed:
        # === 路徑一：策略訊號優先 ===
        sig = generate_signal(symbol)
        if sig:
            if INVERT_SIGNALS and "orig_side" in sig and sig["orig_side"] != sig["side"]:
                console.print(f"[bold yellow]INVERT {symbol}: {sig['orig_side']} -> {sig['side']} ({sig['reason']})[/bold yellow]")

            place_order(
                symbol=symbol,
                side=sig["side"],
                entry=sig["entry"],
                sl=sig["sl"],
                tp=sig["tp"],
                ml_features=sig.get("ml_features"),
                ml_p=sig.get("ml_p")
            )
        else:
            # === 路徑二：若策略未觸發，才讓 AI 決策（且達門檻才開）===
            if AI_ENABLE and (ML.pos_seen + ML.neg_seen) >= AI_MIN_SEEN_FOR_ACTION:
                decision = ai_decision(st)
            else:
                decision = None
            if decision in ("ENTER_LONG", "ENTER_SHORT"):
                try:
                    take, p, x = ML.should_take(SHOW_ACCOUNT, st)
                except NameError:
                    take, p, x = True, None, None

                if take:
                    side  = "LONG" if decision == "ENTER_LONG" else "SHORT"
                    entry = st.last_price if st.last_price is not None else (st.candles[-1]["close"] if st.candles else None)
                    if entry is not None:
                        atr_val = atr_wilder(list(st.candles), n=14)
                        tp, sl  = tp_sl_by_atr(entry, side, atr_val, k_sl=1.5, k_tp=2.5)
                        place_order(
                            symbol=symbol,
                            side=side,
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            ml_features=x,
                            ml_p=p
                        )
            elif decision == "EXIT":
                close_position(symbol, "AI exit", st.last_price)
                
def _reset_states_for_backtest():
    global ACCOUNT_LIVE, ACCOUNT_SIM, SYMAP, SYMBOLS
    ACCOUNT_LIVE = Account("LIVE"); ACCOUNT_SIM = Account("SIM")
    ACCOUNT_SIM.balance = 10000.0
    for s in SYMBOLS: SYMAP[s] = SymbolState(s)

# ===================== Backtester（事件驅動） =====================
def _trade_list_to_equity_curve(start_equity: float, trades: list[dict], fee_pct=TAKER_FEE_PCT/100.0):
    eq = [start_equity]
    cur = start_equity
    for tr in trades:
        cur += float(tr["pnl_cash"])
        eq.append(cur)
    return eq

def _max_drawdown(equity_curve: list[float]):
    peak = equity_curve[0] if equity_curve else 0.0
    mdd = 0.0
    for x in equity_curve:
        if x > peak: peak = x
        dd = (peak - x) / peak if peak > 0 else 0.0
        mdd = max(mdd, dd)
    return mdd

def _sharpe(equity_curve: list[float], bar_risk_free=0.0):
    if len(equity_curve) < 3:
        return 0.0
    rets = []
    for i in range(1, len(equity_curve)):
        r = (equity_curve[i] - equity_curve[i-1]) / (equity_curve[i-1] if equity_curve[i-1] != 0 else 1.0)
        rets.append(r - bar_risk_free)
    if not rets: return 0.0
    avg = sum(rets)/len(rets)
    var = sum((x-avg)**2 for x in rets)/len(rets)
    sd = var**0.5
    return (avg / sd) if sd > 0 else 0.0

def _avg_R(trades: list[dict]):
    # 以每筆的「實際移動 ÷ 開倉風險R」粗估；若缺R則略過。
    Rs = []
    for tr in trades:
        try:
            entry = float(tr["entry"]); exitp = float(tr["exit"])
            side = tr["side"]
            riskR = abs(float(tr.get("risk_R") or 0.0))
            move = (exitp - entry) * (1 if side=="LONG" else -1)
            if riskR > 0:
                Rs.append(move / riskR)
        except Exception:
            pass
    return (sum(Rs)/len(Rs)) if Rs else 0.0

def _loss_streak_stats(trades: list[dict]):
    max_ls = 0; cur = 0
    for tr in trades:
        if float(tr["pnl_cash"]) <= 0:
            cur += 1; max_ls = max(max_ls, cur)
        else:
            cur = 0
    return max_ls

def _fee_ratio(trades: list[dict]):
    fee_sum = 0.0; pnl_gross_sum = 0.0
    for tr in trades:
        # 你在 close_position_one 已經用 taker 雙邊費扣掉淨值，這裡用估法重算個比率供參考
        entry = float(tr["entry"]); exitp = float(tr["exit"])
        side  = tr["side"]; notional = abs(exitp + entry)/2.0  # 粗估
        pnl_cash = float(tr["pnl_cash"])
        pnl_gross = abs(exitp - entry) * (1 if side=="LONG" else -1)
        pnl_gross_sum += abs(pnl_gross)
        # 假設費用占名義約 2*fee_pct*notional_q，這裡僅作展示，不影響結算
    return 0.0 if pnl_gross_sum<=0 else max(0.0, min(1.0, fee_sum/pnl_gross_sum))

def compute_backtest_metrics(start_equity: float, trades: list[dict]):
    n = len(trades)
    wins = sum(1 for t in trades if float(t["pnl_cash"]) > 0)
    losses = n - wins
    winrate = (wins/n*100.0) if n else 0.0
    exp_return = (sum(float(t["pnl_cash"]) for t in trades)/n) if n else 0.0
    avg_R = _avg_R(trades)
    eq_curve = _trade_list_to_equity_curve(start_equity, trades)
    mdd = _max_drawdown(eq_curve)
    sharpe = _sharpe(eq_curve)
    max_ls = _loss_streak_stats(trades)
    fee_ratio = _fee_ratio(trades)
    return {
        "trades": n,
        "winrate_pct": round(winrate, 2),
        "exp_pnl_per_trade": round(exp_return, 4),
        "avg_R": round(avg_R, 3),
        "sharpe": round(sharpe, 3),
        "max_loss_streak": int(max_ls),
        "mdd_pct": round(mdd*100.0, 2),
        "fee_ratio_est": round(fee_ratio*100.0, 2)
    }

def run_backtest(symbol: str, interval: str, klines: list[tuple]):
    """
    klines: [(t_ms,o,h,l,c,v), ...]  (你線上取得或自備)
    使用現有 generate_signal / tp_sl_by_atr / place_order_one / close_position_one 的事件流。
    """
    global INTERVAL, EXECUTION_MODE, SHOW_ACCOUNT, SYMBOLS, SYMAP
    old_iv, old_mode, old_view = INTERVAL, EXECUTION_MODE, SHOW_ACCOUNT
    INTERVAL = interval; EXECUTION_MODE = "SIM"; SHOW_ACCOUNT = "SIM"
    if symbol not in SYMAP: SYMAP[symbol] = SymbolState(symbol)
    if symbol not in SYMBOLS: SYMBOLS.append(symbol)
    _reset_states_for_backtest()

    start_eq = ACCOUNT_SIM.balance
    for (t,o,h,l,c,v) in klines:
        # 只在收盤事件觸發交易（與 on_kline 一致）
        k = {"t": int(t), "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": str(v), "x": True}
        on_kline(symbol, k)

    metrics = compute_backtest_metrics(start_eq, ACCOUNT_SIM.trades)
    # 讓調試更直觀：印出
    console.print(f"[bold cyan]Backtest {symbol} {interval}[/bold cyan] -> {metrics}")
    INTERVAL, EXECUTION_MODE, SHOW_ACCOUNT = old_iv, old_mode, old_view
    return metrics, ACCOUNT_SIM.trades
    
def offline_replay(symbol, interval, klines):
    """
    klines: [(t_ms,o,h,l,c,v), ...]
    """
    global INTERVAL, EXECUTION_MODE, SHOW_ACCOUNT, SYMBOLS, SYMAP
    old_iv, old_mode, old_view = INTERVAL, EXECUTION_MODE, SHOW_ACCOUNT
    INTERVAL = interval; EXECUTION_MODE = "SIM"; SHOW_ACCOUNT = "SIM"
    if symbol not in SYMAP: SYMAP[symbol] = SymbolState(symbol)
    if symbol not in SYMBOLS: SYMBOLS.append(symbol)
    _reset_states_for_backtest()

    eq = []
    for (t,o,h,l,c,v) in klines:
        k = {"t": int(t), "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": str(v), "x": True}
        on_kline(symbol, k)
        eq.append((t, ACCOUNT_SIM.balance))

    INTERVAL, EXECUTION_MODE, SHOW_ACCOUNT = old_iv, old_mode, old_view
    return eq, ACCOUNT_SIM.trades
def ai_should_exit_for(st, which: str) -> bool:
    """
    只根據「該帳戶」的 ROI 來判斷是否 AI 出場。
    避免 ai_decision() 同時檢查兩邊造成重複或互相干擾。
    """
    p = _get_pos(st, which)
    if not p:
        return False
    roi = current_roi_pct(st, which) or 0.0
    # 你的門檻（與 ai_decision 裡一致）：虧損大於 -3% 或獲利超過 +1% 就出
    return (roi < -5.0) or (roi > 10.0)


# 建議放在檔案全域，避免 AI 出場過於頻繁（ms）
_AI_EXIT_COOLDOWN_MS = 1500
_last_ai_exit_ms = {}  # key=(which,symbol) → last_ts

def on_agg_trade(symbol, msg):
    st = SYMAP.get(symbol)
    if not st:
        return

    try:
        # 1) 解析價格（兼容不同欄位）
        price = msg.get("p") or msg.get("price") or msg.get("ap")  # aggTrade 常見欄位 p
        price = float(price)
        st.last_price = price

        # 2) 先更新模擬端的追蹤停損 → 再檢查是否觸發 TP/SL
        _update_sim_trailing(st, "SIM")
        # 🟢 ROI fallback 檢查（優先於 TP/SL）
        for which in ("LIVE", "SIM"):
            try:
                ok, roi = _roi_fallback_should_exit(st, which)
                if ok and not _close_too_soon(which, st.symbol):
                    close_position_one(
                        st.symbol,
                        f"ROI fallback {roi:+.2f}%",
                        st.last_price or 0.0,
                        which
                    )
            except Exception as e:
                add_log(f"roi_fallback error {which} {st.symbol}: {e}", "red")
        _check_tp_sl_and_close_if_hit(st, "SIM")

        # 如需本地模擬 LIVE 出場（平倉）才開這行（通常不用，讓交易所 TP/SL 管理）
        # if not EXCHANGE_MANAGE_EXIT:
        #     _check_tp_sl_and_close_if_hit(st, "LIVE")

        # 3) AI 即時出場（只有「有倉」才評估；並加上簡單節流）
        now = utc_ms()

        def _ai_exit_guard(which: str) -> bool:
            if not _get_pos(st, which):
                return False
            W = (which or "").upper()
            S = (symbol or "").upper()
            key = (W, S)
            last = _last_ai_exit_ms.get(key, 0)
            if now - last < _AI_EXIT_COOLDOWN_MS:
                return False
            _last_ai_exit_ms[key] = now
            return True

        def _fallback_roi_exit(which: str) -> bool:
            roi = current_roi_pct(st, which) or 0.0
            return (roi < -5.0) or (roi > 10.0)

        # —— SIM 優先：只記帳，不送實單 ——
        if _ai_exit_guard("SIM"):
            try:
                should = ai_should_exit_for(st, "SIM")
            except Exception as _e:
                add_log(f"AI exit err(SIM): {_e}", "yellow")
                should = _fallback_roi_exit("SIM")
            if should:
                close_position_one(st.symbol, "AI exit (real-time)", st.last_price, "SIM")

        # —— LIVE：預設真的送 reduceOnly；想先記帳可加 skip_exchange=True ——
        if _ai_exit_guard("LIVE"):
            try:
                should = ai_should_exit_for(st, "LIVE")
            except Exception as _e:
                add_log(f"AI exit err(LIVE): {_e}", "yellow")
                should = _fallback_roi_exit("LIVE")
            if should:
                # ★ 先鏡射關掉 SIM（若仍持有）
                if _get_pos(st, "SIM"):
                    close_position_one(st.symbol, "Mirror LIVE exit", st.last_price, "SIM")
                # 再關 LIVE
                close_position_one(
                    st.symbol,
                    "AI exit (real-time)",
                    st.last_price,
                    "LIVE"
                    # , skip_exchange=True  # 想先記帳再自行手動也可開這行
                )
    except Exception as e:
        add_log(f"agg_trade error {symbol}: {e}", "red")
        return

# ===================== WS =====================
def ws_worker(symbols, live_obj: Live, layout: Layout):
    if not websocket:
        console.print("[red]請先安裝： pip install websocket-client[/red]")
        return
    streams = []
    for s in symbols:
        s_low = s.lower()
        streams.append(f"{s_low}@kline_{INTERVAL}")
        streams.append(f"{s_low}@aggTrade")
    url = f"{WS_URL}{WS_COMBINED_PATH}{'/'.join(streams)}"

    last_update = 0.0
    update_interval = 1 / max(1, REFRESH_FPS)

    def on_message(ws, message):
        nonlocal last_update
        j = json.loads(message)
        data = j.get("data", {})

        if "k" in data and "s" in data:
            on_kline(data["s"], data["k"])
        elif data.get("e") == "aggTrade" and "s" in data and "p" in data:
            on_agg_trade(data["s"], data)
            add_log(f"aggTrade {data['s']} p={data['p']}", "dim")

        # 🟢 新增：定期與交易所同步持倉（內含 5 秒節流）
        try:
            sync_live_positions_periodic()
            sync_live_balance()  # 可選：一併更新權益顯示
        except Exception:
            pass

        now = time.time()
        if now - last_update >= update_interval:
            render_layout(layout)
            try:
                live_obj.update(layout)
            except Exception:
                pass
            last_update = now

    def on_error(ws, err): console.print(f"[red]WS error: {err}[/red]")
    def on_close(ws, a, b): console.print("[yellow]WS closed[/yellow]")
    def on_open(ws): console.print(f"[green]WS connected ({INTERVAL})[/green]")

    ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close, on_open=on_open)
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

# ===================== 面板 =====================
def build_layout():
    layout = Layout()
    h = console.size.height
    footer_size = max(12, int(h * 0.35))   # ← 讓 footer 佔終端高度 ~35%

    layout.split_column(
        Layout(name="header", size=HEADER_ROWS),
        Layout(name="body"),
        Layout(name="footer", size=footer_size)  # ← 用動態 footer_size
    )
    layout["body"].split_row(
        Layout(name="left",  ratio=LEFT_RATIO,  minimum_size=50),
        Layout(name="right", ratio=RIGHT_RATIO, minimum_size=36),
    )
    layout["right"].split_column(
        Layout(name="right_top"),
        Layout(name="right_bottom"),
    )
    return layout

def header_panel():
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    env_txt = ("TESTNET" if TESTNET else "LIVE-KEY") + f" | MODE={EXECUTION_MODE} | VIEW={SHOW_ACCOUNT}"
    line = f"Binance Futures  |  {INTERVAL}  |  {now}  |  {env_txt}"
    text = Text(line)
    return Panel.fit(Align.center(text), box=box.ROUNDED, title="Status", border_style="cyan", padding=(0,1))

def table_symbols():
    t = Table(box=box.MINIMAL_DOUBLE_HEAD, show_lines=False, pad_edge=False, expand=True)
    t.add_column("Symbol", justify="left", no_wrap=True)
    t.add_column("Last",   justify="right")
    t.add_column("Price",  justify="right")
    t.add_column("EMA50/200", justify="right")
    t.add_column("VWAP",   justify="right")
    t.add_column("RSI",    justify="right")
    t.add_column("MACD",   justify="right")
    t.add_column("BB w",   justify="right")
    t.add_column("Band",   justify="center")
    t.add_column("Pos1/ROI1 (LIVE)", justify="right", style="green")
    t.add_column("Pos2/ROI2 (SIM)",  justify="right", style="yellow")

    # ✅ 渲染前把「有持倉的」放最上面（LIVE 或 SIM 任一有倉就算）
    held, rest = [], []
    for s in SYMBOLS:
        st = SYMAP[s]
        if (st.position_live is not None) or (st.position_sim is not None):
            held.append(s)
        else:
            rest.append(s)
    ordered = held + rest

    for s in ordered:
        st = SYMAP[s]
        last_str  = f"{st.last_price:,.6f}" if st.last_price is not None else "-"
        price_str = f"{st.candles[-1]['close']:,.6f}" if st.candles else "-"

        ema_pair = f"{(st.ema_fast or 0):,.2f}/{(st.ema_slow or 0):,.2f}" if st.ema_slow else "-/-"
        vwap = f"{(st.vwap or 0):,.2f}" if st.vwap else "-"

        rsi = "-"
        macd_text = "-"
        if len(st.candles) >= MACD_SLOW+MACD_SIG+5:
            closes = [c["close"] for c in st.candles]
            r = rsi_calc(closes, RSI_LEN)
            m = macd_calc(closes, MACD_FAST, MACD_SLOW, MACD_SIG)
            if r is not None: rsi = f"{r:5.1f}"
            if m: macd_text = f"{m[0]:.3f}/{m[1]:.3f}"

        bbw = "-"
        w = st.bb_width()
        if w is not None: bbw = f"{w*100:5.2f}%"

        band_cell = Text("·", style="dim")
        if st.bb_up and st.bb_dn and st.candles:
            close_px = st.candles[-1]["close"]
            if close_px > st.bb_up:
                band_cell = Text("↑", style="green")
            elif close_px < st.bb_dn:
                band_cell = Text("↓", style="red")

        # LIVE
        p1_str, roi1_str = "-", "-"
        p1 = st.position_live
        if p1:
            p1_str = f"{p1['side']} {p1['qty']:.3f}@{p1['entry']:.4f}"
            roi1 = current_roi_pct(st, "LIVE")
            if roi1 is not None: roi1_str = f"{roi1:+.2f}%"

        # SIM
        p2_str, roi2_str = "-", "-"
        p2 = st.position_sim
        if p2:
            p2_str = f"{p2['side']} {p2['qty']:.3f}@{p2['entry']:.4f}"
            roi2 = current_roi_pct(st, "SIM")
            if roi2 is not None: roi2_str = f"{roi2:+.2f}%"

        t.add_row(
            s, last_str, price_str, ema_pair, vwap, rsi, macd_text, bbw,
            band_cell,
            f"{p1_str}/{roi1_str}", f"{p2_str}/{roi2_str}"
        )

    return Panel(t, title="Markets", border_style="green")
    
def table_perf():
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Metric")
    t.add_column("LIVE", justify="right")
    t.add_column("SIM", justify="right")

    accL, accS = ACCOUNT_LIVE, ACCOUNT_SIM

    # Balance (Equity)
    t.add_row("Balance (Equity)",
              f"{accL.balance:,.2f} USDT",
              f"{accS.balance:,.2f} USDT")

    # LIVE 細節
    if accL._wallet is not None:
        t.add_row("  Wallet", f"{accL._wallet:,.2f} USDT", "")
    if accL._unrealized is not None:
        t.add_row("  Unrealized PnL", f"{accL._unrealized:,.2f} USDT", "")
    if accL._available is not None:
        t.add_row("  Available", f"{accL._available:,.2f} USDT", "")

    # Sizing
    sizing_desc = "ALLOC" if POSITION_SIZING.upper() == "ALLOC" else "RISK"
    sizing_param = f"{ALLOC_PCT:.2f}%" if sizing_desc == "ALLOC" else f"{RISK_PER_TRADE_PCT:.2f}%"
    t.add_row("Sizing Mode", f"{sizing_desc} ({sizing_param})", f"{sizing_desc} ({sizing_param})")

    # Day PnL（以 account.daily_pnl 與 daily_start_equity 計）
    def _day_pct(acc):
        base = acc.daily_start_equity or acc.balance
        return (acc.daily_pnl / base * 100.0) if base else 0.0

    t.add_row("Day PnL",
              f"{accL.daily_pnl:,.2f} ({_day_pct(accL):.2f}%)",
              f"{accS.daily_pnl:,.2f} ({_day_pct(accS):.2f}%)")

    # Total PnL（以 account.total_pnl，不再動態 sum trades）
    t.add_row("Total PnL", f"{accL.total_pnl:,.2f}", f"{accS.total_pnl:,.2f}")

    # Open positions
    t.add_row("Open Positions",
              f"{pos_count_active('LIVE')} / {MAX_CONCURRENT_POS}",
              f"{pos_count_active('SIM')} / {MAX_CONCURRENT_POS}")

    # Risk/Trade
    t.add_row("Risk/Trade",
              f"{RISK_PER_TRADE_PCT:.2f}% (x{LEVERAGE})",
              f"{RISK_PER_TRADE_PCT:.2f}% (x{LEVERAGE})")

    # TP/SL（展示設定值）
    t.add_row("TP/SL",
              f"+{TP_MARGIN_PCT*100:.1f}% margin / -{SL_MARGIN_PCT*100:.1f}% margin",
              f"+{TP_MARGIN_PCT*100:.1f}% margin / -{SL_MARGIN_PCT*100:.1f}% margin")

    # Daily Stop
    t.add_row("Daily Stop",
              f"+{DAILY_TARGET_PCT*100:.1f}% / -{DAILY_MAX_LOSS_PCT*100:.1f}%",
              f"+{DAILY_TARGET_PCT*100:.1f}% / -{DAILY_MAX_LOSS_PCT*100:.1f}%")

    # ML
    t.add_row("ML Samples / Th",
              f"{ML.model.n_seen} / {ML.threshold:.2f}",
              f"{ML.model.n_seen} / {ML.threshold:.2f}")

    return Panel(t, title=f"Performance (VIEW: {SHOW_ACCOUNT})", border_style="magenta")

def grid_advisor_panel():
    # Top 候選
    try:
        top = rank_grid_candidates_cached(top_n=6, hours=GRID_HOURS)
    except Exception as e:
        top = []
        top_err = str(e)

    from rich.columns import Columns
    from rich.table import Table

    top_tbl = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False, pad_edge=False, title="Top Grid Candidates")
    top_tbl.add_column("Sym", style="cyan", no_wrap=True)
    top_tbl.add_column("Score", justify="right")
    top_tbl.add_column("BBw%", justify="right")
    top_tbl.add_column("|ret|%", justify="right")
    top_tbl.add_column("Liq", justify="right")
    top_tbl.add_column("FR", justify="right")

    pick_symbol = None
    if top:
        pick_symbol = top[0]["symbol"]  # 自動挑第一名給下方 Grid 詳解
        for r in top:
            top_tbl.add_row(
                r["symbol"],
                f"{r['score']:.1f}",
                f"{r['bb_width']*100:.2f}",
                f"{r['ret_abs']*100:.2f}",
                f"{r['liq_norm']:.2f}",
                f"{r['funding_abs']*100:.3f}%"
            )
    else:
        top_tbl.add_row("-", "-", "-", "-", "-", "-")

    # 單一標的詳解（用 pick_symbol 或 GRID_SYMBOL）
    target = pick_symbol or GRID_SYMBOL
    try:
        s = suggest_grid(target, hours=GRID_HOURS, grid_count=GRID_COUNT)
    except Exception as e:
        return Panel(Columns([top_tbl, Panel(f"Grid 計算錯誤: {e}", title="Grid Advisor")]), border_style="blue")

    if not s:
        return Panel(Columns([top_tbl, Panel("資料不足，稍後再試", title="Grid Advisor")]), border_style="blue")

    preview = ", ".join(f"{p:.4f}" for p in s["grid_prices"][:8])
    if len(s["grid_prices"]) > 8:
        preview += ", ..."

    detail_tbl = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False, pad_edge=False, title=f"Grid Advisor • {target}")
    detail_tbl.add_column("項目", style="cyan", no_wrap=True)
    detail_tbl.add_column("值", justify="right")
    detail_tbl.add_row("Window", f"{s['hours']}h  ({INTERVAL})")
    detail_tbl.add_row("Trend",  s["trend"])
    detail_tbl.add_row("Range",  f"{s['lower']:.4f}  ~  {s['upper']:.4f}")
    detail_tbl.add_row("Grids",  f"{s['grid_count']} 等距")
    detail_tbl.add_row("EMA50/200", f"{(s['ema_pair'][0] or 0):.2f} / {(s['ema_pair'][1] or 0):.2f}")
    detail_tbl.add_row("BB width", f"{(s['bb_width'] or 0)*100:.2f}%")
    detail_tbl.add_row("Ret", f"{s['ret']*100:.2f}%")
    detail_tbl.add_row("Prices", preview)

    return Panel(Columns([top_tbl, detail_tbl]), title="Grid Advisor", border_style="blue")
    
# === 支援 offset / 分頁版 table_trades ===
def table_trades(max_rows=20, offset=0):
    t = Table(box=box.MINIMAL, expand=True)
    t.add_column("Time"); t.add_column("Acct"); t.add_column("Sym"); t.add_column("Side")
    t.add_column("Entry", justify="right"); t.add_column("Exit", justify="right")
    t.add_column("PnL$", justify="right"); t.add_column("Net%", justify="right")
    t.add_column("Reason")

    # ✅ 只顯示 LIVE 交易紀錄
    rows = [(r, "LIVE") for r in ACCOUNT_LIVE.trades]
    rows.sort(key=lambda x: x[0]["ts"])

    # === 支援 offset（往上翻舊交易） ===
    start = max(0, len(rows) - max_rows - offset)
    end   = max(0, len(rows) - offset)
    page  = rows[start:end]

    # === 畫表格 ===
    for r, who in page:
        t.add_row(
            r["ts"], who, r["symbol"], r["side"],
            str(r["entry"]), str(r["exit"]),
            str(r["pnl_cash"]), str(r["net_pct"]), r["reason"]
        )

    win = sum(1 for r,_ in page if r["pnl_cash"] > 0)
    loss = sum(1 for r,_ in page if r["pnl_cash"] <= 0)
    title = f"Trades • LIVE Only  (W/L={win}/{loss}, Win%={(win/(win+loss)*100 if win+loss>0 else 0):.1f}%) [offset={offset}]"
    return Panel(t, title=title, border_style="yellow")

# === Layout：同時顯示 Trades + Logs（垂直排列） ===
from rich.columns import Columns
from rich.layout import Layout
def _keyboard_worker():
    import sys, termios, tty, select
    global TRADE_OFFSET, LOGS_OFFSET
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not r:
                time.sleep(0.05);
                continue
            ch = sys.stdin.read(1)
            if ch == '[':     LOGS_OFFSET = min(LOGS_OFFSET + LOGS_PAGE, 10_000)
            elif ch == ']':   LOGS_OFFSET = max(LOGS_OFFSET - LOGS_PAGE, 0)
            elif ch == '{':   TRADE_OFFSET = min(TRADE_OFFSET + TRADE_PAGE, 10_000)
            elif ch == '}':   TRADE_OFFSET = max(TRADE_OFFSET - TRADE_PAGE, 0)
            elif ch in ('g','G'): LOGS_OFFSET = 0; TRADE_OFFSET = 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def _recalc_pages_by_console():
    global TRADE_PAGE, LOGS_PAGE
    h = console.size.height
    # footer 取 35%，其中 60% 給 Trades、40% 給 Logs，扣掉邊框/標題
    footer_h = max(12, int(h * 0.35))
    trades_h = max(8, int(footer_h * 0.60) - 4)
    logs_h   = max(6, int(footer_h * 0.40) - 4)
    TRADE_PAGE = max(5, trades_h)
    LOGS_PAGE  = max(5, logs_h)


def render_layout(layout):
    _recalc_pages_by_console()
    layout["header"].update(header_panel())
    layout["left"].update(table_symbols())
    layout["right_top"].update(table_perf())
    layout["right_bottom"].update(market_confidence_panel(top_n=12))
    from rich.layout import Layout
    footer = Layout()
    footer.split_column(
        # ✅ 用 trades_panel()；它會吃全域的 TRADE_PAGE / TRADE_OFFSET
        Layout(trades_panel(SHOW_ACCOUNT), name="trades", ratio=3, minimum_size=8),
        # ✅ Logs 保持用 logs_panel()（會吃 LOGS_PAGE / LOGS_OFFSET）
        Layout(logs_panel(max_rows=LOGS_PAGE, offset=LOGS_OFFSET),
               name="logs", ratio=2, minimum_size=6),
    )
    layout["footer"].update(footer)
    
# ===================== 啟動準備 =====================
def boot_rest_warmup():
    limit = 500
    for s in SYMBOLS:
        try:
            # 直接用完整交易對當 pair（例如 ETHUSDT）
            kl = binance_get(
                "/fapi/v1/continuousKlines",
                f"pair={s}&contractType=PERPETUAL&interval={INTERVAL}&limit={limit}"
            )
        except Exception:
            try:
                kl = binance_get(
                    "/fapi/v1/klines",
                    f"symbol={s}&interval={INTERVAL}&limit={limit}"
                )
            except Exception as e2:
                console.print(f"[red]warmup failed for {s}: {e2}[/red]")
                continue

        st = SYMAP[s]
        st.candles.clear()
        for k in kl:
            o,h,l,c,v = map(float, (k[1],k[2],k[3],k[4],k[5]))
            ts = int(k[0]); typical = (h+l+c)/3
            st.candles.append({
                "open":o,"high":h,"low":l,"close":c,
                "volume":v,"typical":typical,"ts":ts
            })
        st.update_indicators()
def restore_trades_from_csv(path="trades.csv"):
    import csv, os
    if not os.path.exists(path):
        return 0
    loaded = 0
    with open(path, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                acct = (row.get("account") or row.get("acct") or "LIVE").upper()
                rec = {
                    "ts": row.get("ts") or "",
                    "symbol": row.get("symbol") or row.get("Sym") or "",
                    "side": row.get("side") or row.get("Side") or "",
                    "entry": float(row.get("entry") or row.get("Entry") or 0),
                    "exit": float(row.get("exit") or row.get("Exit") or 0),
                    "pnl_cash": float(row.get("pnl_cash") or row.get("PnL$") or 0),
                    "net_pct": float(row.get("net_pct") or row.get("Net%") or 0),
                    "risk_R": float(row.get("risk_R") or 0),
                    "reason": row.get("reason") or row.get("Reason") or "",
                }
                if acct == "LIVE":
                    ACCOUNT_LIVE.trades.append(rec)
                    ACCOUNT_LIVE.total_pnl += rec["pnl_cash"]
                    ACCOUNT_LIVE.daily_start_equity = ACCOUNT_LIVE.daily_start_equity or ACCOUNT_LIVE.balance
                else:
                    ACCOUNT_SIM.trades.append(rec)
                    ACCOUNT_SIM.total_pnl += rec["pnl_cash"]
                    ACCOUNT_SIM.daily_start_equity = ACCOUNT_SIM.daily_start_equity or ACCOUNT_SIM.balance
                loaded += 1
            except Exception:
                continue
    return loaded
# ===================== WS Manager（動態重啟） =====================
class WSManager:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.thread = None
        self.stop_flag = threading.Event()

    def _build_url(self, symbols):
        streams = []
        for s in symbols:
            s_low = s.lower()
            streams.append(f"{s_low}@kline_{INTERVAL}")
            streams.append(f"{s_low}@aggTrade")
        return f"{WS_URL}{WS_COMBINED_PATH}{'/'.join(streams)}"

    def _worker(self, symbols, live_obj: Live):
        if not websocket:
            console.print("[red]請先安裝： pip install websocket-client[/red]")
            return
        url = self._build_url(symbols)
        last_update = 0.0
        update_interval = 1 / max(1, REFRESH_FPS)

        def on_message(ws, message):
            nonlocal last_update
            j = json.loads(message); data = j.get("data", {})
            if "k" in data and "s" in data: on_kline(data["s"], data["k"])
            elif data.get("e") == "aggTrade" and "s" in data and "p" in data: on_agg_trade(data["s"], data)
            now = time.time()
            if now - last_update >= update_interval:
                render_layout(self.layout)
                try: live_obj.update(self.layout)
                except Exception: pass
                last_update = now
            if self.stop_flag.is_set(): ws.close()

        def on_open(ws): console.print(f"[green]WS connected ({INTERVAL}) - {len(symbols)} symbols[/green]")
        def on_close(ws, a, b): console.print("[yellow]WS closed[/yellow]")
        def on_error(ws, err): console.print(f"[red]WS error: {err}[/red]")

        ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close, on_open=on_open)
        ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

    def start(self, symbols, live_obj: Live):
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._worker, args=(symbols, live_obj), daemon=True)
        self.thread.start()

    def restart(self, symbols, live_obj: Live):
        self.stop()
        time.sleep(1)
        self.start(symbols, live_obj)

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.stop_flag.set()
            self.thread.join(timeout=5)
            self.thread = None
            
class UserWSManager:
    def __init__(self):
        self.thread = None
        self.keepalive_thread = None
        self.stop_flag = threading.Event()
        self.listen_key = None
        self._ws = None

    # ----------------- WS event handlers -----------------
    def _on_message(self, ws, message):
        data = json.loads(message)
        etype = data.get("e")
        if etype == "ORDER_TRADE_UPDATE":
            self._handle_order_trade_update(data)
        elif etype == "ACCOUNT_UPDATE":
            self._handle_account_update(data)

    def _on_error(self, ws, err):
        add_log(f"UserWS error: {err}", "red")

    def _on_close(self, ws, a, b):
        console.print("[yellow]UserWS closed[/yellow]")

    def _on_open(self, ws):
        console.print("[green]UserWS connected[/green]")

    # ----------------- Handlers -----------------
    def _handle_order_trade_update(self, data):
        o = data.get("o", {})
        sym        = o.get("s")
        status     = o.get("X")                          # FILLED / PARTIALLY_FILLED / NEW...
        side_raw   = (o.get("S") or "").upper()          # BUY / SELL
        avg_px     = float(o.get("ap") or 0)             # 平均成交價（若有）
        realized   = float(o.get("rp") or 0)             # 已實現損益（有些事件才帶）
        commission = float(o.get("n") or 0)              # 手續費
        cp = str(o.get("cp") or "").lower() == "true"    # closePosition
        ro = str(o.get("R")  or o.get("ro") or "").lower() == "true"  # reduceOnly

        # 成交後先做一次保底同步（降低殘影）
        if status == "FILLED":
            try:
                sync_live_positions_periodic()
            except Exception:
                pass

        if sym not in SYMAP:
            with ORDER_LOCK:
                OPEN_INFLIGHT.discard(sym)
            return

        st = SYMAP[sym]
        local = st.position_live

        # 若本地沒有倉位，這次更新多半是開倉或與我們無關；釋放 inflight 即可
        if not local:
            with ORDER_LOCK:
                OPEN_INFLIGHT.discard(sym)
            return

        # 判定是否等同平倉
        is_close_by_flag = cp or ro
        opp_by_side = False
        if side_raw in ("BUY", "SELL"):
            opp_by_side = ((local["side"] == "LONG"  and side_raw == "SELL") or
                           (local["side"] == "SHORT" and side_raw == "BUY"))

        should_close = (status == "FILLED") and (is_close_by_flag or opp_by_side)

        if should_close:
            # 以成交均價為準，沒有就用最新/進場價
            px = avg_px if avg_px > 0 else (st.last_price or local["entry"])
            reason = "Exchange exit (TP/SL)" if cp else ("ReduceOnly filled" if ro else "Opposite-side filled")

            # 走既有流程（含撤單、清理、ML）
            close_position_one(sym, reason, px, "LIVE", skip_exchange=True)

            # 若交易所回了 realized / 手續費，覆寫最後一筆 PnL 讓帳務更準
            try:
                if ACCOUNT_LIVE.trades:
                    last = ACCOUNT_LIVE.trades[-1]
                    if last.get("symbol") == sym and "LIVE" in last.get("reason", ""):
                        recorded = float(last.get("pnl_cash", 0.0))
                        precise  = float(realized - commission)
                        delta    = precise - recorded
                        last["pnl_cash"] = round(precise, 2)
                        ACCOUNT_LIVE.balance   += delta
                        ACCOUNT_LIVE.daily_pnl += delta
                        ACCOUNT_LIVE.total_pnl += delta
                        console.print(f"[dim]LIVE PnL refined by exchange: {sym} Δ={delta:+.2f} (rp={realized:.2f}, fee={commission:.2f})[/dim]")
            except Exception as _e:
                console.print(f"[dim]trade refine skip: {_e}[/dim]")

            # 釋放 inflight（保險）
            with ORDER_LOCK:
                OPEN_INFLIGHT.discard(sym)
            return

        # 非平倉事件：若是完全成交的開倉，也釋放 inflight
        if status == "FILLED":
            with ORDER_LOCK:
                OPEN_INFLIGHT.discard(sym)

    def _handle_account_update(self, data):
        """備援：從帳戶更新同步持倉數量（避免漏單）。"""
        a = data.get("a", {})
        positions = a.get("P", [])
        for p in positions:
            try:
                sym = p.get("s")
                amt = float(p.get("pa") or 0)     # positionAmt
                entry = float(p.get("ep") or 0)   # entryPrice
                if sym not in SYMAP:
                    continue
                st = SYMAP[sym]
                if abs(amt) <= 0:
                    if st.position_live:
                        px = st.last_price or st.position_live["entry"]
                        close_position_one(sym, "Account sync flat", px, "LIVE")
                else:
                    side = "LONG" if amt > 0 else "SHORT"
                    qty  = abs(amt)
                    if (not st.position_live or
                        st.position_live["side"] != side or
                        abs(float(st.position_live["qty"]) - qty) > 1e-9):
                        st.position_live = {"side": side, "qty": qty, "entry": entry, "trail": None}
            except Exception:
                continue

    # ----------------- Worker (optional single-threaded helper) -----------------
    def _ws_worker(self):
        if not websocket:
            console.print("[red]請先安裝： pip install websocket-client[/red]")
            return
        self.listen_key = create_listen_key()
        url = f"{WS_URL}/ws/{self.listen_key}"
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        self._ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

    # ----------------- Public controls -----------------
    def start(self):
        if self.thread and self.thread.is_alive():
            console.print("[yellow]UserWS already running[/yellow]")
            return
        if not API_KEY:
            console.print("[dim]UserWS skipped: no API key[/dim]")
            return

        # 先建立 listenKey
        try:
            self.listen_key = create_listen_key()
            console.print("[green]listenKey created[/green]")
        except Exception as e:
            console.print(f"[red]create_listen_key failed: {e}[/red]")
            return

        self.stop_flag.clear()

        # === 主線程：建立 WebSocket ===
        def _run():
            if not websocket:
                console.print("[red]請先安裝： pip install websocket-client[/red]")
                return

            # ✅ 正確的 Futures User Data WS URL（單一 listenKey）
            url = f"{WS_URL}/ws/{self.listen_key}"

            self._ws = websocket.WebSocketApp(
                url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open
            )
            self._ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

        # === 保活線程：定期刷新 listenKey ===
        def _keep():
            while not self.stop_flag.is_set():
                try:
                    time.sleep(LISTENKEY_KEEPALIVE_SEC)
                    keepalive_listen_key(self.listen_key)
                    console.print("[dim]listenKey keepalive OK[/dim]")
                except Exception as e:
                    console.print(f"[red]listenKey keepalive failed: {e}[/red]")

        self.keepalive_thread = threading.Thread(target=_keep, daemon=True)
        self.keepalive_thread.start()

    def stop(self):
        self.stop_flag.set()
        try:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
        except Exception:
            pass

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            self.thread = None

        if self.keepalive_thread and self.keepalive_thread.is_alive():
            self.keepalive_thread.join(timeout=5)
            self.keepalive_thread = None

        console.print("[yellow]UserWS stopped[/yellow]")
        
# ============== 自動刷新幣池（可選） ==============
def auto_refresh_worker(ws_mgr: WSManager, live_obj: Live):
    while True:
        try:
            console.print("[cyan]Refreshing symbol pool...[/cyan]")
            refresh_symbol_pool(n=27, top_volume=True)
            boot_rest_warmup()
            console.print("[green]Symbol pool refreshed[/green]")
            if USE_WEBSOCKET: ws_mgr.restart(SYMBOLS, live_obj)
        except Exception as e:
            console.print(f"[red]auto_refresh_worker error: {e}[/red]")
        time.sleep(120)

# ===================== 啟動 =====================
def _server_time_ms():
    try:
        j = binance_get("/fapi/v1/time")
        return int(j.get("serverTime", 0))
    except Exception:
        return 0

def _preflight_live_or_raise():
    if EXECUTION_MODE.upper() in ("LIVE","BOTH") and not TESTNET:
        if not API_KEY or not API_SECRET:
            raise RuntimeError("LIVE 模式需要 BINANCE_FUTURES_KEY / BINANCE_FUTURES_SECRET")
        _sync_server_time_offset()
        drift = abs(SERVER_TIME_OFFSET_MS)
        if drift > 5000:
            raise RuntimeError(f"本機時間與伺服器相差 {drift} ms，請校時（NTP）")
        _ = binance_signed("GET", "/fapi/v2/account", {"recvWindow": 20000})
        dual = binance_signed("GET", "/fapi/v1/positionSide/dual", {"recvWindow": 20000})
        if str(dual.get("dualSidePosition","false")).lower() == "true":
            binance_signed("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition":"false","recvWindow":20000})
        _ = create_listen_key()
        
def _ensure_symbol_pool():
    """初始化 SYMBOLS 與 SYMAP（尊重你的各種模式開關）。"""
    global SYMBOLS, SYMAP
    # 先確保 SYMAP 存在
    if 'SYMAP' not in globals() or SYMAP is None:
        SYMAP = {}
    # 依模式建立 SYMBOLS
    if USE_SINGLE_MODE:
        SYMBOLS = [SINGLE_SYMBOL]
    elif SCAN_SYMBOLS:
        # 使用設定的固定清單（已在全域常數中）
        SYMBOLS = list(dict.fromkeys([s.upper() for s in SCAN_SYMBOLS]))
    else:
        # 自動隨機清單（取前100大量能池中隨機 n 檔）
        refresh_symbol_pool(n=27, top_volume=True)
        return
    # 幫清單中的每個 symbol 建立狀態
    for s in SYMBOLS:
        if s not in SYMAP:
            SYMAP[s] = SymbolState(s)
    console.print(f"[green]Symbols initialized: {len(SYMBOLS)}[/green]")


def _start_user_stream():
    """啟動 Futures User Data Stream（需要 API_KEY / API_SECRET）。"""
    if not API_KEY or not API_SECRET or TESTNET:
        console.print("[yellow]No (LIVE) API key/secret or in TESTNET -> skip user stream[/yellow]")
        return None
    try:
        mgr = UserWSManager()
        mgr.start()               # 交給 class 內建的啟動/keepalive
        console.print("[green]User data stream started[/green]")
        return mgr
    except Exception as e:
        console.print(f"[red]User stream start failed: {e}[/red]")
        return None


def main():
    ML.load("ml_state.json")  # 啟動時載入既有權重/門檻/樣本計數
    # 啟動時加入：
    add_log(
        f"CSV logging: LOG_TRADES_CSV={os.getenv('LOG_TRADES_CSV','1')}, "
        f"path={TRADES_CSV_PATH}, cwd={os.getcwd()}",
        "dim"
    )
    threading.Thread(target=_key_listener, daemon=True).start()
    threading.Thread(target=housekeeping_worker, daemon=True).start()
    threading.Thread(target=autosave_state_worker, daemon=True).start()
    console.print(f"[dim]ML loaded: n_seen={ML.model.n_seen}  threshold={ML.threshold:.2f}[/dim]")
    threading.Thread(target=_keyboard_worker, daemon=True).start()
    restore_live_positions()
    restore_trades_from_csv("trades.csv")
    restore_sim_state()  # 若你希望 SIM 也接續，建議也載
    refresh_symbol_pool(n=27, top_volume=True)
    global SYMBOLS, SYMAP
    _sync_server_time_offset()
    threading.Thread(target=_server_time_sync_worker, args=(60,), daemon=True).start()
    _preflight_live_or_raise()
    # 啟動時（例如 boot/warmup 完後）
    user_ws = UserWSManager()
    user_ws.start()  # 會自動 keepalive，收到 ORDER_TRADE_UPDATE 時會調用 close_position_one(...)
    
    try:
        ML.load()
    except Exception as e:
        console.print(f"[yellow]ML load skipped: {e}[/yellow]")
    try:
        _sync_server_time_offset()
    except Exception:
        pass

    # 1) 還原 SIM 帳務（餘額 / 歷史交易 / SIM 倉位）
    try:
        restore_sim_state()
    except Exception as e:
        console.print(f"[yellow]restore_sim_state failed: {e}[/yellow]")

    # 2) 初始化幣池與狀態（尊重 USE_SINGLE_MODE/SCAN_SYMBOLS/隨機模式）
    _ensure_symbol_pool()
    if not SYMBOLS:
        console.print("[red]No symbols to watch. Check SINGLE/SCAN settings.[/red]")
        return

    # 3) 熱身歷史K（避免面板第一次渲染空白）
    try:
        boot_rest_warmup()
    except Exception as e:
        console.print(f"[red]Warmup error: {e}[/red]")

    # 4) 日切重設（第一次呼叫會以當前餘額為基準線）
    daily_reset_if_needed()

    # 5) LIVE：恢復交易所倉位 / 同步餘額
    try:
        restore_live_positions()
        sync_live_balance()
    except Exception as e:
        console.print(f"[yellow]Live restore failed: {e}[/yellow]")

    # 6) 啟動自動保存背景執行緒（SIM 持倉/帳務定期落地）
    try:
        threading.Thread(target=autosave_state_worker, daemon=True).start()
    except Exception:
        pass

    # 7) LIVE：逐倉/槓桿校正（有 key 才會動作；失敗自動忽略）
    try:
        ensure_isolated_and_leverage(SYMBOLS, LEVERAGE)
    except Exception:
        pass

    # 8) 面板與 WS 管理器
    layout = build_layout()
    render_layout(layout)
    ws_mgr = WSManager(layout)
    
    # 9) 啟動「交易所帳戶事件流」（只有 LIVE key 才有用）
    user_ws_mgr = _start_user_stream()

    # 10) 啟動持倉同步守護執行緒（1s 輪詢，確保本地/交易所同步）
    def pos_sync_worker():
        while True:
            try:
                sync_live_positions_periodic()
            except Exception as e:
                console.print(f"[dim]pos_sync_worker error: {e}[/dim]")
            time.sleep(1.0)

    threading.Thread(target=pos_sync_worker, daemon=True).start()
    console.print("[cyan]pos_sync_worker started (1s interval)[/cyan]")

    # 11) 進入主循環（行情 WS + 面板渲染 + 自動刷新幣池）
    last_refresh = time.time()
    with Live(layout, refresh_per_second=max(1, REFRESH_FPS), screen=True) as live:
        if USE_WEBSOCKET:
            ws_mgr.start(SYMBOLS, live_obj=live)  # 行情 WS

        try:
            while True:
                # 定時同步 LIVE 餘額與日切檢查
                sync_live_balance()
                daily_reset_if_needed()

                # 定期保存 SIM 狀態
                persist_sim_state(force=False)

                # 自動刷新幣池（預設 30 分鐘）
                if USE_AUTO_REFRESH and (time.time() - last_refresh > 1800):
                    try:
                        refresh_symbol_pool(n=27, top_volume=True)  # 會保留持倉幣
                        boot_rest_warmup()
                        ws_mgr.restart(SYMBOLS, live_obj=live)
                        console.print(f"[cyan]Symbol pool auto-refreshed: {len(SYMBOLS)}[/cyan]")
                    except Exception as e:
                        console.print(f"[yellow]Auto-refresh symbols failed: {e}[/yellow]")
                    last_refresh = time.time()

                # 重繪
                render_layout(layout)
                live.update(layout)
                time.sleep(0.5)
        except KeyboardInterrupt:
            console.print("[yellow]Stopped by user[/yellow]")
        finally:
            # 安全收尾：保存 / 關閉 WS
            try:
                persist_sim_state(force=True)
                ML.save()
            except Exception:
                pass
            try:
                ws_mgr.stop()
            except Exception:
                pass
            try:
                if user_ws_mgr:
                    user_ws_mgr.stop()
            except Exception:
                pass

# === PATCH: 把背景工作統一在這裡啟動（等所有 def 都已載入）===
def _start_background_workers():
    try:
        # 1) 幣安 server 時間同步（校正簽名 timestamp）
        threading.Thread(target=_server_time_sync_worker, args=(60,), daemon=True).start()
    except Exception as e:
        add_log(f"server time sync start fail: {e}", "red")

    try:
        # 2) SIM 自動存檔
        threading.Thread(target=autosave_state_worker, daemon=True).start()
    except Exception as e:
        add_log(f"autosave worker start fail: {e}", "red")

    try:
        # 3) LIVE 倉位 housekeeping（偵測被交易所 TP/SL 的單）
        threading.Thread(target=housekeeping_worker, daemon=True).start()
    except Exception as e:
        add_log(f"housekeeping worker start fail: {e}", "red")

    try:
        # 4) 啟動時還原 SIM 狀態 + LIVE 持倉
        restore_sim_state()
        restore_live_positions()
    except Exception as e:
        add_log(f"restore state fail: {e}", "red")

    try:
        # 5) 初始化幣池（避免畫面空白）
        if not SYMBOLS:
            refresh_symbol_pool(n=27, top_volume=True)
    except Exception as e:
        add_log(f"refresh_symbol_pool fail: {e}", "red")
        
def dynamic_exit_manager_worker():
    while True:
        try:
            dynamic_exit_manager_once()
        except Exception as e:
            add_log(f"dynamic_exit_manager error: {e}", "red")
        time.sleep(3)  # 每 3 秒巡一次（可調）
def ml_confidence_worker(loop_gap=3.0):
    """每隔 loop_gap 秒更新所有 symbol 的 p 值。"""
    while True:
        try:
            for s in list(SYMBOLS):
                st = SYMAP.get(s)
                if not st:
                    continue
                try:
                    st.update_indicators()
                    p = eval_ml_confidence_for_symbol(st)
                    if p is not None:
                        st.ml_p = p
                        st.ml_p_ts = utc_ms()
                except Exception:
                    continue
        except Exception as e:
            add_log(f"ml_confidence_worker error: {e}", "red")
        time.sleep(loop_gap)

if __name__ == "__main__":
    _start_background_workers()
    threading.Thread(target=dynamic_exit_manager_worker, daemon=True).start()
    # 啟動：AI 信心背景計算
    threading.Thread(target=ml_confidence_worker, daemon=True).start()
    try:
        main()
    except KeyboardInterrupt:
        console.print("[yellow]Stopped by user[/yellow]")
    finally:
        try:
            persist_sim_state(force=True)
            ML.save()  # 🧠 確保機器學習模型即時存檔
            console.print("[green]Final save completed[/green]")
        except Exception as e:
            console.print(f"[red]Final save failed: {e}[/red]")
