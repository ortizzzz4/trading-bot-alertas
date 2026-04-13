"""
Bot de Alertas de Trading v7
- Acciones/ETF: RSI + EMA + Volumen
- Forex: SMC (BOS, CHoCH, Order Block, FVG, Liquidity Sweep)
- Oro: MA Cross SMA50/SMA100 (estrategia propia)
- BTC: RSI + Soporte/Resistencia
- TODOS: Precio entrada, Stop Loss y Take Profit exactos
"""
import requests, pandas as pd, numpy as np
import smtplib, time, os, random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

EMAIL_REMITENTE  = os.environ.get("EMAIL_REMITENTE",  "tu_correo@gmail.com")
EMAIL_CONTRASENA = os.environ.get("EMAIL_CONTRASENA", "xxxx xxxx xxxx xxxx")
EMAIL_DESTINO    = os.environ.get("EMAIL_DESTINO",    "tu_correo@gmail.com")
INTERVALO_MIN    = int(os.environ.get("INTERVALO_MINUTOS", "30"))

# ── Timeframes y periodos ────────────────────────────────────
FOREX_INTERVALO = os.environ.get("FOREX_INTERVALO", "1h")
FOREX_PERIODO   = int(os.environ.get("FOREX_PERIODO_DIAS", "30"))

INTERVALOS_MERCADO = {
    "FOREX":  {"tf": FOREX_INTERVALO, "dias": FOREX_PERIODO, "cada_min": 60},
    "ORO":    {"tf": "1d", "dias": 120, "cada_min": 240},   # cada 4h dentro de sesiones
    "CRYPTO": {"tf": "1d", "dias": 90,  "cada_min": 30},
    "ACCION": {"tf": "1d", "dias": 90,  "cada_min": 1440},  # 1x al dia, solo en sesion NY
    "ETF":    {"tf": "1d", "dias": 90,  "cada_min": 1440},  # 1x al dia, solo en sesion NY
}

_ultimo_chequeo = {}

# ─────────────────────────────────────────────────────────────
# GESTOR DE TRADES ACTIVOS
# Guarda el estado de cada operacion abierta en memoria
# Estructura: { "BTC-USD": { "direccion", "entrada", "sl", "tp", "hora", "senales" } }
# ─────────────────────────────────────────────────────────────
_trades_activos = {}

def hay_trade_activo(sym):
    return sym in _trades_activos

def abrir_trade(sym, resultado):
    """Guarda un trade nuevo como activo."""
    _trades_activos[sym] = {
        "direccion": resultado["direccion"],
        "entrada":   resultado["niveles"]["entrada"],
        "sl":        resultado["niveles"]["sl"],
        "tp":        resultado["niveles"]["tp"],
        "sl_pct":    resultado["niveles"]["sl_pct"],
        "tp_pct":    resultado["niveles"]["tp_pct"],
        "rr":        resultado["niveles"]["rr"],
        "hora":      resultado["hora"],
        "nombre":    resultado["nombre"],
        "cat":       resultado["cat"],
        "senales":   resultado["senales"],
        "max_precio": resultado["niveles"]["entrada"],  # para trailing
        "min_precio": resultado["niveles"]["entrada"],
    }

def cerrar_trade(sym):
    """Elimina un trade activo."""
    if sym in _trades_activos:
        del _trades_activos[sym]

def verificar_trades_activos(resultados_nuevos):
    """
    Para cada trade activo:
    1. Verifica si el precio actual toco SL o TP
    2. Verifica si la señal cambio de direccion
    Retorna lista de eventos (cierres, cambios de direccion)
    """
    eventos = []
    # Mapa rapido de precio actual por simbolo
    precios = {r["sym"]: r["precio"] for r in resultados_nuevos}
    # Tambien necesitamos precios de activos SIN señal nueva
    syms_sin_precio = [s for s in list(_trades_activos.keys()) if s not in precios]

    for sym in list(_trades_activos.keys()):
        trade = _trades_activos[sym]
        precio_actual = precios.get(sym)
        if precio_actual is None:
            continue

        dir_trade = trade["direccion"]
        entrada   = trade["entrada"]
        sl        = trade["sl"]
        tp        = trade["tp"]
        ganancia_pct = ((precio_actual - entrada) / entrada * 100) if dir_trade == "COMPRA" else ((entrada - precio_actual) / entrada * 100)

        # ── Verificar SL tocado ──
        if dir_trade == "COMPRA" and precio_actual <= sl:
            eventos.append({
                "tipo": "SL_TOCADO", "sym": sym, "trade": trade,
                "precio_cierre": precio_actual,
                "resultado_pct": -trade["sl_pct"],
                "ganancia_usd": precio_actual - entrada,
                "mensaje": f"🛑 STOP LOSS TOCADO — Perdida de {trade['sl_pct']}%"
            })
            cerrar_trade(sym)

        elif dir_trade == "VENTA" and precio_actual >= sl:
            eventos.append({
                "tipo": "SL_TOCADO", "sym": sym, "trade": trade,
                "precio_cierre": precio_actual,
                "resultado_pct": -trade["sl_pct"],
                "ganancia_usd": entrada - precio_actual,
                "mensaje": f"🛑 STOP LOSS TOCADO — Perdida de {trade['sl_pct']}%"
            })
            cerrar_trade(sym)

        # ── Verificar TP tocado ──
        elif dir_trade == "COMPRA" and precio_actual >= tp:
            eventos.append({
                "tipo": "TP_TOCADO", "sym": sym, "trade": trade,
                "precio_cierre": precio_actual,
                "resultado_pct": trade["tp_pct"],
                "ganancia_usd": precio_actual - entrada,
                "mensaje": f"✅ TAKE PROFIT ALCANZADO — Ganancia de {trade['tp_pct']}%"
            })
            cerrar_trade(sym)

        elif dir_trade == "VENTA" and precio_actual <= tp:
            eventos.append({
                "tipo": "TP_TOCADO", "sym": sym, "trade": trade,
                "precio_cierre": precio_actual,
                "resultado_pct": trade["tp_pct"],
                "ganancia_usd": entrada - precio_actual,
                "mensaje": f"✅ TAKE PROFIT ALCANZADO — Ganancia de {trade['tp_pct']}%"
            })
            cerrar_trade(sym)

    return eventos

def filtrar_señales_nuevas(resultados):
    """
    De la lista de resultados, devuelve solo los que son realmente NUEVOS:
    - No hay trade activo para ese simbolo, O
    - La direccion cambio (ej: teniamos COMPRA y ahora es VENTA)
    """
    nuevos = []
    for r in resultados:
        sym = r["sym"]
        if sym not in _trades_activos:
            nuevos.append(r)
            abrir_trade(sym, r)
        else:
            trade_actual = _trades_activos[sym]
            # Cambio de direccion — cerrar el anterior y abrir nuevo
            if trade_actual["direccion"] != r["direccion"]:
                print(f"  🔄 {sym}: Direccion cambio {trade_actual['direccion']} → {r['direccion']}")
                cerrar_trade(sym)
                nuevos.append(r)
                abrir_trade(sym, r)
            else:
                print(f"  ⏸️  {sym}: Trade activo ({trade_actual['direccion']}) — esperando SL/TP")
    return nuevos

