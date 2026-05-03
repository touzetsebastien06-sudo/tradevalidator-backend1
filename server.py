"""
TradeValidator Pro - Backend Cloud v1.0
Deployé sur Railway — accessible depuis Vercel
"""
import os
import json
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "d7prfc1r01qosaapn1cgd7prfc1r01qosaapn1d0")
TWELVE_KEY  = os.environ.get("TWELVE_KEY",  "ac36438cc83248c3bfb2ea719aadeba0")

FTMO_CONFIG = {
    "compte":        10000,
    "objectif_pct":  0.10,
    "max_loss_pct":  0.10,
    "daily_loss_pct":0.05,
    "risque_trade":  100,
    "min_rr":        3.0,
    "min_score":     10,
}

FTMO_TICKERS = ["CL=F","GC=F","GOOG","TSLA","^FCHI","^GDAXI"]

# Cache en memoire (remplace les fichiers locaux)
_cache        = {}
_ftmo_state   = {"pnl_total":0,"pnl_jour":0,"nb_trades":0,"nb_wins":0,
                  "consecutifs_perdus":0,"challenge_actif":False,
                  "date_debut":"","jours_trading":0,"date_dernier_trade":""}
_journal      = []
_cache_lock   = threading.Lock()

YAHOO_ONLY = {"^GDAXI","^FCHI","^FTSE","^IBEX","^STOXX50E","^N225","^HSI","^AXJO","^RUT","^IXIC"}


# ─── ROUTES ───────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status":"ok","version":"1.0","time":datetime.now().isoformat()})

@app.route("/api/health")
def api_health():
    return jsonify({"status":"ok"})

@app.route("/api/analyze", methods=["POST"])
def analyze():
    data   = request.json or {}
    ticker = data.get("ticker","").strip().upper()
    mode   = data.get("mode","swing")
    if not ticker:
        return jsonify({"error":"Ticker manquant"}), 400
    try:
        result = run_analysis(ticker, mode)
        with _cache_lock:
            _cache[f"{ticker}_{mode}"] = result
        return jsonify(result)
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/scan_ftmo")
def scan_ftmo():
    mode    = request.args.get("mode","swing")
    results = []
    for ticker in FTMO_TICKERS:
        try:
            r = run_analysis(ticker, mode)
            results.append(r)
            with _cache_lock:
                _cache[f"{ticker}_{mode}"] = r
        except Exception as e:
            results.append({"ticker":ticker,"error":str(e)[:50],"trend":"neutral","ftmo_ok":False})
    return jsonify(results)

@app.route("/api/ftmo", methods=["GET"])
def get_ftmo():
    state = dict(_ftmo_state)
    state["objectif_euros"]  = FTMO_CONFIG["compte"] * FTMO_CONFIG["objectif_pct"]
    state["max_loss_euros"]  = FTMO_CONFIG["compte"] * FTMO_CONFIG["max_loss_pct"]
    state["daily_loss_euros"]= FTMO_CONFIG["compte"] * FTMO_CONFIG["daily_loss_pct"]
    state["progression_pct"] = state["pnl_total"] / state["objectif_euros"] * 100
    state["win_rate"]        = state["nb_wins"]/state["nb_trades"]*100 if state["nb_trades"]>0 else 0
    return jsonify(state)

@app.route("/api/ftmo/trade", methods=["POST"])
def record_trade():
    data   = request.json or {}
    euros  = float(data.get("euros",0))
    ticker = data.get("ticker","")
    today  = datetime.now().strftime("%Y-%m-%d")

    if _ftmo_state["date_dernier_trade"] != today:
        _ftmo_state["pnl_jour"] = 0
    _ftmo_state["date_dernier_trade"] = today
    _ftmo_state["pnl_total"]  += euros
    _ftmo_state["pnl_jour"]   += euros
    _ftmo_state["nb_trades"]  += 1
    if euros > 0:
        _ftmo_state["nb_wins"] += 1
        _ftmo_state["consecutifs_perdus"] = 0
    else:
        _ftmo_state["consecutifs_perdus"] += 1
    if not _ftmo_state["challenge_actif"]:
        _ftmo_state["challenge_actif"] = True
        _ftmo_state["date_debut"] = today
    _journal.append({
        "id":       len(_journal)+1,
        "ticker":   ticker,
        "euros":    euros,
        "statut":   "GAGNE" if euros>0 else "PERDU",
        "date":     datetime.now().strftime("%d/%m/%Y %H:%M"),
    })
    return jsonify({"ok":True,"pnl_total":_ftmo_state["pnl_total"]})