# ── Sesiones de mercado ───────────────────────────────────────
from datetime import timezone, timedelta

TZ_ET  = timezone(timedelta(hours=-4))   # Eastern Time (UTC-4 verano, -5 invierno)
TZ_HKT = timezone(timedelta(hours=+8))   # Hong Kong Time (Asia)

def en_sesion_ny():
    """True si estamos en horario de mercado de Nueva York (9:30-16:00 ET, L-V)."""
    ahora_et = datetime.now(TZ_ET)
    if ahora_et.weekday() >= 5:   # sabado=5, domingo=6
        return False
    hora = ahora_et.hour + ahora_et.minute / 60
    return 9.5 <= hora <= 16.0

def en_sesion_oro():
    """
    Sesiones del Oro:
    - Asia:    08:00-16:00 HKT = 00:00-08:00 UTC  → revisa cada 4h, TF diario
    - NY peak: 06:00-10:00 ET  = 10:00-14:00 UTC  → revisa cada 30min, TF 30min (mas volatil)
    - NY resto:10:00-17:00 ET  = 14:00-21:00 UTC  → revisa cada 4h, TF diario
    Retorna: (activo: bool, tf: str, cada_min: int, nombre_sesion: str)
    """
    ahora_utc = datetime.now(timezone.utc)
    if ahora_utc.weekday() >= 5:
        return False, "1d", 240, "Fin de semana"
    hora_utc = ahora_utc.hour + ahora_utc.minute / 60
    # Sesion Asia: 00:00-08:00 UTC
    if 0 <= hora_utc < 8:
        return True, "1d", 240, "🌏 Asia"
    # NY Peak (alta volatilidad): 10:00-14:00 UTC = 06:00-10:00 ET
    if 10 <= hora_utc < 14:
        return True, "30m", 30, "🗽 NY Peak (alta volatilidad)"
    # NY resto: 14:00-21:00 UTC
    if 14 <= hora_utc < 21:
        return True, "1d", 240, "🗽 NY"
    return False, "1d", 240, "Fuera de sesion"

def sesion_activa(cat):
    """Devuelve True si el mercado de esa categoria esta activo ahora."""
    if cat in ("ACCION", "ETF"):
        return en_sesion_ny()
    if cat == "ORO":
        activo, tf, cada, nombre = en_sesion_oro()
        return activo
    return True  # FOREX y CRYPTO corren 24h

def config_oro_dinamica():
    """Retorna tf y cada_min del oro segun la sesion actual."""
    activo, tf, cada, nombre = en_sesion_oro()
    return activo, tf, cada, nombre

# SL y TP en % fijo por mercado
SL_TP = {
    "ACCION": {"sl": 2.0,  "tp": 4.0},   # R:R 1:2
    "ETF":    {"sl": 1.5,  "tp": 3.0},   # R:R 1:2
    "FOREX":  {"sl": 0.5,  "tp": 1.0},   # R:R 1:2
    "ORO":    {"sl": 1.0,  "tp": 2.0},   # R:R 1:2
    "CRYPTO": {"sl": 5.0,  "tp": 10.0},  # R:R 1:2
}

ACCIONES = ["AAPL","NVDA","TSLA","MSFT","AMZN","GOOGL","META"]
ETFS     = ["VTI","VOO","BND","SDY"]
FOREX    = ["EURUSD=X","USDJPY=X","USDCAD=X","AUDUSD=X"]
ORO      = ["GC=F"]
CRYPTO   = ["BTC-USD"]
TODOS    = ACCIONES + ETFS + FOREX + ORO + CRYPTO

NOMBRES = {
    "GC=F":"XAUUSD — Oro","BTC-USD":"BTC/USD — Bitcoin",
    "EURUSD=X":"EUR/USD","USDJPY=X":"USD/JPY",
    "USDCAD=X":"USD/CAD","AUDUSD=X":"AUD/USD",
    "VTI":"VTI — ETF Mercado Total","VOO":"VOO — ETF S&P500",
    "BND":"BND — ETF Bonos","SDY":"SDY — ETF Dividendos",
}

def categoria(s):
    if s in FOREX:  return "FOREX"
    if s in ORO:    return "ORO"
    if s in CRYPTO: return "CRYPTO"
    if s in ETFS:   return "ETF"
    return "ACCION"

ICONOS = {"ACCION":"📈","ETF":"🗂️","FOREX":"💱","ORO":"🥇","CRYPTO":"₿"}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]
SES = requests.Session()

def get_crumb():
    try:
        h={"User-Agent":random.choice(UA),"Accept":"text/html,*/*","Accept-Language":"en-US,en;q=0.5"}
        SES.get("https://finance.yahoo.com",headers=h,timeout=15)
        r=SES.get("https://query1.finance.yahoo.com/v1/test/getcrumb",headers=h,timeout=10)
        c=r.text.strip()
        return c if c and len(c)>3 else None
    except: return None

def get_datos(sym, cr=None, tf="1d", dias=120):
    now=int(time.time()); ini=now-(dias*24*3600)
    for i in range(3):
        try:
            h={"User-Agent":random.choice(UA),"Accept":"application/json,*/*",
               "Accept-Language":"en-US,en;q=0.9","Referer":f"https://finance.yahoo.com/quote/{sym}"}
            srv=["query1","query2"][i%2]
            url=f"https://{srv}.finance.yahoo.com/v8/finance/chart/{sym}?period1={ini}&period2={now}&interval={tf}&includeAdjustedClose=true"
            if cr: url+=f"&crumb={cr}"
            resp=SES.get(url,headers=h,timeout=25)
            if not resp.text.strip() or resp.status_code!=200: time.sleep(3);continue
            d=resp.json(); res=d.get("chart",{}).get("result",[])
            if not res: time.sleep(3);continue
            r=res[0]; ts=r.get("timestamp",[])
            cl=r.get("indicators",{}).get("adjclose",[{}])[0].get("adjclose",[])
            hi=r.get("indicators",{}).get("quote",[{}])[0].get("high",[])
            lo=r.get("indicators",{}).get("quote",[{}])[0].get("low",[])
            vo=r.get("indicators",{}).get("quote",[{}])[0].get("volume",[])
            op=r.get("indicators",{}).get("quote",[{}])[0].get("open",[])
            if not ts or not cl: continue
            n=min(len(ts),len(cl),len(hi) if hi else len(cl),len(lo) if lo else len(cl))
            df=pd.DataFrame({
                "timestamp":ts[:n],"Close":cl[:n],
                "High":hi[:n] if hi else cl[:n],
                "Low":lo[:n] if lo else cl[:n],
                "Open":op[:n] if op else cl[:n],
                "Volume":vo[:n] if vo else [None]*n
            }).dropna(subset=["Close"])
            df["Date"]=pd.to_datetime(df["timestamp"],unit="s")
            return df.set_index("Date").sort_index()
        except:
            if i<2: time.sleep(4+i*2)
    return None

def calc_rsi(s, n=14):
    d=s.diff(); g=d.where(d>0,0.0); p=-d.where(d<0,0.0)
    return 100-(100/(1+(g.rolling(n).mean()/p.rolling(n).mean())))

def calc_atr(df, n=14):
    """Average True Range — mide la volatilidad real del activo."""
    h=df["High"]; l=df["Low"]; c=df["Close"]
    tr=pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

# ─────────────────────────────────────────────────────────────
# CALCULAR ENTRADA, SL y TP
# ─────────────────────────────────────────────────────────────
def calc_niveles(precio, direccion, cat, atr_val=None):
    """
    Calcula precio de entrada, stop loss y take profit.
    Usa % fijo por mercado. Si hay ATR disponible, lo usa para SL mas preciso.
    """
    sl_pct = SL_TP[cat]["sl"] / 100
    tp_pct = SL_TP[cat]["tp"] / 100

    entrada = precio

    if direccion == "COMPRA":
        sl = round(entrada * (1 - sl_pct), 5)
        tp = round(entrada * (1 + tp_pct), 5)
    else:  # VENTA
        sl = round(entrada * (1 + sl_pct), 5)
        tp = round(entrada * (1 - tp_pct), 5)

    rr = tp_pct / sl_pct
    return {"entrada": entrada, "sl": sl, "tp": tp, "rr": rr,
            "sl_pct": SL_TP[cat]["sl"], "tp_pct": SL_TP[cat]["tp"]}

# ─────────────────────────────────────────────────────────────
# ESTRATEGIA FOREX — SMC (Smart Money Concepts)
# ─────────────────────────────────────────────────────────────
def analizar_smc(df, sym):
    """
    Detecta conceptos SMC en datos diarios:
    - BOS (Break of Structure): rompe maximo/minimo previo
    - CHoCH (Change of Character): cambio de estructura
    - Order Block (OB): ultima vela bajista antes de subida fuerte / viceversa
    - FVG (Fair Value Gap): hueco de precio entre 3 velas
    - Liquidity Sweep: barre stops y revierte
    """
    if len(df) < 30:
        return None

    c = df["Close"]; h = df["High"]; l = df["Low"]; o = df["Open"]
    precio = float(c.iloc[-1])
    senales = []
    direccion = None

    # ── 1. BOS / CHoCH — Estructura de mercado ──────────────
    # Busca en las últimas 20 velas
    ventana = 20
    highs = h.iloc[-ventana:].values
    lows  = l.iloc[-ventana:].values
    closes = c.iloc[-ventana:].values

    # Máximo y mínimo de las últimas 10 velas (excluyendo la última)
    prev_high = float(h.iloc[-ventana:-1].max())
    prev_low  = float(l.iloc[-ventana:-1].min())
    prev_high_5 = float(h.iloc[-6:-1].max())
    prev_low_5  = float(l.iloc[-6:-1].min())

    # BOS alcista: precio rompe máximo previo
    if precio > prev_high_5 and float(c.iloc[-2]) <= prev_high_5:
        senales.append({
            "tipo": "🔺 BOS ALCISTA (SMC)",
            "a": "✅ SEÑAL DE COMPRA",
            "desc": f"Rompio estructura: precio supero maximo previo ${prev_high_5:,.5f}",
            "f": "FUERTE"
        })
        direccion = "COMPRA"

    # BOS bajista: precio rompe mínimo previo
    elif precio < prev_low_5 and float(c.iloc[-2]) >= prev_low_5:
        senales.append({
            "tipo": "🔻 BOS BAJISTA (SMC)",
            "a": "❌ SEÑAL DE VENTA",
            "desc": f"Rompio estructura: precio bajo minimo previo ${prev_low_5:,.5f}",
            "f": "FUERTE"
        })
        direccion = "VENTA"

    # CHoCH: en tendencia bajista, hace un BOS alcista (y viceversa)
    # Detectar tendencia previa por dirección de los últimos 10 cierres
    tend = "ALCISTA" if closes[-1] > closes[-10] else "BAJISTA"
    if tend == "BAJISTA" and precio > prev_high_5:
        senales.append({
            "tipo": "🔄 CHoCH — CAMBIO ESTRUCTURA (SMC)",
            "a": "✅ POSIBLE REVERSIÓN ALCISTA",
            "desc": f"Tendencia bajista cambia a alcista. Rompio ${prev_high_5:,.5f}",
            "f": "MUY FUERTE"
        })
        direccion = "COMPRA"
    elif tend == "ALCISTA" and precio < prev_low_5:
        senales.append({
            "tipo": "🔄 CHoCH — CAMBIO ESTRUCTURA (SMC)",
            "a": "❌ POSIBLE REVERSIÓN BAJISTA",
            "desc": f"Tendencia alcista cambia a bajista. Rompio ${prev_low_5:,.5f}",
            "f": "MUY FUERTE"
        })
        direccion = "VENTA"

    # ── 2. Order Block ───────────────────────────────────────
    # OB alcista: última vela bajista antes de un movimiento fuerte al alza
    # OB bajista: última vela alcista antes de un movimiento fuerte a la baja
    for i in range(-5, -1):
        vela_o = float(o.iloc[i]); vela_c = float(c.iloc[i])
        vela_h = float(h.iloc[i]); vela_l = float(l.iloc[i])
        sig_vela = float(c.iloc[i+1]) - float(o.iloc[i+1])

        # OB alcista: vela bajista seguida de vela alcista fuerte
        if vela_c < vela_o and sig_vela > 0:
            mov = abs(sig_vela / vela_o) * 100
            if mov > 0.3 and vela_l <= precio <= vela_h:
                senales.append({
                    "tipo": "📦 ORDER BLOCK ALCISTA (SMC)",
                    "a": "✅ ZONA DE COMPRA",
                    "desc": f"Precio en OB alcista (${vela_l:,.5f} - ${vela_h:,.5f}). Zona donde entran institucionales.",
                    "f": "FUERTE"
                })
                if not direccion: direccion = "COMPRA"
                break

        # OB bajista: vela alcista seguida de vela bajista fuerte
        if vela_c > vela_o and sig_vela < 0:
            mov = abs(sig_vela / vela_o) * 100
            if mov > 0.3 and vela_l <= precio <= vela_h:
                senales.append({
                    "tipo": "📦 ORDER BLOCK BAJISTA (SMC)",
                    "a": "❌ ZONA DE VENTA",
                    "desc": f"Precio en OB bajista (${vela_l:,.5f} - ${vela_h:,.5f}). Zona donde venden institucionales.",
                    "f": "FUERTE"
                })
                if not direccion: direccion = "VENTA"
                break

    # ── 3. FVG — Fair Value Gap ──────────────────────────────
    # FVG alcista: Low de vela 3 > High de vela 1 (hueco al alza)
    # FVG bajista: High de vela 3 < Low de vela 1 (hueco a la baja)
    for i in range(-6, -2):
        h1 = float(h.iloc[i]);   l1 = float(l.iloc[i])
        h3 = float(h.iloc[i+2]); l3 = float(l.iloc[i+2])

        # FVG alcista
        if l3 > h1:
            zona_mid = (l3 + h1) / 2
            if abs(precio - zona_mid) / zona_mid < 0.005:  # precio cerca del FVG
                senales.append({
                    "tipo": "⚡ FVG ALCISTA (SMC)",
                    "a": "✅ ZONA DE COMPRA",
                    "desc": f"Fair Value Gap alcista: hueco ${h1:,.5f} - ${l3:,.5f}. El precio viene a llenar el hueco.",
                    "f": "MEDIA"
                })
                if not direccion: direccion = "COMPRA"
                break

        # FVG bajista
        if h3 < l1:
            zona_mid = (h3 + l1) / 2
            if abs(precio - zona_mid) / zona_mid < 0.005:
                senales.append({
                    "tipo": "⚡ FVG BAJISTA (SMC)",
                    "a": "❌ ZONA DE VENTA",
                    "desc": f"Fair Value Gap bajista: hueco ${h3:,.5f} - ${l1:,.5f}. El precio viene a llenar el hueco.",
                    "f": "MEDIA"
                })
                if not direccion: direccion = "VENTA"
                break

    # ── 4. Liquidity Sweep ───────────────────────────────────
    # El precio barre el máximo/mínimo de las últimas N velas y revierte
    max_20 = float(h.iloc[-21:-1].max())
    min_20 = float(l.iloc[-21:-1].min())
    prev_close = float(c.iloc[-2])

    # Sweep alcista: precio barrió mínimos pero cerró por encima (trampa bajista)
    if float(l.iloc[-1]) < min_20 and precio > min_20:
        senales.append({
            "tipo": "🎯 LIQUIDITY SWEEP ALCISTA (SMC)",
            "a": "✅ SEÑAL DE COMPRA",
            "desc": f"Barro minimos de 20 dias (${min_20:,.5f}) y revirtio. Trampa bajista — institucionales compraron.",
            "f": "MUY FUERTE"
        })
        if not direccion: direccion = "COMPRA"

    # Sweep bajista: precio barrió máximos pero cerró por debajo (trampa alcista)
    elif float(h.iloc[-1]) > max_20 and precio < max_20:
        senales.append({
            "tipo": "🎯 LIQUIDITY SWEEP BAJISTA (SMC)",
            "a": "❌ SEÑAL DE VENTA",
            "desc": f"Barro maximos de 20 dias (${max_20:,.5f}) y revirtio. Trampa alcista — institucionales vendieron.",
            "f": "MUY FUERTE"
        })
        if not direccion: direccion = "VENTA"

    if not senales or not direccion:
        return None

    niveles = calc_niveles(precio, direccion, "FOREX")
    return {"senales": senales, "direccion": direccion, "niveles": niveles}