@app.route("/api/ftmo/reset", methods=["POST"])
def reset_ftmo():
    for k in ["pnl_total","pnl_jour","nb_trades","nb_wins","consecutifs_perdus","jours_trading"]:
        _ftmo_state[k] = 0
    _ftmo_state["challenge_actif"] = False
    _ftmo_state["date_debut"] = ""
    _journal.clear()
    return jsonify({"ok":True})

@app.route("/api/journal")
def get_journal():
    return jsonify(list(reversed(_journal[-50:])))

@app.route("/api/price/<ticker>")
def get_price(ticker):
    """Prix temps reel d un ticker."""
    try:
        price, source = get_best_price(ticker.upper())
        return jsonify({"ticker":ticker,"price":price,"source":source})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


# ─── ANALYSE ─────────────────────────────────────────────────────────────────
def run_analysis(ticker: str, mode: str = "swing") -> dict:
    import yfinance as yf
    import pandas as pd
    import numpy as np

    # Timeframes
    if mode == "day":
        tf_h, tf_l = "1h", "15m"
        p_h,  p_l  = "3mo","1mo"
    else:
        tf_h, tf_l = "1d", "4h"
        p_h,  p_l  = "2y", "6mo"

    # Charger les donnees
    df_h = get_ohlcv(ticker, tf_h, p_h)
    df_l = get_ohlcv(ticker, tf_l, p_l)

    if df_h.empty or df_l.empty:
        return {"ticker":ticker,"error":"Données indisponibles","trend":"neutral",
                "zones":[],"ftmo_ok":False}

    # Indicateurs
    df_h = calc_indicators(df_h, mode)
    df_l = calc_indicators(df_l, mode)

    # Tendance
    trend = get_trend(df_h, mode)

    # Prix
    price = float(df_l["Close"].iloc[-1])
    try:
        best_p, src = get_best_price(ticker)
        if best_p > 0: price = best_p
    except: pass

    # Info ticker
    name = ticker
    currency = "USD"
    try:
        info = yf.Ticker(ticker).info
        name     = info.get("shortName") or info.get("longName") or ticker
        currency = info.get("currency","USD")
    except: pass

    # Marche ouvert
    market_open = is_market_open(ticker)

    # Earnings
    earnings = get_earnings_info(ticker)

    # Zones
    zones_json = []
    if trend != "neutral":
        direction = "long" if trend=="bullish" else "short"
        zones = detect_zones(df_h, df_l, trend, direction)
        for z in zones[:3]:
            sc   = z.get("score",0)
            rr2  = z.get("rr2",0) or 0
            rr1  = z.get("rr1",0) or 0
            fok  = sc>=10 and rr2>=3.0
            pct  = round(sc/14*100)
            vd   = "EXCELLENT" if sc>=13 else "FORT" if sc>=10 else "ACCEPTABLE" if sc>=7 else "FAIBLE"
            vc   = "#00ff88" if sc>=13 else "#00C851" if sc>=10 else "#FF8800" if sc>=7 else "#FF4444"
            zones_json.append({
                "direction":   z.get("direction",""),
                "center":      z.get("center",0),
                "sl":          z.get("sl",0),
                "tp1":         z.get("tp1",0),
                "tp2":         z.get("tp2",0),
                "rr1":         round(rr1,2),
                "rr2":         round(rr2,2),
                "rr_valid":    rr2>=2.0,
                "score":       sc,
                "score_max":   14,
                "score_total": sc,
                "verdict":     vd,
                "verdict_color":vc,
                "confluences": z.get("confluences",[])[:4],
                "nb_conf":     len(z.get("confluences",[])),
                "ftmo_ok":     fok,
                "pct":         pct,
            })

    best_score = zones_json[0]["score"] if zones_json else 0
    best_rr    = zones_json[0]["rr2"]   if zones_json else 0
    ftmo_ok    = best_score>=10 and best_rr>=3.0 and market_open and not earnings.get("danger",False)

    return {
        "ticker":       ticker,
        "name":         name,
        "price":        price,
        "currency":     currency,
        "trend":        trend,
        "direction":    "long" if trend=="bullish" else "short" if trend=="bearish" else None,
        "mode":         mode,
        "zones":        zones_json,
        "best_score":   best_score,
        "best_rr":      round(best_rr,2),
        "ftmo_ok":      ftmo_ok,
        "market_status":{"is_open":market_open,
                         "label":"OUVERT" if market_open else "FERME",
                         "emoji":"🟢" if market_open else "🔴",
                         "color":"#00C851" if market_open else "#FF4444"},
        "earnings":     earnings,
        "timestamp":    datetime.now().strftime("%H:%M:%S"),
    }