# ─────────────────────────────────────────────────────────────
# ESTRATEGIA ORO — MA Cross SMA50/SMA100
# ─────────────────────────────────────────────────────────────
def analizar_ma_cross(df, sym):
    """
    Estrategia MA Cross para Oro:
    SMA50 (rapida) y SMA100 (lenta)
    - SMA50 cruza ARRIBA SMA100 → COMPRA
    - SMA50 cruza ABAJO SMA100  → VENTA
    La señal ocurre exactamente en el punto del cruce (la cruz del plot)
    """
    if len(df) < 105:
        return None

    c = df["Close"]
    precio = float(c.iloc[-1])

    sma50  = c.rolling(50).mean()
    sma100 = c.rolling(100).mean()

    # Valores actuales y anteriores
    s50_hoy  = float(sma50.iloc[-1]);  s50_ayer  = float(sma50.iloc[-2])
    s100_hoy = float(sma100.iloc[-1]); s100_ayer = float(sma100.iloc[-2])

    senales  = []
    direccion = None

    # Cruce alcista: SMA50 estaba ABAJO y ahora está ARRIBA
    if s50_ayer < s100_ayer and s50_hoy > s100_hoy:
        senales.append({
            "tipo": "⭐ MA CROSS ALCISTA — SMA50 cruzo ARRIBA SMA100",
            "a": "✅ SEÑAL DE COMPRA",
            "desc": f"SMA50=${s50_hoy:,.2f} cruzo ARRIBA SMA100=${s100_hoy:,.2f}. Zona verde activada.",
            "f": "MUY FUERTE"
        })
        direccion = "COMPRA"

    # Cruce bajista: SMA50 estaba ARRIBA y ahora está ABAJO
    elif s50_ayer > s100_ayer and s50_hoy < s100_hoy:
        senales.append({
            "tipo": "💀 MA CROSS BAJISTA — SMA50 cruzo ABAJO SMA100",
            "a": "❌ SEÑAL DE VENTA",
            "desc": f"SMA50=${s50_hoy:,.2f} cruzo ABAJO SMA100=${s100_hoy:,.2f}. Zona roja activada.",
            "f": "MUY FUERTE"
        })
        direccion = "VENTA"

    # Sin cruce reciente — mostrar estado actual de las medias
    else:
        estado = "SMA50 > SMA100 (zona VERDE — tendencia alcista)" if s50_hoy > s100_hoy else "SMA50 < SMA100 (zona ROJA — tendencia bajista)"
        dist = abs(s50_hoy - s100_hoy) / s100_hoy * 100
        if dist < 0.5:  # Las medias están muy cerca — cruce inminente
            senales.append({
                "tipo": "⚠️ CRUCE INMINENTE SMA50/SMA100",
                "a": "👀 PREPARAR ENTRADA",
                "desc": f"SMA50 y SMA100 separadas solo {dist:.2f}%. {estado}. Cruce puede ocurrir pronto.",
                "f": "MEDIA"
            })
            direccion = "COMPRA" if s50_hoy > s100_hoy else "VENTA"

    if not senales or not direccion:
        return None

    # RSI como filtro adicional
    rsi_val = float(calc_rsi(c).iloc[-1])
    if direccion == "COMPRA" and rsi_val > 70:
        senales.append({
            "tipo": "⚠️ FILTRO RSI",
            "a": "👀 PRECAUCION",
            "desc": f"MA Cross dice COMPRA pero RSI={rsi_val:.0f} sobrecomprado. Espera retroceso.",
            "f": "MEDIA"
        })
    elif direccion == "VENTA" and rsi_val < 30:
        senales.append({
            "tipo": "⚠️ FILTRO RSI",
            "a": "👀 PRECAUCION",
            "desc": f"MA Cross dice VENTA pero RSI={rsi_val:.0f} sobrevendido. Espera rebote.",
            "f": "MEDIA"
        })

    niveles = calc_niveles(precio, direccion, "ORO")
    return {"senales": senales, "direccion": direccion, "niveles": niveles,
            "sma50": s50_hoy, "sma100": s100_hoy, "rsi": rsi_val}

# ─────────────────────────────────────────────────────────────
# ESTRATEGIA GENERAL — Acciones, ETF, BTC
# ─────────────────────────────────────────────────────────────
def analizar_general(df, sym, cat):
    PARAMS = {
        "ACCION":{"rsi_b":30,"rsi_a":70,"cambio":2.0,"ema_c":20,"ema_l":50},
        "ETF":   {"rsi_b":35,"rsi_a":65,"cambio":1.5,"ema_c":20,"ema_l":50},
        "CRYPTO":{"rsi_b":30,"rsi_a":70,"cambio":4.0,"ema_c":20,"ema_l":50},
    }
    p = PARAMS.get(cat, PARAMS["ACCION"])
    c = df["Close"]
    precio = float(c.iloc[-1])
    precio_ayer = float(c.iloc[-2])
    precio_7d   = float(c.iloc[-6]) if len(c)>=6 else precio_ayer
    cd = ((precio - precio_ayer) / precio_ayer) * 100
    c7 = ((precio - precio_7d)   / precio_7d)   * 100

    ema_c = c.ewm(span=p["ema_c"],adjust=False).mean()
    ema_l = c.ewm(span=p["ema_l"],adjust=False).mean()
    rsi_v = float(calc_rsi(c).iloc[-1])

    senales = []; direccion = None

    if cd >= p["cambio"]:
        senales.append({"tipo":"📈 SUBIDA FUERTE","a":"⚡ POSIBLE COMPRA",
                         "desc":f"Subio {cd:.2f}% hoy (umbral {p['cambio']}%)","f":"MEDIA"})
        direccion = "COMPRA"
    elif cd <= -p["cambio"]:
        senales.append({"tipo":"📉 CAIDA FUERTE","a":"⚠️ POSIBLE VENTA",
                         "desc":f"Cayo {abs(cd):.2f}% hoy (umbral {p['cambio']}%)","f":"MEDIA"})
        direccion = "VENTA"

    if rsi_v <= p["rsi_b"]:
        senales.append({"tipo":"🔵 RSI SOBREVENDIDO","a":"✅ SEÑAL DE COMPRA",
                         "desc":f"RSI={rsi_v:.1f} bajo {p['rsi_b']} — muy barato tecnicamente","f":"FUERTE"})
        direccion = "COMPRA"
    elif rsi_v >= p["rsi_a"]:
        senales.append({"tipo":"🔴 RSI SOBRECOMPRADO","a":"❌ SEÑAL DE VENTA",
                         "desc":f"RSI={rsi_v:.1f} sobre {p['rsi_a']} — muy caro tecnicamente","f":"FUERTE"})
        direccion = "VENTA"

    ec_h=float(ema_c.iloc[-1]); ec_a=float(ema_c.iloc[-2])
    el_h=float(ema_l.iloc[-1]); el_a=float(ema_l.iloc[-2])
    if ec_a < el_a and ec_h > el_h:
        senales.append({"tipo":"⭐ GOLDEN CROSS","a":"✅ COMPRA FUERTE",
                         "desc":f"EMA{p['ema_c']} cruzo ARRIBA a EMA{p['ema_l']}","f":"MUY FUERTE"})
        direccion = "COMPRA"
    elif ec_a > el_a and ec_h < el_h:
        senales.append({"tipo":"💀 DEATH CROSS","a":"❌ VENTA FUERTE",
                         "desc":f"EMA{p['ema_c']} cruzo ABAJO a EMA{p['ema_l']}","f":"MUY FUERTE"})
        direccion = "VENTA"

    if cat != "FOREX" and "Volume" in df.columns:
        v = df["Volume"].dropna()
        if len(v) > 20:
            va=float(v.iloc[-1]); vp=float(v.rolling(20).mean().iloc[-1])
            if vp > 0 and va/vp >= 2.0:
                senales.append({"tipo":"🔊 VOLUMEN INUSUAL","a":"👀 PRESTAR ATENCION",
                                  "desc":f"Volumen {va/vp:.1f}x el promedio","f":"MEDIA"})

    if cat == "CRYPTO":
        mn=float(c.rolling(30).min().iloc[-1]); mx=float(c.rolling(30).max().iloc[-1])
        dm=((precio-mn)/mn)*100; dM=((mx-precio)/mx)*100
        if dm<=5:
            senales.append({"tipo":"🛡️ CERCA SOPORTE","a":"✅ ZONA DE COMPRA",
                              "desc":f"BTC a {dm:.1f}% del minimo 30d (${mn:,.0f})","f":"FUERTE"})
            direccion = "COMPRA"
        elif dM<=5:
            senales.append({"tipo":"🚧 CERCA RESISTENCIA","a":"❌ ZONA DE VENTA",
                              "desc":f"BTC a {dM:.1f}% del maximo 30d (${mx:,.0f})","f":"FUERTE"})
            direccion = "VENTA"

    if not senales or not direccion:
        return None

    niveles = calc_niveles(precio, direccion, cat)
    return {"senales": senales, "direccion": direccion, "niveles": niveles,
            "rsi": rsi_v, "cd": cd, "c7": c7,
            "ec": ec_h, "el": el_h,
            "ec_n": p["ema_c"], "el_n": p["ema_l"]}