# ─── INDICATEURS ─────────────────────────────────────────────────────────────
def calc_indicators(df, mode="swing"):
    import numpy as np
    if df.empty or len(df) < 20: return df

    c = df["Close"]
    h = df["High"]
    l = df["Low"]

    # MMA
    ma_period = 50 if mode=="day" else 100
    df["ma_trend"] = c.rolling(ma_period, min_periods=1).mean()
    df["ema20"]    = c.ewm(span=20, adjust=False).mean()

    # Bollinger
    ma20  = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_upper"]    = ma20 + 2*std20
    df["bb_lower"]    = ma20 - 2*std20
    df["bb_position"] = ((c - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])).clip(0,1)

    # ATR
    tr = np.maximum(h-l, np.maximum(abs(h-c.shift(1)), abs(l-c.shift(1))))
    df["atr"] = pd.Series(tr, index=df.index).rolling(14).mean()

    # SuperTrend
    atr14 = df["atr"]
    hl2   = (h+l)/2
    df["st_upper"] = hl2 + 3*atr14
    df["st_lower"] = hl2 - 3*atr14
    df["supertrend_dir"] = "bullish"
    for i in range(1, len(df)):
        if c.iloc[i] > df["st_upper"].iloc[i-1]:
            df.iloc[i, df.columns.get_loc("supertrend_dir")] = "bullish"
        elif c.iloc[i] < df["st_lower"].iloc[i-1]:
            df.iloc[i, df.columns.get_loc("supertrend_dir")] = "bearish"
        else:
            df.iloc[i, df.columns.get_loc("supertrend_dir")] = \
                df["supertrend_dir"].iloc[i-1]

    # MACD
    ema12 = c.ewm(span=12,adjust=False).mean()
    ema26 = c.ewm(span=26,adjust=False).mean()
    macd  = ema12 - ema26
    df["macd_trend"] = ["bullish" if v>0 else "bearish" for v in macd]

    return df


def get_trend(df, mode="swing"):
    if df.empty or len(df) < 5: return "neutral"
    last = df.iloc[-1]
    price = float(last["Close"])
    ma    = float(last.get("ma_trend", price))
    st    = str(last.get("supertrend_dir",""))

    ma_bull = price > ma * 1.001
    ma_bear = price < ma * 0.999

    if ma_bull:
        return "bullish"
    elif ma_bear:
        return "bearish"
    else:
        if st == "bullish": return "bullish"
        if st == "bearish": return "bearish"
        return "neutral"


# ─── ZONES ────────────────────────────────────────────────────────────────────
def detect_zones(df_h, df_l, trend, direction):
    import numpy as np
    if df_l.empty or len(df_l) < 20:
        return []

    price = float(df_l["Close"].iloc[-1])
    atr   = float(df_l["atr"].iloc[-1]) if "atr" in df_l.columns else price*0.01

    # Supports et resistances
    highs = df_h["High"].values[-100:]
    lows  = df_h["Low"].values[-100:]
    pivots_h = [highs[i] for i in range(5,len(highs)-5)
                if highs[i]==max(highs[i-5:i+6])]
    pivots_l = [lows[i]  for i in range(5,len(lows)-5)
                if lows[i]==min(lows[i-5:i+6])]

    supports    = sorted([l for l in pivots_l if l < price*0.998], reverse=True)[:5]
    resistances = sorted([h for h in pivots_h if h > price*1.002])[:5]

    last = df_l.iloc[-1]
    zones = []

    def make_zone(center):
        # SL / TP
        if direction == "long":
            nearby_s = [s for s in supports if s < center]
            sl  = (nearby_s[0] - atr*0.3) if nearby_s else center - atr*1.5
            if sl >= center: sl = center - atr*1.5
            risk = center - sl
            nearby_r = [r for r in resistances if r > center]
            tp1 = nearby_r[0] if nearby_r and (nearby_r[0]-center)/risk>=1.5 else center+risk*1.5
            tp2 = nearby_r[1] if len(nearby_r)>1 and nearby_r[1]>tp1 else center+risk*3.0
            if tp2<=tp1: tp2=tp1+risk
        else:
            nearby_r = [r for r in resistances if r > center]
            sl  = (nearby_r[0] + atr*0.3) if nearby_r else center + atr*1.5
            if sl <= center: sl = center + atr*1.5
            risk = sl - center
            nearby_s = [s for s in supports if s < center]
            tp1 = nearby_s[0] if nearby_s and (center-nearby_s[0])/risk>=1.5 else center-risk*1.5
            tp2 = nearby_s[1] if len(nearby_s)>1 and nearby_s[1]<tp1 else center-risk*3.0
            if tp2>=tp1: tp2=tp1-risk

        if risk <= 0: return None
        rr1 = abs(tp1-center)/risk
        rr2 = abs(tp2-center)/risk

        # Score simplifie
        score = 0
        bb_pos = float(last.get("bb_position",0.5))
        st_dir = str(last.get("supertrend_dir",""))
        ma_dir = str(last.get("ma_trend",0))
        macd_d = str(last.get("macd_trend",""))

        # S/R (3 pts)
        near_sr = min([abs(center-l) for l in supports+resistances], default=999)
        if near_sr < atr*0.5:   score+=3
        elif near_sr < atr*1.5: score+=2
        elif near_sr < atr*3:   score+=1

        # Tendance MMA (3 pts)
        if direction=="long"  and float(df_l["Close"].iloc[-1]) > float(df_l.get("ma_trend",df_l["Close"]).iloc[-1]): score+=3
        elif direction=="short"and float(df_l["Close"].iloc[-1]) < float(df_l.get("ma_trend",df_l["Close"]).iloc[-1]): score+=3

        # Bollinger (2 pts)
        if direction=="long"  and bb_pos < 0.25: score+=2
        elif direction=="short"and bb_pos > 0.75: score+=2
        elif direction=="long"  and bb_pos < 0.40: score+=1
        elif direction=="short"and bb_pos > 0.60: score+=1

        # SuperTrend (1 pt)
        if (direction=="long" and st_dir=="bullish") or \
           (direction=="short"and st_dir=="bearish"): score+=1

        # MACD (1 pt)
        if (direction=="long" and macd_d=="bullish") or \
           (direction=="short"and macd_d=="bearish"): score+=1

        # Macro (2 pts par defaut — pas de macro en cloud)
        score += 2

        # Fibonacci (2 pts)
        try:
            swing_l = min(df_h["Low"].values[-50:])
            swing_h = max(df_h["High"].values[-50:])
            if swing_h > swing_l:
                fib50 = swing_h - (swing_h-swing_l)*0.5
                fib618= swing_h - (swing_h-swing_l)*0.618
                fz_low,fz_high = min(fib50,fib618),max(fib50,fib618)
                if fz_low <= center <= fz_high: score+=2
                elif abs(center-fib50)<atr or abs(center-fib618)<atr: score+=1
        except: pass

        # Confluences
        conf = []
        if near_sr < atr:       conf.append("Support/Résistance clé")
        if direction=="long"  and bb_pos<0.25: conf.append("Bollinger survente")
        if direction=="short" and bb_pos>0.75: conf.append("Bollinger surachat")
        if (direction=="long" and st_dir=="bullish") or (direction=="short" and st_dir=="bearish"):
            conf.append("SuperTrend confirmé")
        if (direction=="long" and macd_d=="bullish") or (direction=="short" and macd_d=="bearish"):
            conf.append("MACD ZL haussier" if direction=="long" else "MACD ZL baissier")
        conf.append("MMA " + ("haussière" if direction=="long" else "baissière"))

        return {
            "direction":  "ACHAT" if direction=="long" else "VENTE",
            "center":     round(center,4),
            "sl":         round(sl,4),
            "tp1":        round(tp1,4),
            "tp2":        round(tp2,4),
            "rr1":        round(rr1,2),
            "rr2":        round(rr2,2),
            "score":      min(score,14),
            "confluences":conf,
        }

    # Zone au prix actuel
    z = make_zone(price)
    if z: zones.append(z)

    # Zones sur S/R proches
    levels = supports if direction=="long" else resistances
    for level in levels[:3]:
        if 0.01 < abs(level-price)/price < 0.15:
            z = make_zone(level)
            if z and z["center"] != zones[0]["center"] if zones else True:
                zones.append(z)

    # Zone Bollinger
    bb_level = float(last.get("bb_lower",0)) if direction=="long" else float(last.get("bb_upper",0))
    if bb_level > 0 and ((direction=="long" and bb_level<price) or (direction=="short" and bb_level>price)):
        z = make_zone(bb_level)
        if z: zones.append(z)

    # Dedupliquer + trier par score
    seen,unique=[],[]
    for z in zones:
        k=round(z["center"],1)
        if k not in seen: seen.append(k); unique.append(z)
    return sorted(unique, key=lambda x: x["score"], reverse=True)[:3]