# ─────────────────────────────────────────────────────────────
# DISPATCHER PRINCIPAL
# ─────────────────────────────────────────────────────────────
def analizar(sym, cr=None):
    try:
        cat = categoria(sym)
        if cat == "ORO":
            _, oro_tf, _, oro_sesion = en_sesion_oro()
            oro_dias = 30 if oro_tf == "30m" else 120
            df = get_datos(sym, cr, tf=oro_tf, dias=oro_dias)
            print(f"    [{oro_sesion}|{oro_tf}]", end=" ")
        else:
            cfg = INTERVALOS_MERCADO.get(cat, {"tf":"1d","dias":90})
            df  = get_datos(sym, cr, tf=cfg["tf"], dias=cfg["dias"])
        if df is None or len(df) < 30:
            return None

        precio = float(df["Close"].iloc[-1])
        precio_ayer = float(df["Close"].iloc[-2])
        cd = ((precio - precio_ayer) / precio_ayer) * 100
        c7 = ((precio - float(df["Close"].iloc[-6])) / float(df["Close"].iloc[-6])) * 100 if len(df)>=6 else 0

        # Usar estrategia específica por mercado
        if cat == "FOREX":
            res = analizar_smc(df, sym)
        elif cat == "ORO":
            res = analizar_ma_cross(df, sym)
        else:
            res = analizar_general(df, sym, cat)

        if not res:
            return None

        return {
            "sym": sym,
            "nombre": NOMBRES.get(sym, sym),
            "cat": cat,
            "icono": ICONOS[cat],
            "precio": precio,
            "cd": cd, "c7": c7,
            "senales": res["senales"],
            "direccion": res["direccion"],
            "niveles": res["niveles"],
            "extra": res,
            "hora": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    except Exception as e:
        print(f"  ⚠️ {sym}: {e}")
        return None

# ─────────────────────────────────────────────────────────────
# HTML DEL CORREO
# ─────────────────────────────────────────────────────────────
CAT_LABEL = {"ACCION":"ACCIONES","ETF":"ETFs","ORO":"ORO — Estrategia MA Cross SMA50/SMA100",
             "CRYPTO":"CRYPTO — BTC/USD","FOREX":"FOREX — Estrategia SMC (Smart Money Concepts)"}
CAT_DESC  = {
    "ACCION":"RSI + EMA + Volumen. Opera solo 9:30-16:00 ET.",
    "ETF":   "RSI + EMA. Menos volatiles, ideales largo plazo.",
    "ORO":   "SMA50 cruza SMA100. Zona verde=alcista, zona roja=bajista. SL 1% / TP 2%.",
    "CRYPTO":"RSI + Soporte/Resistencia 30d. SL 5% / TP 10%. Alta volatilidad.",
    "FOREX": "BOS, CHoCH, Order Block, FVG, Liquidity Sweep. SL 0.5% / TP 1%. Opera 24h.",
}

def construir_html(resultados):
    grupos = {}
    for r in resultados: grupos.setdefault(r["cat"],[]).append(r)
    body = ""

    for cat in ["ACCION","ETF","ORO","CRYPTO","FOREX"]:
        if cat not in grupos: continue
        body += (f'<div style="background:#0F3460;border-radius:10px;padding:10px 16px;margin:20px 0 8px;">'
                 f'<span style="font-size:16px;">{ICONOS[cat]}</span> '
                 f'<b style="color:#F5A623;font-size:14px;">{CAT_LABEL[cat]}</b>'
                 f'<div style="color:#aaa;font-size:11px;margin-top:2px;">{CAT_DESC[cat]}</div></div>')

        for r in grupos[cat]:
            col = "#00B074" if r["cd"]>=0 else "#E94560"
            sig = "▲" if r["cd"]>=0 else "▼"
            dir_col = "#00B074" if r["direccion"]=="COMPRA" else "#E94560"
            dir_emoji = "✅" if r["direccion"]=="COMPRA" else "❌"
            niv = r["niveles"]

            # Señales detalladas
            bq = ""
            for s in r["senales"]:
                bg="#1a3a2a" if "COMPRA" in s["a"] else "#3a1a1a" if "VENTA" in s["a"] else "#1a2a3a"
                bq += (f'<div style="background:{bg};border-radius:8px;padding:7px 10px;margin:4px 0;">'
                       f'<b style="font-size:12px;">{s["tipo"]}</b> — '
                       f'<span style="color:#F5A623;font-size:12px;">{s["a"]}</span><br>'
                       f'<span style="color:#ccc;font-size:11px;">{s["desc"]}</span> '
                       f'<span style="background:#0F3460;padding:1px 5px;border-radius:6px;font-size:10px;">{s["f"]}</span>'
                       f'</div>')

            # ── BLOQUE PRINCIPAL DE OPERACIÓN ──
            op_html = (
                f'<div style="background:#0a1a0a;border:2px solid {dir_col};border-radius:12px;padding:16px;margin:10px 0;">'
                f'<div style="text-align:center;margin-bottom:12px;">'
                f'<span style="font-size:22px;">{dir_emoji}</span> '
                f'<b style="font-size:18px;color:{dir_col};">{r["direccion"]}</b>'
                f'</div>'
                f'<table width="100%" style="border-collapse:collapse;">'
                # Entrada
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">🎯 PRECIO DE ENTRADA</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#fff;">${niv["entrada"]:,.5f}</td>'
                f'</tr>'
                # Stop Loss
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">🛑 STOP LOSS ({niv["sl_pct"]}%)</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#E94560;">${niv["sl"]:,.5f}</td>'
                f'</tr>'
                # Take Profit
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">✅ TAKE PROFIT ({niv["tp_pct"]}%)</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#00B074;">${niv["tp"]:,.5f}</td>'
                f'</tr>'
                # R:R
                f'<tr>'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">⚖️ RELACION RIESGO:GANANCIA</td>'
                f'<td style="padding:8px;text-align:right;font-size:13px;font-weight:bold;color:#F5A623;">1 : {niv["rr"]:.1f}</td>'
                f'</tr>'
                f'</table>'
                f'<div style="background:#0F3460;border-radius:8px;padding:8px;margin-top:10px;text-align:center;">'
                f'<span style="color:#aaa;font-size:11px;">⚠️ Precio referencial al momento de la señal. '
                f'Verifica en tu plataforma antes de operar.</span>'
                f'</div>'
                f'</div>'
            )

            # Info adicional según mercado
            info_extra = ""
            ex = r.get("extra", {})
            if cat == "ORO" and "sma50" in ex:
                info_extra = (f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                              f'SMA50: <b style="color:#4488ff;">${ex["sma50"]:,.2f}</b> | '
                              f'SMA100: <b style="color:#44bb44;">${ex["sma100"]:,.2f}</b> | '
                              f'RSI: <b style="color:#F5A623;">{ex["rsi"]:.1f}</b>'
                              f'</div>')
            elif cat == "FOREX":
                info_extra = (f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                              f'Estrategia: <b style="color:#F5A623;">Smart Money Concepts (SMC)</b> | '
                              f'Señales detectadas: <b>{len(r["senales"])}</b>'
                              f'</div>')

            body += (
                f'<div style="background:#16213E;border-radius:12px;padding:16px;margin:8px 0;border-left:4px solid {dir_col};">'
                f'<table width="100%"><tr>'
                f'<td><b style="font-size:16px;color:#fff;">{r["nombre"]}</b><br>'
                f'<span style="color:#aaa;font-size:10px;">{r["hora"]}</span></td>'
                f'<td align="right"><b style="font-size:16px;color:#fff;">${r["precio"]:,.5f}</b><br>'
                f'<span style="color:{col};font-size:12px;">{sig} {abs(r["cd"]):.2f}% hoy</span></td>'
                f'</tr></table>'
                f'{info_extra}'
                f'{op_html}'
                f'<details style="margin-top:8px;">'
                f'<summary style="color:#F5A623;font-size:11px;cursor:pointer;">📊 Ver señales detalladas ({len(r["senales"])})</summary>'
                f'{bq}</details>'
                f'</div>'
            )

    return (
        f'<html><body style="margin:0;padding:0;background:#0D0D1A;font-family:Arial,sans-serif;color:#fff;">'
        f'<div style="max-width:640px;margin:0 auto;padding:20px;">'
        f'<div style="background:#1A1A2E;border-radius:16px;padding:20px;text-align:center;margin-bottom:8px;">'
        f'<div style="font-size:28px;">📊</div>'
        f'<h1 style="margin:6px 0;font-size:20px;color:#F5A623;">ALERTA DE TRADING</h1>'
        f'<p style="color:#aaa;font-size:12px;">{len(resultados)} activo(s) con señales — {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>'
        f'<p style="color:#555;font-size:10px;">⚠️ No es asesoria financiera. Analiza y confirma antes de operar.</p>'
        f'</div>'
        f'{body}'
        f'<div style="text-align:center;color:#555;font-size:10px;padding:10px;border-top:1px solid #222;margin-top:12px;">'
        f'Bot Railway 24/7 | Forex SMC: cada 60min en {FOREX_INTERVALO} | Oro/BTC/Acciones: cada 30min diario</div>'
        f'</div></body></html>'
    )

# ─────────────────────────────────────────────────────────────
# ENVÍO Y LOOP
# ─────────────────────────────────────────────────────────────
def enviar_cierre_trades(eventos):
    """Envía correo cuando un trade cierra por SL o TP."""
    try:
        msg = MIMEMultipart("alternative")
        cierres_tp = [e for e in eventos if e["tipo"] == "TP_TOCADO"]
        cierres_sl = [e for e in eventos if e["tipo"] == "SL_TOCADO"]
        asunto_icon = "✅" if cierres_tp and not cierres_sl else "🛑" if cierres_sl and not cierres_tp else "📊"
        msg["Subject"] = f"{asunto_icon} {len(eventos)} Trade(s) Cerrado(s) — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        msg["From"] = EMAIL_REMITENTE
        msg["To"]   = EMAIL_DESTINO

        filas = ""
        for ev in eventos:
            t = ev["trade"]
            es_tp = ev["tipo"] == "TP_TOCADO"
            color_res = "#00B074" if es_tp else "#E94560"
            icono_res = "✅ GANANCIA" if es_tp else "🛑 PÉRDIDA"
            signo     = "+" if es_tp else "-"
            diff      = abs(ev["precio_cierre"] - t["entrada"])

            filas += (
                f'<div style="background:#16213E;border-radius:12px;padding:18px;margin:12px 0;'
                f'border-left:6px solid {color_res};">'
                f'<table width="100%"><tr>'
                f'<td><b style="font-size:18px;color:#fff;">{t["nombre"]}</b><br>'
                f'<span style="color:#aaa;font-size:11px;">Cerrado: {datetime.now().strftime("%d/%m/%Y %H:%M")}</span></td>'
                f'<td align="right">'
                f'<span style="font-size:22px;font-weight:bold;color:{color_res};">{icono_res}</span><br>'
                f'<span style="font-size:16px;color:{color_res};font-weight:bold;">{signo}{abs(ev["resultado_pct"]):.1f}%</span>'
                f'</td></tr></table>'
                f'<table width="100%" style="margin:12px 0;background:#0F3460;border-radius:8px;">'
                f'<tr>'
                f'<td style="color:#aaa;font-size:11px;padding:6px 10px;">Dirección</td>'
                f'<td style="color:#aaa;font-size:11px;padding:6px 10px;">Entrada</td>'
                f'<td style="color:#aaa;font-size:11px;padding:6px 10px;">Cierre</td>'
                f'<td style="color:#aaa;font-size:11px;padding:6px 10px;">Diferencia</td>'
                f'</tr><tr>'
                f'<td style="color:#F5A623;font-weight:bold;padding:6px 10px;">{t["direccion"]}</td>'
                f'<td style="color:#fff;padding:6px 10px;">${t["entrada"]:,.5f}</td>'
                f'<td style="color:{color_res};font-weight:bold;padding:6px 10px;">${ev["precio_cierre"]:,.5f}</td>'
                f'<td style="color:{color_res};font-weight:bold;padding:6px 10px;">{signo}${diff:,.5f}</td>'
                f'</tr></table>'
                f'<div style="background:{color_res}22;border:1px solid {color_res};border-radius:8px;'
                f'padding:10px;text-align:center;margin-top:8px;">'
                f'<b style="color:{color_res};font-size:14px;">{ev["mensaje"]}</b>'
                f'</div>'
                f'<div style="color:#555;font-size:10px;margin-top:8px;text-align:center;">'
                f'Abierto: {t["hora"]} | SL original: ${t["sl"]:,.5f} | TP original: ${t["tp"]:,.5f}'
                f'</div>'
                f'</div>'
            )

        resumen_color = "#00B074" if cierres_tp and not cierres_sl else "#E94560" if cierres_sl and not cierres_tp else "#F5A623"
        html_body = (
            f'<html><body style="margin:0;padding:0;background:#0D0D1A;font-family:Arial,sans-serif;color:#fff;">'
            f'<div style="max-width:640px;margin:0 auto;padding:20px;">'
            f'<div style="background:#1A1A2E;border-radius:16px;padding:20px;text-align:center;margin-bottom:12px;">'
            f'<div style="font-size:32px;">{"✅" if cierres_tp and not cierres_sl else "🛑" if cierres_sl and not cierres_tp else "📊"}</div>'
            f'<h1 style="margin:6px 0;font-size:20px;color:{resumen_color};">TRADE(S) CERRADO(S)</h1>'
            f'<p style="color:#aaa;font-size:12px;">'
            f'{"✅ " + str(len(cierres_tp)) + " Take Profit" if cierres_tp else ""} '
            f'{"🛑 " + str(len(cierres_sl)) + " Stop Loss" if cierres_sl else ""}'
            f'</p></div>'
            f'{filas}'
            f'<div style="text-align:center;color:#555;font-size:10px;padding:10px;border-top:1px solid #222;margin-top:12px;">'
            f'Bot Railway 24/7 — Registro automatico de trades</div>'
            f'</div></body></html>'
        )

        txt = "\n".join([f"{ev['trade']['nombre']}: {ev['mensaje']} | Cierre: ${ev['precio_cierre']:,.5f}" for ev in eventos])
        msg.attach(MIMEText(txt, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.sendmail(EMAIL_REMITENTE, EMAIL_DESTINO, msg.as_string())
        print(f"  ✅ Correo de cierre enviado — {len(eventos)} trade(s)")
    except Exception as e:
        print(f"  ❌ Error correo cierre: {e}")

def enviar(res):
    try:
        msg = MIMEMultipart("alternative")
        cats = set(r["cat"] for r in res)
        msg["Subject"] = f"🚨 {len(res)} Alerta(s) [{', '.join(cats)}] — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        msg["From"] = EMAIL_REMITENTE; msg["To"] = EMAIL_DESTINO
        txt = "\n".join([
            f"{r['nombre']}: ${r['precio']:,.5f} ({r['cd']:+.2f}%) → {r['direccion']} | SL:${r['niveles']['sl']:,.5f} TP:${r['niveles']['tp']:,.5f}"
            for r in res
        ])
        msg.attach(MIMEText(txt,"plain"))
        msg.attach(MIMEText(construir_html(res),"html"))
        with smtplib.SMTP("smtp.gmail.com",587,timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_REMITENTE,EMAIL_CONTRASENA)
            s.sendmail(EMAIL_REMITENTE,EMAIL_DESTINO,msg.as_string())
        print(f"  ✅ Correo enviado — {len(res)} alerta(s)")
    except Exception as e:
        print(f"  ❌ Error correo: {e}")

def debe_revisar(cat):
    """
    Devuelve True si:
    1. La sesion del mercado esta activa
    2. Ya paso el intervalo correspondiente (dinamico para el oro)
    """
    ahora = time.time()
    if cat == "ORO":
        activo, tf, cada, nombre = en_sesion_oro()
        if not activo:
            return False
        ult = _ultimo_chequeo.get(cat, 0)
        return (ahora - ult) >= (cada * 60)
    if not sesion_activa(cat):
        return False
    cada = INTERVALOS_MERCADO.get(cat, {"cada_min": 30})["cada_min"] * 60
    ult  = _ultimo_chequeo.get(cat, 0)
    return (ahora - ult) >= cada

def marcar_revisado(cat):
    _ultimo_chequeo[cat] = time.time()

def revisar():
    print(f"\n{'='*55}")
    print(f"  🔍 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*55}")

    # Determinar qué mercados toca revisar ahora
    cats_a_revisar = [cat for cat in ["ACCION","ETF","ORO","CRYPTO","FOREX"] if debe_revisar(cat)]
    activos_a_revisar = [s for s in TODOS if categoria(s) in cats_a_revisar]

    if not activos_a_revisar:
        print("  ⏳ Ningún mercado activo ahora")
        for cat in ["ACCION","ETF","ORO","CRYPTO","FOREX"]:
            cada   = INTERVALOS_MERCADO.get(cat, {"cada_min":30})["cada_min"]
            ult    = _ultimo_chequeo.get(cat, 0)
            faltan = max(0, int((ult + cada*60 - time.time()) / 60))
            if cat == "ORO":
                activo, tf, cada_oro, nombre_ses = en_sesion_oro()
                estado = f"🟢 {nombre_ses}" if activo else "🔴 Fuera de sesión"
            else:
                tf     = INTERVALOS_MERCADO.get(cat, {}).get("tf","1d")
                activo = sesion_activa(cat)
                estado = "🟢 Sesión activa" if activo else "🔴 Fuera de sesión"
            print(f"    {ICONOS.get(cat,'📊')} {cat:6} [{tf}] {estado} — próxima revisión en {faltan} min")
        return

    print(f"  📋 Revisando: {', '.join(cats_a_revisar)}")
    for cat in cats_a_revisar:
        tf = INTERVALOS_MERCADO.get(cat, {}).get("tf","1d")
        print(f"    {ICONOS.get(cat,'📊')} {cat} — timeframe {tf}")

    cr = get_crumb()
    print(f"  {'✅ Conectado a Yahoo' if cr else '⚠️ Sin crumb...'}")

    # ── Analizar todos los activos del turno ──
    todos_resultados = []
    for sym in activos_a_revisar:
        cat = categoria(sym)
        tf  = INTERVALOS_MERCADO.get(cat, {}).get("tf","1d")
        print(f"  [{cat:6}|{tf}] {sym}...", end=" ", flush=True)
        r = analizar(sym, cr)
        if r:
            print(f"⚠️  señal → {r['direccion']} | Entrada:${r['niveles']['entrada']:,.4f} SL:${r['niveles']['sl']:,.4f} TP:${r['niveles']['tp']:,.4f}")
            todos_resultados.append(r)
        else:
            print("✅ Sin señal")
        time.sleep(2)

    # Marcar como revisados
    for cat in cats_a_revisar:
        marcar_revisado(cat)

    # ── Verificar trades activos (SL/TP tocado) ──
    eventos_cierre = verificar_trades_activos(todos_resultados)
    if eventos_cierre:
        print(f"\n  🔔 {len(eventos_cierre)} trade(s) cerrado(s):")
        for ev in eventos_cierre:
            print(f"    {ev['sym']}: {ev['mensaje']}")
        enviar_cierre_trades(eventos_cierre)

    # ── Filtrar solo señales NUEVAS (sin trade activo o cambio de direccion) ──
    senales_nuevas = filtrar_señales_nuevas(todos_resultados)

    # ── Estado de trades activos ──
    if _trades_activos:
        print(f"\n  📂 Trades activos: {len(_trades_activos)}")
        for sym, t in _trades_activos.items():
            print(f"    {sym}: {t['direccion']} desde ${t['entrada']:,.4f} | SL:${t['sl']:,.4f} | TP:${t['tp']:,.4f}")

    print(f"\n  📊 {len(senales_nuevas)} señal(es) NUEVA(s) / {len(todos_resultados)} detectadas")
    if senales_nuevas:
        enviar(senales_nuevas)
    elif not eventos_cierre:
        print("  😴 Sin señales nuevas")

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════╗
║   🤖 BOT DE TRADING v7 — CON ENTRADA SL Y TP        ║
╠══════════════════════════════════════════════════════╣
║  Forex  ({len(FOREX)}):  SMC — BOS/CHoCH/OB/FVG/Sweep       ║
║  Oro    ({len(ORO)}):  MA Cross SMA50/SMA100             ║
║  BTC    ({len(CRYPTO)}):  RSI + Soporte/Resistencia         ║
║  Acc    ({len(ACCIONES)}):  RSI + EMA + Volumen               ║
║  ETF    ({len(ETFS)}):  RSI + EMA                         ║
╠══════════════════════════════════════════════════════╣
║  SL/TP: Forex 0.5/1% | Oro 1/2% | BTC 5/10%        ║
║         Acciones 2/4% | ETF 1.5/3%                  ║
╚══════════════════════════════════════════════════════╝""")
    revisar()
    while True:
        time.sleep(5 * 60)  # Chequea cada 5 min si hay mercado que revisar
        revisar()