# ─── PRIX ────────────────────────────────────────────────────────────────────
def get_ohlcv(ticker, interval, period):
    import yfinance as yf
    import pandas as pd
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
        df.dropna(inplace=True)
        return df
    except:
        return pd.DataFrame()


def get_best_price(ticker):
    import yfinance as yf
    # Indices : Yahoo uniquement
    if ticker in YAHOO_ONLY:
        try:
            info = yf.Ticker(ticker).info
            p = float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
            if p > 0: return p, "yahoo"
        except: pass
        return 0.0, "none"
    # Autres : Finnhub en priorite
    try:
        import requests as req
        fh = TICKER_MAP_FINNHUB.get(ticker, ticker)
        r  = req.get(f"https://finnhub.io/api/v1/quote",
                     params={"symbol":fh,"token":FINNHUB_KEY}, timeout=4)
        if r.status_code==200:
            p = float(r.json().get("c",0))
            if p > 0: return p, "finnhub"
    except: pass
    # Fallback Yahoo
    try:
        info = yf.Ticker(ticker).info
        p = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        if p > 0: return p, "yahoo"
    except: pass
    return 0.0, "none"


TICKER_MAP_FINNHUB = {
    "GC=F":"OANDA:XAUUSD","CL=F":"OANDA:USOIL","BZ=F":"OANDA:UKOIL",
    "EURUSD=X":"OANDA:EURUSD","GBPUSD=X":"OANDA:GBPUSD","USDJPY=X":"OANDA:USDJPY",
    "BTC-USD":"BINANCE:BTCUSDT","ETH-USD":"BINANCE:ETHUSDT","SOL-USD":"BINANCE:SOLUSDT",
}


def is_market_open(ticker):
    from datetime import datetime
    import pytz
    now = datetime.now(pytz.UTC)
    wd  = now.weekday()
    if wd >= 5: return False
    t = ticker.upper()
    if "-USD" in t or "=F" in t: return True  # Crypto/Futures H24
    if t.endswith("=X"): return True           # Forex H24
    if t in {"^FCHI","^STOXX50E","^IBEX","MC.PA","AF.PA"}:
        paris = now.astimezone(pytz.timezone("Europe/Paris"))
        return 9 <= paris.hour < 17 or (paris.hour==17 and paris.minute<=30)
    if t in {"^GDAXI","VOW3.DE"}:
        berlin = now.astimezone(pytz.timezone("Europe/Berlin"))
        return 9 <= berlin.hour < 17 or (berlin.hour==17 and berlin.minute<=30)
    # US par defaut
    ny = now.astimezone(pytz.timezone("America/New_York"))
    return (ny.hour==9 and ny.minute>=30) or (10<=ny.hour<=15) or (ny.hour==16 and ny.minute==0)


def get_earnings_info(ticker):
    t = ticker.upper()
    if t.endswith("=X") or t.endswith("=F") or "-USD" in t or t.startswith("^"):
        return {"has_earnings":False}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        ts   = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if ts:
            from datetime import datetime, timedelta
            d    = datetime.fromtimestamp(int(ts))
            days = (d - datetime.now()).days
            if 0 <= days <= 7:
                return {"has_earnings":True,"days_remaining":days,
                        "badge":f"⚠️ Earnings dans {days}j","danger":True,
                        "color":"#FF4444"}
            elif 0 <= days <= 14:
                return {"has_earnings":True,"days_remaining":days,
                        "badge":f"~ Earnings dans {days}j","danger":False,
                        "color":"#FF8800"}
    except: pass
    return {"has_earnings":False,"danger":False}


# ─── LANCEMENT ────────────────────────────────────────────────────────────────
def keep_alive():
    """Ping le serveur toutes les 14 minutes pour eviter l endormissement sur Render."""
    import time, requests as req
    time.sleep(60)
    while True:
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL","")
            if url:
                req.get(url+"/api/health", timeout=10)
        except: pass
        time.sleep(14*60)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Lancer le keep-alive en arriere-plan sur Render
    if os.environ.get("RENDER"):
        t = threading.Thread(target=keep_alive, daemon=True)
        t.start()
    app.run(host="0.0.0.0", port=port, debug=False)
