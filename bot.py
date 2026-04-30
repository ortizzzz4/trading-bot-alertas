"""
Bot de Alertas de Trading v10.0
─────────────────────────────────────────────────────────────
ESTRATEGIAS:
  • ORO   (GC=F):    MA Cross SMA50/SMA100 — TF 30min — revisa c/3min en sesión
  • FOREX (4 pares): MA Cross SMA50/SMA100 — TF 30min — revisa c/3min (lun-vie)
  • BTC:             RSI Divergencia + EMA Cross (9/21) — TF 30min — c/15min
  • NAS100/SP500:    EMA Cross (9/21) + RSI + S/R — TF 15min — c/15min
  • Acciones/ETF:    RSI + EMA + Volumen — TF 1d

REGLAS:
  • ORO y FOREX: señal SOLO si SMA50 cruza SMA100 (ventana 3 velas)
                 Sin fallbacks. Sin señales débiles. Sin "cruce inminente".
  • BTC: señal si EMA9 cruza EMA21 Y RSI confirma (no sobreextendido)
  • Cada correo incluye gráfico con indicadores
  • Gestor de trades activos: evita duplicar alertas, detecta SL/TP

HORARIOS:
  • ORO:   Sesiones CST — 06:00-10:00 (NY mañana) | 18:00-20:30 (Asia)
  • FOREX: Lunes-Viernes, 24h
  • BTC/Índices: 24h / solo días hábiles
"""

import requests, pandas as pd, numpy as np
import smtplib, time, os, random, io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime, timezone, timedelta

# ── Configuración ────────────────────────────────────────────
EMAIL_REMITENTE  = os.environ.get("EMAIL_REMITENTE",  "tu_correo@gmail.com")
EMAIL_CONTRASENA = os.environ.get("EMAIL_CONTRASENA", "xxxx xxxx xxxx xxxx")
EMAIL_DESTINO    = os.environ.get("EMAIL_DESTINO",    "tu_correo@gmail.com")

# ── Timeframes ───────────────────────────────────────────────
INTERVALOS_MERCADO = {
    "FOREX":  {"tf": "30m", "dias": 35,  "cada_min": 3},
    "ORO":    {"tf": "30m", "dias": 35,  "cada_min": 3},
    "CRYPTO": {"tf": "30m", "dias": 10,  "cada_min": 15},
    "INDICE": {"tf": "15m", "dias": 7,   "cada_min": 15},
    "ACCION": {"tf": "1d",  "dias": 90,  "cada_min": 1440},
    "ETF":    {"tf": "1d",  "dias": 90,  "cada_min": 1440},
}

_ultimo_chequeo = {}
_trades_activos = {}
_df_cache       = {}

# ── SL/TP por categoría ──────────────────────────────────────
SL_TP = {
    "ACCION": {"sl": 2.0,  "tp": 4.0},
    "ETF":    {"sl": 1.5,  "tp": 3.0},
    "FOREX":  {"sl": 0.5,  "tp": 1.0},
    "ORO":    {"sl": 1.0,  "tp": 2.0},
    "CRYPTO": {"sl": 3.0,  "tp": 6.0},
    "INDICE": {"sl": 1.0,  "tp": 2.0},
}

# ── Activos ──────────────────────────────────────────────────
ACCIONES = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "GOOGL", "META"]
ETFS     = ["VTI", "VOO", "BND", "SDY"]
FOREX    = ["EURUSD=X", "USDJPY=X", "USDCAD=X", "AUDUSD=X"]
ORO      = ["GC=F"]
CRYPTO   = ["BTC-USD"]
INDICES  = ["NQ=F", "ES=F"]
TODOS    = ACCIONES + ETFS + FOREX + ORO + CRYPTO + INDICES

NOMBRES = {
    "GC=F":     "XAUUSD — Oro",
    "BTC-USD":  "BTC/USD — Bitcoin",
    "NQ=F":     "NAS100 — Nasdaq 100 Futures",
    "ES=F":     "SP500 — S&P 500 Futures",
    "EURUSD=X": "EUR/USD", "USDJPY=X": "USD/JPY",
    "USDCAD=X": "USD/CAD", "AUDUSD=X": "AUD/USD",
    "VTI": "VTI — ETF Mercado Total", "VOO": "VOO — ETF S&P500",
    "BND": "BND — ETF Bonos",         "SDY": "SDY — ETF Dividendos",
}

def categoria(s):
    if s in FOREX:   return "FOREX"
    if s in ORO:     return "ORO"
    if s in CRYPTO:  return "CRYPTO"
    if s in INDICES: return "INDICE"
    if s in ETFS:    return "ETF"
    return "ACCION"

ICONOS = {"ACCION": "📈", "ETF": "🗂️", "FOREX": "💱",
          "ORO": "🥇", "CRYPTO": "₿", "INDICE": "📊"}

# ─────────────────────────────────────────────────────────────
# PALETA DE COLORES
# ─────────────────────────────────────────────────────────────
CS = {
    "bg":         "#0D0D1A",
    "panel_bg":   "#16213E",
    "grid":       "#1e2a44",
    "text":       "#CCCCCC",
    "title":      "#F5A623",
    "bull":       "#00B074",
    "bear":       "#E94560",
    "sma50":      "#4488FF",
    "sma100":     "#44BB44",
    "ema_fast":   "#4488FF",
    "ema_slow":   "#FF8844",
    "rsi_line":   "#9B59B6",
    "rsi_ob":     "#E94560",
    "rsi_os":     "#00B074",
    "vol_up":     "#00B07466",
    "vol_dn":     "#E9456066",
    "sl_line":    "#E94560",
    "tp_line":    "#00B074",
    "entry_line": "#F5A623",
}

# ─────────────────────────────────────────────────────────────
# HELPERS GRÁFICO
# ─────────────────────────────────────────────────────────────
def _setup_ax(ax, title=""):
    ax.set_facecolor(CS["panel_bg"])
    ax.tick_params(colors=CS["text"], labelsize=8)
    ax.yaxis.label.set_color(CS["text"])
    ax.xaxis.label.set_color(CS["text"])
    for spine in ax.spines.values():
        spine.set_edgecolor(CS["grid"])
    ax.grid(True, color=CS["grid"], linewidth=0.5, alpha=0.7)
    if title:
        ax.set_title(title, color=CS["text"], fontsize=9, pad=4)

def _dibujar_velas(ax, df, n=80):
    sub = df.tail(n).copy().reset_index()
    xs  = range(len(sub))
    for i, row in sub.iterrows():
        o, c, h, l = row["Open"], row["Close"], row["High"], row["Low"]
        color = CS["bull"] if c >= o else CS["bear"]
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        body_h = abs(c - o) if abs(c - o) > 0 else (h - l) * 0.01
        rect = plt.Rectangle((i - 0.35, min(o, c)), 0.7, body_h,
                              color=color, zorder=3)
        ax.add_patch(rect)
    return sub, xs

def _lineas_sl_tp(ax, xs, niveles, y_min, y_max):
    e, sl, tp = niveles["entrada"], niveles["sl"], niveles["tp"]
    for val, col, lbl, ls in [
        (e,  CS["entry_line"], f'Entrada ${e:,.5g}', "--"),
        (sl, CS["sl_line"],    f'SL ${sl:,.5g}',     ":"),
        (tp, CS["tp_line"],    f'TP ${tp:,.5g}',     ":"),
    ]:
        if y_min <= val <= y_max * 1.1:
            ax.axhline(val, color=col, linewidth=1.2, linestyle=ls, alpha=0.9, zorder=5)
            ax.text(len(xs) - 0.5, val, f" {lbl}", color=col,
                    fontsize=7, va="center", ha="left", zorder=6,
                    bbox=dict(fc=CS["bg"], ec="none", pad=1))

def _tag_hora(ax):
    ax.text(0.99, 0.99, datetime.now().strftime("%d/%m/%Y %H:%M"),
            transform=ax.transAxes, color=CS["text"],
            fontsize=7, ha="right", va="top", alpha=0.6)

def _etiquetar_eje_x(ax, sub):
    n = len(sub)
    ticks = list(range(0, n, max(1, n // 8)))
    labels = []
    for i in ticks:
        try:
            d = sub["Date"].iloc[i] if "Date" in sub.columns else sub.index[i]
            labels.append(pd.Timestamp(d).strftime("%d/%m %H:%M"))
        except:
            labels.append("")
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=30, fontsize=7, color=CS["text"])

def _fig_a_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=CS["bg"])
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def _calc_rsi_serie(s, n=14):
    d = s.diff()
    g = d.where(d > 0, 0.0)
    p = -d.where(d < 0, 0.0)
    return 100 - (100 / (1 + (g.rolling(n).mean() / p.rolling(n).mean())))

# ─────────────────────────────────────────────────────────────
# GRÁFICO MA CROSS (ORO y FOREX)
# ─────────────────────────────────────────────────────────────
def generar_grafico_ma_cross(df, resultado):
    try:
        nombre  = resultado["nombre"]
        niv     = resultado["niveles"]
        dir_    = resultado["direccion"]
        ex      = resultado.get("extra", {})
        n_velas = 120

        fig = plt.figure(figsize=(10, 6.5), facecolor=CS["bg"])
        gs  = GridSpec(2, 1, figure=fig,
                       height_ratios=[3, 1],
                       hspace=0.08, top=0.92, bottom=0.06,
                       left=0.07, right=0.90)
        ax_p = fig.add_subplot(gs[0])
        ax_r = fig.add_subplot(gs[1], sharex=ax_p)

        _setup_ax(ax_p)
        sub, xs = _dibujar_velas(ax_p, df, n_velas)

        c_full   = df["Close"]
        sma50_f  = c_full.rolling(50).mean()
        sma100_f = c_full.rolling(100).mean()
        sma50_s  = sma50_f.iloc[-n_velas:].values
        sma100_s = sma100_f.iloc[-n_velas:].values

        ax_p.plot(xs, sma50_s,  color=CS["sma50"],  linewidth=1.5, label="SMA50",  zorder=4)
        ax_p.plot(xs, sma100_s, color=CS["sma100"], linewidth=1.5, label="SMA100", zorder=4)

        # Marcar cruce con X
        cruce_x = ex.get("cruce_vela_idx")
        cruce_y = ex.get("cruce_precio")
        if cruce_x is not None and cruce_y is not None:
            ax_p.plot(cruce_x, cruce_y, 'x',
                      color='white', markersize=12, markeredgewidth=3,
                      zorder=8, label="Cruce")

        valid = ~(np.isnan(sma50_s) | np.isnan(sma100_s))
        xi = [i for i in xs if valid[i]]
        ax_p.fill_between(xi, sma50_s[valid], sma100_s[valid],
                          where=(sma50_s[valid] >= sma100_s[valid]),
                          color=CS["bull"], alpha=0.10)
        ax_p.fill_between(xi, sma50_s[valid], sma100_s[valid],
                          where=(sma50_s[valid] < sma100_s[valid]),
                          color=CS["bear"], alpha=0.10)

        y_min = float(sub["Low"].min()) * 0.995
        y_max = float(sub["High"].max()) * 1.005
        ax_p.set_ylim(y_min, y_max)
        _lineas_sl_tp(ax_p, xs, niv, y_min, y_max)
        ax_p.legend(loc="upper left", fontsize=7,
                    facecolor=CS["bg"], labelcolor=CS["text"], framealpha=0.7)

        dir_col = CS["bull"] if dir_ == "COMPRA" else CS["bear"]
        velas_txt = f" ({ex.get('velas_desde_cruce', 0)}v atrás)" if ex.get("velas_desde_cruce", 0) > 0 else ""
        fig.suptitle(
            f"{nombre}  —  MA Cross {'▲ COMPRA' if dir_ == 'COMPRA' else '▼ VENTA'}{velas_txt}",
            color=dir_col, fontsize=12, fontweight="bold"
        )
        _tag_hora(ax_p)

        _setup_ax(ax_r, f"RSI(14) = {ex.get('rsi', 50):.1f}")
        rsi_s = _calc_rsi_serie(c_full, 14).iloc[-n_velas:].values
        ax_r.plot(xs, rsi_s, color=CS["rsi_line"], linewidth=1.2)
        ax_r.axhline(70, color=CS["rsi_ob"], linewidth=0.8, linestyle="--", alpha=0.7)
        ax_r.axhline(30, color=CS["rsi_os"], linewidth=0.8, linestyle="--", alpha=0.7)
        ax_r.set_ylim(0, 100)
        ax_r.set_ylabel("RSI", fontsize=7)
        _etiquetar_eje_x(ax_r, sub)

        return _fig_a_bytes(fig)
    except Exception as e:
        print(f"    ⚠️ Error gráfico MA Cross: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# GRÁFICO BTC — EMA Cross + RSI
# ─────────────────────────────────────────────────────────────
def generar_grafico_btc(df, resultado):
    try:
        nombre  = resultado["nombre"]
        niv     = resultado["niveles"]
        dir_    = resultado["direccion"]
        ex      = resultado.get("extra", {})
        n_velas = 80

        fig = plt.figure(figsize=(10, 7), facecolor=CS["bg"])
        gs  = GridSpec(3, 1, figure=fig,
                       height_ratios=[3, 1, 1],
                       hspace=0.08, top=0.92, bottom=0.06,
                       left=0.07, right=0.90)
        ax_p = fig.add_subplot(gs[0])
        ax_v = fig.add_subplot(gs[1], sharex=ax_p)
        ax_r = fig.add_subplot(gs[2], sharex=ax_p)

        _setup_ax(ax_p)
        sub, xs = _dibujar_velas(ax_p, df, n_velas)

        c_full = df["Close"]
        ema9_s  = c_full.ewm(span=9,  adjust=False).mean().iloc[-n_velas:].values
        ema21_s = c_full.ewm(span=21, adjust=False).mean().iloc[-n_velas:].values
        ax_p.plot(xs, ema9_s,  color=CS["ema_fast"], linewidth=1.3, label="EMA9",  zorder=4)
        ax_p.plot(xs, ema21_s, color=CS["ema_slow"], linewidth=1.3, label="EMA21", zorder=4)

        y_min = float(sub["Low"].min()) * 0.995
        y_max = float(sub["High"].max()) * 1.005
        ax_p.set_ylim(y_min, y_max)
        _lineas_sl_tp(ax_p, xs, niv, y_min, y_max)
        ax_p.legend(loc="upper left", fontsize=7,
                    facecolor=CS["bg"], labelcolor=CS["text"], framealpha=0.7)

        dir_col = CS["bull"] if dir_ == "COMPRA" else CS["bear"]
        fig.suptitle(
            f"{nombre}  —  EMA Cross {'▲ COMPRA' if dir_ == 'COMPRA' else '▼ VENTA'}",
            color=dir_col, fontsize=12, fontweight="bold"
        )
        _tag_hora(ax_p)

        _setup_ax(ax_v, "Volumen")
        if "Volume" in df.columns:
            vol_s  = df["Volume"].iloc[-n_velas:].values
            cl_s   = df["Close"].iloc[-n_velas:].values
            op_s   = df["Open"].iloc[-n_velas:].values
            vcols  = [CS["vol_up"] if cl_s[i] >= op_s[i] else CS["vol_dn"]
                      for i in range(len(vol_s))]
            ax_v.bar(xs, vol_s, color=vcols, width=0.7, zorder=3)
            vol_ma = pd.Series(vol_s).rolling(20).mean()
            ax_v.plot(xs, vol_ma, color=CS["title"], linewidth=1, alpha=0.8)
        ax_v.set_ylabel("Vol", fontsize=7)
        plt.setp(ax_v.get_xticklabels(), visible=False)

        _setup_ax(ax_r, f"RSI(14) = {ex.get('rsi', 50):.1f}")
        rsi_s = _calc_rsi_serie(c_full, 14).iloc[-n_velas:].values
        ax_r.plot(xs, rsi_s, color=CS["rsi_line"], linewidth=1.2)
        ax_r.axhline(70, color=CS["rsi_ob"], linewidth=0.8, linestyle="--", alpha=0.7)
        ax_r.axhline(30, color=CS["rsi_os"], linewidth=0.8, linestyle="--", alpha=0.7)
        ax_r.fill_between(xs, rsi_s, 70, where=(rsi_s >= 70),
                          color=CS["rsi_ob"], alpha=0.15)
        ax_r.fill_between(xs, rsi_s, 30, where=(rsi_s <= 30),
                          color=CS["rsi_os"], alpha=0.15)
        ax_r.set_ylim(0, 100)
        ax_r.set_ylabel("RSI", fontsize=7)
        _etiquetar_eje_x(ax_r, sub)

        return _fig_a_bytes(fig)
    except Exception as e:
        print(f"    ⚠️ Error gráfico BTC: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# GRÁFICO GENERAL (Acciones, ETF, Índices)
# ─────────────────────────────────────────────────────────────
def generar_grafico_general(df, resultado):
    try:
        cat    = resultado["cat"]
        nombre = resultado["nombre"]
        niv    = resultado["niveles"]
        dir_   = resultado["direccion"]
        ex     = resultado.get("extra", {})
        n_velas = 60

        fig = plt.figure(figsize=(10, 7), facecolor=CS["bg"])
        gs  = GridSpec(3, 1, figure=fig,
                       height_ratios=[3, 1, 1],
                       hspace=0.08, top=0.92, bottom=0.06,
                       left=0.07, right=0.90)
        ax_p = fig.add_subplot(gs[0])
        ax_v = fig.add_subplot(gs[1], sharex=ax_p)
        ax_r = fig.add_subplot(gs[2], sharex=ax_p)

        _setup_ax(ax_p)
        sub, xs = _dibujar_velas(ax_p, df, n_velas)

        c_full = df["Close"]
        ec_n = ex.get("ec_n", 20)
        el_n = ex.get("el_n", 50)
        ema_c_s = c_full.ewm(span=ec_n, adjust=False).mean().iloc[-n_velas:].values
        ema_l_s = c_full.ewm(span=el_n, adjust=False).mean().iloc[-n_velas:].values
        ax_p.plot(xs, ema_c_s, color=CS["ema_fast"], linewidth=1.3,
                  label=f"EMA{ec_n}", zorder=4)
        ax_p.plot(xs, ema_l_s, color=CS["ema_slow"], linewidth=1.3,
                  label=f"EMA{el_n}", zorder=4)

        y_min = float(sub["Low"].min()) * 0.995
        y_max = float(sub["High"].max()) * 1.005
        ax_p.set_ylim(y_min, y_max)
        _lineas_sl_tp(ax_p, xs, niv, y_min, y_max)
        ax_p.legend(loc="upper left", fontsize=7,
                    facecolor=CS["bg"], labelcolor=CS["text"], framealpha=0.7)

        dir_col = CS["bull"] if dir_ == "COMPRA" else CS["bear"]
        fig.suptitle(
            f"{nombre}  —  {'▲ COMPRA' if dir_ == 'COMPRA' else '▼ VENTA'}",
            color=dir_col, fontsize=12, fontweight="bold"
        )
        _tag_hora(ax_p)

        _setup_ax(ax_v, "Volumen")
        if "Volume" in df.columns:
            vol_s = df["Volume"].iloc[-n_velas:].values
            cl_s  = df["Close"].iloc[-n_velas:].values
            op_s  = df["Open"].iloc[-n_velas:].values
            vcols = [CS["vol_up"] if cl_s[i] >= op_s[i] else CS["vol_dn"]
                     for i in range(len(vol_s))]
            ax_v.bar(xs, vol_s, color=vcols, width=0.7, zorder=3)
            ax_v.plot(xs, pd.Series(vol_s).rolling(20).mean(),
                      color=CS["title"], linewidth=1, alpha=0.8)
        ax_v.set_ylabel("Vol", fontsize=7)
        plt.setp(ax_v.get_xticklabels(), visible=False)

        _setup_ax(ax_r, f"RSI(14) = {ex.get('rsi', 50):.1f}")
        rsi_s = _calc_rsi_serie(c_full, 14).iloc[-n_velas:].values
        ax_r.plot(xs, rsi_s, color=CS["rsi_line"], linewidth=1.2)
        ax_r.axhline(70, color=CS["rsi_ob"], linewidth=0.8, linestyle="--", alpha=0.7)
        ax_r.axhline(30, color=CS["rsi_os"], linewidth=0.8, linestyle="--", alpha=0.7)
        ax_r.set_ylim(0, 100)
        ax_r.set_ylabel("RSI", fontsize=7)
        _etiquetar_eje_x(ax_r, sub)

        return _fig_a_bytes(fig)
    except Exception as e:
        print(f"    ⚠️ Error gráfico general: {e}")
        return None


def generar_grafico(df, resultado):
    cat = resultado.get("cat")
    try:
        if cat in ("ORO", "FOREX"):
            return generar_grafico_ma_cross(df, resultado)
        elif cat == "CRYPTO":
            return generar_grafico_btc(df, resultado)
        else:
            return generar_grafico_general(df, resultado)
    except Exception as e:
        print(f"    ⚠️ generar_grafico: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# SESIONES DE MERCADO
# ─────────────────────────────────────────────────────────────
TZ_ET  = timezone(timedelta(hours=-4))
TZ_CST = timezone(timedelta(hours=-6))

def en_sesion_ny():
    ahora = datetime.now(TZ_ET)
    if ahora.weekday() >= 5:
        return False
    hora = ahora.hour + ahora.minute / 60
    return 9.5 <= hora <= 16.0

def en_sesion_oro():
    """
    Sesiones Oro en CST (UTC-6):
    - NY mañana: 06:00–10:00
    - Asia:      18:00–20:30
    """
    ahora = datetime.now(TZ_CST)
    if ahora.weekday() >= 5:
        return False, "Sin sesión (fin de semana)"
    hora = ahora.hour + ahora.minute / 60
    if 6.0 <= hora < 10.0:
        return True, "🗽 NY Mañana (06:00–10:00 CST)"
    if 18.0 <= hora < 20.5:
        return True, "🌏 Asia (18:00–20:30 CST)"
    return False, "Fuera de sesión Oro"

def en_sesion_forex():
    """Forex opera lunes-viernes (cualquier hora)."""
    ahora = datetime.now(TZ_CST)
    return ahora.weekday() < 5

def sesion_activa(cat):
    if cat in ("ACCION", "ETF"):
        return en_sesion_ny()
    if cat == "ORO":
        activo, _ = en_sesion_oro()
        return activo
    if cat == "FOREX":
        return en_sesion_forex()
    return True  # CRYPTO, INDICE — 24h


# ─────────────────────────────────────────────────────────────
# DESCARGA DE DATOS
# ─────────────────────────────────────────────────────────────
UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]
SES = requests.Session()

def get_crumb():
    try:
        h = {"User-Agent": random.choice(UA), "Accept": "text/html,*/*",
             "Accept-Language": "en-US,en;q=0.5"}
        SES.get("https://finance.yahoo.com", headers=h, timeout=15)
        r = SES.get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                    headers=h, timeout=10)
        c = r.text.strip()
        return c if c and len(c) > 3 else None
    except:
        return None

def get_datos(sym, cr=None, tf="1d", dias=120):
    now = int(time.time())
    ini = now - (dias * 24 * 3600)
    for i in range(3):
        try:
            h = {"User-Agent": random.choice(UA), "Accept": "application/json,*/*",
                 "Accept-Language": "en-US,en;q=0.9",
                 "Referer": f"https://finance.yahoo.com/quote/{sym}"}
            srv = ["query1", "query2"][i % 2]
            url = (f"https://{srv}.finance.yahoo.com/v8/finance/chart/{sym}"
                   f"?period1={ini}&period2={now}&interval={tf}"
                   f"&includeAdjustedClose=true")
            if cr:
                url += f"&crumb={cr}"
            resp = SES.get(url, headers=h, timeout=25)
            if not resp.text.strip() or resp.status_code != 200:
                time.sleep(3); continue
            d   = resp.json()
            res = d.get("chart", {}).get("result", [])
            if not res:
                time.sleep(3); continue
            r  = res[0]
            ts = r.get("timestamp", [])
            cl = r.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", [])
            hi = r.get("indicators", {}).get("quote", [{}])[0].get("high", [])
            lo = r.get("indicators", {}).get("quote", [{}])[0].get("low", [])
            vo = r.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
            op = r.get("indicators", {}).get("quote", [{}])[0].get("open", [])
            if not ts or not cl:
                continue
            n = min(len(ts), len(cl),
                    len(hi) if hi else len(cl),
                    len(lo) if lo else len(cl))
            df = pd.DataFrame({
                "timestamp": ts[:n], "Close": cl[:n],
                "High":  hi[:n] if hi else cl[:n],
                "Low":   lo[:n] if lo else cl[:n],
                "Open":  op[:n] if op else cl[:n],
                "Volume": vo[:n] if vo else [None] * n,
            }).dropna(subset=["Close"])
            df["Date"] = pd.to_datetime(df["timestamp"], unit="s")
            return df.set_index("Date").sort_index()
        except:
            if i < 2:
                time.sleep(4 + i * 2)
    return None

def calc_rsi(s, n=14):
    d = s.diff()
    g = d.where(d > 0, 0.0)
    p = -d.where(d < 0, 0.0)
    return 100 - (100 / (1 + (g.rolling(n).mean() / p.rolling(n).mean())))

def calc_niveles(precio, direccion, cat):
    sl_pct = SL_TP[cat]["sl"] / 100
    tp_pct = SL_TP[cat]["tp"] / 100
    if direccion == "COMPRA":
        sl = round(precio * (1 - sl_pct), 5)
        tp = round(precio * (1 + tp_pct), 5)
    else:
        sl = round(precio * (1 + sl_pct), 5)
        tp = round(precio * (1 - tp_pct), 5)
    return {
        "entrada": precio, "sl": sl, "tp": tp,
        "rr": tp_pct / sl_pct,
        "sl_pct": SL_TP[cat]["sl"], "tp_pct": SL_TP[cat]["tp"],
    }


# ─────────────────────────────────────────────────────────────
# ESTRATEGIA MA CROSS SMA50/SMA100 — ORO y FOREX
# ─────────────────────────────────────────────────────────────
def analizar_ma_cross(df, sym, cat):
    """
    Señal SOLO cuando SMA50 cruza SMA100.
    Ventana de detección: últimas 3 velas (para no perder cruces por timing).
    Sin fallbacks. Si no hay cruce → None.

    Requiere mínimo 105 velas para SMA100 confiable.
    """
    c = df["Close"]
    n = len(c)

    if n < 105:
        print(f"    ⚠️ {sym}: solo {n} velas, necesita 105 para SMA50/SMA100")
        return None

    precio  = float(c.iloc[-1])
    ma50    = c.rolling(50).mean()
    ma100   = c.rolling(100).mean()
    rsi_val = float(calc_rsi(c).iloc[-1])

    senales           = []
    direccion         = None
    cruce_vela_idx    = None
    cruce_precio      = None
    velas_desde_cruce = 0

    # Buscar cruce en las últimas 3 velas
    for offset in range(1, 4):
        idx_ant = -(offset + 1)
        idx_act = -offset

        mr_ant = float(ma50.iloc[idx_ant])
        ml_ant = float(ma100.iloc[idx_ant])
        mr_act = float(ma50.iloc[idx_act])
        ml_act = float(ma100.iloc[idx_act])

        # Cruce alcista: SMA50 pasa de abajo a arriba de SMA100
        if mr_ant < ml_ant and mr_act >= ml_act:
            velas_desde_cruce = offset - 1
            cruce_precio      = float(c.iloc[idx_act])
            txt_atras = (f" (hace {velas_desde_cruce} vela{'s' if velas_desde_cruce != 1 else ''})"
                         if velas_desde_cruce > 0 else "")
            senales.append({
                "tipo": "⭐ MA CROSS ALCISTA — SMA50 cruzó ARRIBA SMA100",
                "a":    "✅ SEÑAL DE COMPRA",
                "desc": (f"SMA50=${mr_act:,.5f} cruzó ARRIBA SMA100=${ml_act:,.5f}. "
                         f"Zona verde activada.{txt_atras}"),
                "f":    "MUY FUERTE",
            })
            direccion = "COMPRA"
            break

        # Cruce bajista: SMA50 pasa de arriba a abajo de SMA100
        elif mr_ant > ml_ant and mr_act <= ml_act:
            velas_desde_cruce = offset - 1
            cruce_precio      = float(c.iloc[idx_act])
            txt_atras = (f" (hace {velas_desde_cruce} vela{'s' if velas_desde_cruce != 1 else ''})"
                         if velas_desde_cruce > 0 else "")
            senales.append({
                "tipo": "💀 MA CROSS BAJISTA — SMA50 cruzó ABAJO SMA100",
                "a":    "❌ SEÑAL DE VENTA",
                "desc": (f"SMA50=${mr_act:,.5f} cruzó ABAJO SMA100=${ml_act:,.5f}. "
                         f"Zona roja activada.{txt_atras}"),
                "f":    "MUY FUERTE",
            })
            direccion = "VENTA"
            break

    # Sin cruce en ventana de 3 velas → no hay señal
    if not senales or not direccion:
        return None

    # RSI como advertencia (no bloquea la señal)
    if direccion == "COMPRA" and rsi_val > 75:
        senales.append({
            "tipo": "⚠️ RSI SOBRECOMPRADO",
            "a":    "👀 PRECAUCIÓN",
            "desc": f"MA Cross dice COMPRA pero RSI={rsi_val:.0f}. Considera esperar retroceso.",
            "f":    "MEDIA",
        })
    elif direccion == "VENTA" and rsi_val < 25:
        senales.append({
            "tipo": "⚠️ RSI SOBREVENDIDO",
            "a":    "👀 PRECAUCIÓN",
            "desc": f"MA Cross dice VENTA pero RSI={rsi_val:.0f}. Considera esperar rebote.",
            "f":    "MEDIA",
        })

    n_velas_grafico = 120
    if cruce_precio is not None:
        cruce_vela_idx = n_velas_grafico - 1 - velas_desde_cruce

    return {
        "senales":           senales,
        "direccion":         direccion,
        "niveles":           calc_niveles(precio, direccion, cat),
        "sma50":             float(ma50.iloc[-1]),
        "sma100":            float(ma100.iloc[-1]),
        "rsi":               rsi_val,
        "cruce_vela_idx":    cruce_vela_idx,
        "cruce_precio":      cruce_precio,
        "velas_desde_cruce": velas_desde_cruce,
    }


# ─────────────────────────────────────────────────────────────
# ESTRATEGIA BTC — RSI Divergencia + EMA Cross (9/21)
# ─────────────────────────────────────────────────────────────
def analizar_btc(df, sym):
    """
    Señal cuando EMA9 cruza EMA21 Y el RSI no está en zona contraria extrema.

    - Cruce alcista EMA9 > EMA21 + RSI no sobrecomprado (< 70) → COMPRA
    - Cruce bajista EMA9 < EMA21 + RSI no sobrevendido  (> 30) → VENTA
    - Detección en ventana de 3 velas (igual que MA Cross)
    - Bonus: divergencia RSI como señal adicional informativa
    """
    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    n = len(c)

    if n < 30:
        return None

    precio  = float(c.iloc[-1])
    ema9    = c.ewm(span=9,  adjust=False).mean()
    ema21   = c.ewm(span=21, adjust=False).mean()
    rsi_ser = calc_rsi(c, 14)
    rsi_val = float(rsi_ser.iloc[-1])

    senales   = []
    direccion = None

    # ── EMA Cross en ventana 3 velas ──
    for offset in range(1, 4):
        idx_ant = -(offset + 1)
        idx_act = -offset

        e9_ant  = float(ema9.iloc[idx_ant])
        e21_ant = float(ema21.iloc[idx_ant])
        e9_act  = float(ema9.iloc[idx_act])
        e21_act = float(ema21.iloc[idx_act])

        # Cruce alcista
        if e9_ant < e21_ant and e9_act >= e21_act:
            # Filtro RSI: no sobrecomprado
            if rsi_val < 70:
                velas_atras = offset - 1
                txt = (f" (hace {velas_atras} vela{'s' if velas_atras != 1 else ''})"
                       if velas_atras > 0 else "")
                senales.append({
                    "tipo": "⭐ EMA CROSS ALCISTA (9/21)",
                    "a":    "✅ SEÑAL DE COMPRA",
                    "desc": (f"EMA9=${e9_act:,.2f} cruzó ARRIBA EMA21=${e21_act:,.2f}. "
                             f"RSI={rsi_val:.0f} (no sobrecomprado).{txt}"),
                    "f":    "MUY FUERTE",
                })
                direccion = "COMPRA"
            else:
                senales.append({
                    "tipo": "⚠️ EMA CROSS ALCISTA — RSI SOBRECOMPRADO",
                    "a":    "👀 SEÑAL FILTRADA",
                    "desc": (f"EMA9 cruzó arriba EMA21 PERO RSI={rsi_val:.0f} sobrecomprado. "
                             f"Señal bloqueada por filtro RSI."),
                    "f":    "BAJA",
                })
            break

        # Cruce bajista
        elif e9_ant > e21_ant and e9_act <= e21_act:
            if rsi_val > 30:
                velas_atras = offset - 1
                txt = (f" (hace {velas_atras} vela{'s' if velas_atras != 1 else ''})"
                       if velas_atras > 0 else "")
                senales.append({
                    "tipo": "💀 EMA CROSS BAJISTA (9/21)",
                    "a":    "❌ SEÑAL DE VENTA",
                    "desc": (f"EMA9=${e9_act:,.2f} cruzó ABAJO EMA21=${e21_act:,.2f}. "
                             f"RSI={rsi_val:.0f} (no sobrevendido).{txt}"),
                    "f":    "MUY FUERTE",
                })
                direccion = "VENTA"
            else:
                senales.append({
                    "tipo": "⚠️ EMA CROSS BAJISTA — RSI SOBREVENDIDO",
                    "a":    "👀 SEÑAL FILTRADA",
                    "desc": (f"EMA9 cruzó abajo EMA21 PERO RSI={rsi_val:.0f} sobrevendido. "
                             f"Señal bloqueada por filtro RSI."),
                    "f":    "BAJA",
                })
            break

    # Sin cruce → no hay señal
    if not direccion:
        return None

    # ── Divergencia RSI (informativa) ──
    # Busca si el precio hace nuevo máximo/mínimo pero el RSI no confirma
    if len(rsi_ser) >= 10:
        rsi_10 = rsi_ser.iloc[-10:].values
        c_10   = c.iloc[-10:].values
        if direccion == "COMPRA":
            # Divergencia alcista: precio hace nuevo mínimo pero RSI no
            if c_10[-1] < c_10[0] and rsi_10[-1] > rsi_10[0]:
                senales.append({
                    "tipo": "📐 DIVERGENCIA ALCISTA RSI",
                    "a":    "✅ CONFIRMACIÓN EXTRA",
                    "desc": (f"Precio bajó (${c_10[0]:,.2f}→${c_10[-1]:,.2f}) "
                             f"pero RSI subió ({rsi_10[0]:.0f}→{rsi_10[-1]:.0f}). "
                             f"Señal de reversión alcista confirmada."),
                    "f":    "FUERTE",
                })
        elif direccion == "VENTA":
            # Divergencia bajista: precio hace nuevo máximo pero RSI no
            if c_10[-1] > c_10[0] and rsi_10[-1] < rsi_10[0]:
                senales.append({
                    "tipo": "📐 DIVERGENCIA BAJISTA RSI",
                    "a":    "❌ CONFIRMACIÓN EXTRA",
                    "desc": (f"Precio subió (${c_10[0]:,.2f}→${c_10[-1]:,.2f}) "
                             f"pero RSI bajó ({rsi_10[0]:.0f}→{rsi_10[-1]:.0f}). "
                             f"Señal de reversión bajista confirmada."),
                    "f":    "FUERTE",
                })

    # ── Soporte/Resistencia como contexto ──
    if len(c) >= 50:
        mn50 = float(c.rolling(50).min().iloc[-1])
        mx50 = float(c.rolling(50).max().iloc[-1])
        dist_soporte    = ((precio - mn50) / mn50) * 100
        dist_resistencia = ((mx50 - precio) / mx50) * 100
        if direccion == "COMPRA" and dist_soporte <= 2.0:
            senales.append({
                "tipo": "🛡️ CERCA DE SOPORTE",
                "a":    "✅ CONTEXTO FAVORABLE",
                "desc": f"Precio a {dist_soporte:.1f}% del soporte de 50 velas (${mn50:,.2f}). Rebote probable.",
                "f":    "FUERTE",
            })
        elif direccion == "VENTA" and dist_resistencia <= 2.0:
            senales.append({
                "tipo": "🚧 CERCA DE RESISTENCIA",
                "a":    "❌ CONTEXTO FAVORABLE",
                "desc": f"Precio a {dist_resistencia:.1f}% de la resistencia de 50 velas (${mx50:,.2f}).",
                "f":    "FUERTE",
            })

    return {
        "senales":  senales,
        "direccion": direccion,
        "niveles":  calc_niveles(precio, "CRYPTO", "CRYPTO"),
        "rsi":      rsi_val,
        "ema9":     float(ema9.iloc[-1]),
        "ema21":    float(ema21.iloc[-1]),
    }


# ─────────────────────────────────────────────────────────────
# ESTRATEGIA GENERAL — Acciones, ETF, Índices
# ─────────────────────────────────────────────────────────────
def analizar_general(df, sym, cat):
    PARAMS = {
        "ACCION": {"rsi_b": 30, "rsi_a": 70, "cambio": 2.0, "ema_c": 20, "ema_l": 50},
        "ETF":    {"rsi_b": 35, "rsi_a": 65, "cambio": 1.5, "ema_c": 20, "ema_l": 50},
        "INDICE": {"rsi_b": 35, "rsi_a": 65, "cambio": 0.5, "ema_c": 9,  "ema_l": 21},
    }
    p = PARAMS.get(cat, PARAMS["ACCION"])
    c = df["Close"]
    precio      = float(c.iloc[-1])
    precio_ayer = float(c.iloc[-2])
    cd = ((precio - precio_ayer) / precio_ayer) * 100
    c7 = ((precio - float(c.iloc[-6])) / float(c.iloc[-6]) * 100) if len(c) >= 6 else 0
    ema_c   = c.ewm(span=p["ema_c"], adjust=False).mean()
    ema_l   = c.ewm(span=p["ema_l"], adjust=False).mean()
    rsi_v   = float(calc_rsi(c).iloc[-1])
    senales = []
    direccion = None

    if cd >= p["cambio"]:
        senales.append({"tipo": "📈 SUBIDA FUERTE", "a": "⚡ POSIBLE COMPRA",
                         "desc": f"Subió {cd:.2f}% en vela anterior", "f": "MEDIA"})
        direccion = "COMPRA"
    elif cd <= -p["cambio"]:
        senales.append({"tipo": "📉 CAÍDA FUERTE", "a": "⚠️ POSIBLE VENTA",
                         "desc": f"Cayó {abs(cd):.2f}% en vela anterior", "f": "MEDIA"})
        direccion = "VENTA"

    if rsi_v <= p["rsi_b"]:
        senales.append({"tipo": "🔵 RSI SOBREVENDIDO", "a": "✅ SEÑAL DE COMPRA",
                         "desc": f"RSI={rsi_v:.1f} bajo {p['rsi_b']}", "f": "FUERTE"})
        direccion = "COMPRA"
    elif rsi_v >= p["rsi_a"]:
        senales.append({"tipo": "🔴 RSI SOBRECOMPRADO", "a": "❌ SEÑAL DE VENTA",
                         "desc": f"RSI={rsi_v:.1f} sobre {p['rsi_a']}", "f": "FUERTE"})
        direccion = "VENTA"

    ec_h = float(ema_c.iloc[-1]); ec_a = float(ema_c.iloc[-2])
    el_h = float(ema_l.iloc[-1]); el_a = float(ema_l.iloc[-2])
    if ec_a < el_a and ec_h > el_h:
        senales.append({"tipo": "⭐ GOLDEN CROSS", "a": "✅ COMPRA FUERTE",
                         "desc": f"EMA{p['ema_c']} cruzó ARRIBA EMA{p['ema_l']}", "f": "MUY FUERTE"})
        direccion = "COMPRA"
    elif ec_a > el_a and ec_h < el_h:
        senales.append({"tipo": "💀 DEATH CROSS", "a": "❌ VENTA FUERTE",
                         "desc": f"EMA{p['ema_c']} cruzó ABAJO EMA{p['ema_l']}", "f": "MUY FUERTE"})
        direccion = "VENTA"

    if "Volume" in df.columns:
        v = df["Volume"].dropna()
        if len(v) > 20:
            va = float(v.iloc[-1]); vp = float(v.rolling(20).mean().iloc[-1])
            if vp > 0 and va / vp >= 2.0:
                senales.append({"tipo": "🔊 VOLUMEN INUSUAL", "a": "👀 ATENCIÓN",
                                  "desc": f"Volumen {va/vp:.1f}x el promedio de 20 velas", "f": "MEDIA"})

    if cat == "INDICE" and len(c) >= 50:
        mn = float(c.rolling(50).min().iloc[-1])
        mx = float(c.rolling(50).max().iloc[-1])
        dm = ((precio - mn) / mn) * 100
        dM = ((mx - precio) / mx) * 100
        if dm <= 1.5:
            senales.append({"tipo": "🛡️ CERCA SOPORTE 50v", "a": "✅ ZONA DE COMPRA",
                              "desc": f"A {dm:.2f}% del soporte (${mn:,.2f})", "f": "FUERTE"})
            direccion = "COMPRA"
        elif dM <= 1.5:
            senales.append({"tipo": "🚧 CERCA RESISTENCIA 50v", "a": "❌ ZONA DE VENTA",
                              "desc": f"A {dM:.2f}% de la resistencia (${mx:,.2f})", "f": "FUERTE"})
            direccion = "VENTA"

    if not senales or not direccion:
        return None

    return {
        "senales":   senales,
        "direccion": direccion,
        "niveles":   calc_niveles(precio, direccion, cat),
        "rsi":       rsi_v, "cd": cd, "c7": c7,
        "ec":        ec_h,  "el": el_h,
        "ec_n":      p["ema_c"], "el_n": p["ema_l"],
    }


# ─────────────────────────────────────────────────────────────
# DISPATCHER PRINCIPAL
# ─────────────────────────────────────────────────────────────
def analizar(sym, cr=None):
    try:
        cat = categoria(sym)
        cfg = INTERVALOS_MERCADO.get(cat, {"tf": "1d", "dias": 90})
        df  = get_datos(sym, cr, tf=cfg["tf"], dias=cfg["dias"])

        if df is None or len(df) < 30:
            return None

        _df_cache[sym] = df
        precio      = float(df["Close"].iloc[-1])
        precio_ayer = float(df["Close"].iloc[-2])
        cd = ((precio - precio_ayer) / precio_ayer) * 100
        c7 = ((precio - float(df["Close"].iloc[-6])) / float(df["Close"].iloc[-6]) * 100
              if len(df) >= 6 else 0)

        if cat in ("ORO", "FOREX"):
            res = analizar_ma_cross(df, sym, cat)
        elif cat == "CRYPTO":
            res = analizar_btc(df, sym)
        else:
            res = analizar_general(df, sym, cat)

        if not res:
            return None

        return {
            "sym":      sym,
            "nombre":   NOMBRES.get(sym, sym),
            "cat":      cat,
            "icono":    ICONOS[cat],
            "precio":   precio,
            "cd":       cd, "c7": c7,
            "senales":  res["senales"],
            "direccion": res["direccion"],
            "niveles":  res["niveles"],
            "extra":    res,
            "hora":     datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    except Exception as e:
        print(f"  ⚠️ {sym}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# GESTOR DE TRADES ACTIVOS
# ─────────────────────────────────────────────────────────────
def hay_trade_activo(sym):
    return sym in _trades_activos

def abrir_trade(sym, resultado):
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
    }

def cerrar_trade(sym):
    _trades_activos.pop(sym, None)

def verificar_trades_activos(resultados_nuevos):
    eventos = []
    precios = {r["sym"]: r["precio"] for r in resultados_nuevos}
    for sym in list(_trades_activos.keys()):
        trade  = _trades_activos[sym]
        precio = precios.get(sym)
        if precio is None:
            continue
        dir_   = trade["direccion"]
        entrada = trade["entrada"]
        sl, tp  = trade["sl"], trade["tp"]

        if dir_ == "COMPRA" and precio <= sl:
            eventos.append({"tipo": "SL_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio,
                             "resultado_pct": -trade["sl_pct"],
                             "ganancia_usd":  precio - entrada,
                             "mensaje": f"🛑 STOP LOSS — Pérdida de {trade['sl_pct']}%"})
            cerrar_trade(sym)
        elif dir_ == "VENTA" and precio >= sl:
            eventos.append({"tipo": "SL_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio,
                             "resultado_pct": -trade["sl_pct"],
                             "ganancia_usd":  entrada - precio,
                             "mensaje": f"🛑 STOP LOSS — Pérdida de {trade['sl_pct']}%"})
            cerrar_trade(sym)
        elif dir_ == "COMPRA" and precio >= tp:
            eventos.append({"tipo": "TP_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio,
                             "resultado_pct": trade["tp_pct"],
                             "ganancia_usd":  precio - entrada,
                             "mensaje": f"✅ TAKE PROFIT — Ganancia de {trade['tp_pct']}%"})
            cerrar_trade(sym)
        elif dir_ == "VENTA" and precio <= tp:
            eventos.append({"tipo": "TP_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio,
                             "resultado_pct": trade["tp_pct"],
                             "ganancia_usd":  entrada - precio,
                             "mensaje": f"✅ TAKE PROFIT — Ganancia de {trade['tp_pct']}%"})
            cerrar_trade(sym)
    return eventos

def filtrar_senales_nuevas(resultados):
    nuevos = []
    for r in resultados:
        sym = r["sym"]
        if sym not in _trades_activos:
            nuevos.append(r)
            abrir_trade(sym, r)
        else:
            actual = _trades_activos[sym]
            if actual["direccion"] != r["direccion"]:
                print(f"  🔄 {sym}: dirección cambió {actual['direccion']} → {r['direccion']}")
                cerrar_trade(sym)
                nuevos.append(r)
                abrir_trade(sym, r)
            else:
                print(f"  ⏸️  {sym}: trade activo ({actual['direccion']}) — esperando SL/TP")
    return nuevos


# ─────────────────────────────────────────────────────────────
# HTML DEL CORREO
# ─────────────────────────────────────────────────────────────
CAT_LABEL = {
    "ACCION": "ACCIONES",
    "ETF":    "ETFs",
    "ORO":    "ORO — MA Cross SMA50/SMA100 (30min)",
    "CRYPTO": "BTC — EMA Cross 9/21 + RSI Divergencia (30min)",
    "INDICE": "ÍNDICES — NAS100 & SP500 (15min)",
    "FOREX":  "FOREX — MA Cross SMA50/SMA100 (30min)",
}
CAT_DESC = {
    "ACCION": "RSI + EMA + Volumen. Opera solo 9:30-16:00 ET.",
    "ETF":    "RSI + EMA. SL 1.5% / TP 3%.",
    "ORO":    "Señal SOLO si SMA50 cruza SMA100. Sesiones: 06:00–10:00 CST | 18:00–20:30 CST. SL 1% / TP 2%.",
    "CRYPTO": "EMA9 cruza EMA21 + RSI no sobreextendido + Divergencia RSI. SL 3% / TP 6%.",
    "INDICE": "EMA9/21 + RSI + S/R. SL 1% / TP 2%.",
    "FOREX":  "Señal SOLO si SMA50 cruza SMA100. TF 30min. SL 0.5% / TP 1%.",
}

def construir_html(resultados, img_cids):
    grupos = {}
    for r in resultados:
        grupos.setdefault(r["cat"], []).append(r)
    body = ""

    for cat in ["ACCION", "ETF", "ORO", "CRYPTO", "INDICE", "FOREX"]:
        if cat not in grupos:
            continue
        body += (
            f'<div style="background:#0F3460;border-radius:10px;padding:10px 16px;margin:20px 0 8px;">'
            f'<span style="font-size:16px;">{ICONOS[cat]}</span> '
            f'<b style="color:#F5A623;font-size:14px;">{CAT_LABEL[cat]}</b>'
            f'<div style="color:#aaa;font-size:11px;margin-top:2px;">{CAT_DESC[cat]}</div></div>'
        )

        for r in grupos[cat]:
            col       = "#00B074" if r["cd"] >= 0 else "#E94560"
            sig       = "▲" if r["cd"] >= 0 else "▼"
            dir_col   = "#00B074" if r["direccion"] == "COMPRA" else "#E94560"
            dir_emoji = "✅" if r["direccion"] == "COMPRA" else "❌"
            niv       = r["niveles"]
            ex        = r.get("extra", {})

            img_blk = ""
            cid = img_cids.get(r["sym"])
            if cid:
                img_blk = (
                    f'<div style="margin:10px 0;border-radius:10px;overflow:hidden;">'
                    f'<img src="cid:{cid}" alt="Gráfico {r["nombre"]}" '
                    f'style="width:100%;max-width:600px;display:block;border-radius:8px;" />'
                    f'</div>'
                )

            bq = ""
            for s in r["senales"]:
                bg = "#1a3a2a" if "COMPRA" in s["a"] else "#3a1a1a" if "VENTA" in s["a"] else "#1a2a3a"
                bq += (
                    f'<div style="background:{bg};border-radius:8px;padding:7px 10px;margin:4px 0;">'
                    f'<b style="font-size:12px;">{s["tipo"]}</b> — '
                    f'<span style="color:#F5A623;font-size:12px;">{s["a"]}</span><br>'
                    f'<span style="color:#ccc;font-size:11px;">{s["desc"]}</span> '
                    f'<span style="background:#0F3460;padding:1px 5px;border-radius:6px;font-size:10px;">{s["f"]}</span>'
                    f'</div>'
                )

            op_html = (
                f'<div style="background:#0a1a0a;border:2px solid {dir_col};border-radius:12px;'
                f'padding:16px;margin:10px 0;">'
                f'<div style="text-align:center;margin-bottom:12px;">'
                f'<span style="font-size:22px;">{dir_emoji}</span> '
                f'<b style="font-size:18px;color:{dir_col};">{r["direccion"]}</b>'
                f'</div>'
                f'<table width="100%" style="border-collapse:collapse;">'
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">🎯 PRECIO DE ENTRADA</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#fff;">'
                f'${niv["entrada"]:,.5f}</td></tr>'
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">🛑 STOP LOSS ({niv["sl_pct"]}%)</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#E94560;">'
                f'${niv["sl"]:,.5f}</td></tr>'
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">✅ TAKE PROFIT ({niv["tp_pct"]}%)</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#00B074;">'
                f'${niv["tp"]:,.5f}</td></tr>'
                f'<tr>'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">⚖️ RIESGO:GANANCIA</td>'
                f'<td style="padding:8px;text-align:right;font-size:13px;font-weight:bold;color:#F5A623;">'
                f'1 : {niv["rr"]:.1f}</td></tr>'
                f'</table>'
                f'<div style="background:#0F3460;border-radius:8px;padding:8px;margin-top:10px;text-align:center;">'
                f'<span style="color:#aaa;font-size:11px;">⚠️ Precio referencial. Verifica en tu plataforma.</span>'
                f'</div></div>'
            )

            # Info extra por categoría
            info_extra = ""
            if cat in ("ORO", "FOREX"):
                velas_txt = ""
                if ex.get("velas_desde_cruce", 0) > 0:
                    velas_txt = f' | Cruce hace <b style="color:#E94560;">{ex["velas_desde_cruce"]} vela(s)</b>'
                sesion_txt = ""
                if cat == "ORO":
                    _, ses_nombre = en_sesion_oro()
                    sesion_txt = f'Sesión: <b style="color:#F5A623;">{ses_nombre}</b> | '
                info_extra = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                    f'{sesion_txt}TF: <b>30min</b> | '
                    f'SMA50: <b style="color:#4488ff;">${ex.get("sma50", 0):,.5f}</b> | '
                    f'SMA100: <b style="color:#44bb44;">${ex.get("sma100", 0):,.5f}</b> | '
                    f'RSI: <b style="color:#F5A623;">{ex.get("rsi", 0):.1f}</b>{velas_txt}'
                    f'</div>'
                )
            elif cat == "CRYPTO":
                info_extra = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                    f'TF: <b>30min</b> | '
                    f'EMA9: <b style="color:#4488ff;">${ex.get("ema9", 0):,.2f}</b> | '
                    f'EMA21: <b style="color:#FF8844;">${ex.get("ema21", 0):,.2f}</b> | '
                    f'RSI: <b style="color:#F5A623;">{ex.get("rsi", 0):.1f}</b>'
                    f'</div>'
                )
            elif cat == "INDICE":
                info_extra = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                    f'TF: <b>15min</b> | '
                    f'EMA{ex.get("ec_n",9)}: <b style="color:#4488ff;">${ex.get("ec",0):,.2f}</b> | '
                    f'EMA{ex.get("el_n",21)}: <b style="color:#FF8844;">${ex.get("el",0):,.2f}</b> | '
                    f'RSI: <b style="color:#F5A623;">{ex.get("rsi",0):.1f}</b>'
                    f'</div>'
                )

            body += (
                f'<div style="background:#16213E;border-radius:12px;padding:16px;margin:8px 0;'
                f'border-left:4px solid {dir_col};">'
                f'<table width="100%"><tr>'
                f'<td><b style="font-size:16px;color:#fff;">{r["nombre"]}</b><br>'
                f'<span style="color:#aaa;font-size:10px;">{r["hora"]}</span></td>'
                f'<td align="right"><b style="font-size:16px;color:#fff;">${r["precio"]:,.5f}</b><br>'
                f'<span style="color:{col};font-size:12px;">{sig} {abs(r["cd"]):.2f}% hoy</span></td>'
                f'</tr></table>'
                f'{info_extra}'
                f'{img_blk}'
                f'{op_html}'
                f'<details style="margin-top:8px;">'
                f'<summary style="color:#F5A623;font-size:11px;cursor:pointer;">'
                f'📊 Ver señales ({len(r["senales"])})</summary>'
                f'{bq}</details>'
                f'</div>'
            )

    return (
        f'<html><body style="margin:0;padding:0;background:#0D0D1A;'
        f'font-family:Arial,sans-serif;color:#fff;">'
        f'<div style="max-width:640px;margin:0 auto;padding:20px;">'
        f'<div style="background:#1A1A2E;border-radius:16px;padding:20px;'
        f'text-align:center;margin-bottom:8px;">'
        f'<div style="font-size:28px;">📊</div>'
        f'<h1 style="margin:6px 0;font-size:20px;color:#F5A623;">ALERTA DE TRADING</h1>'
        f'<p style="color:#aaa;font-size:12px;">{len(resultados)} activo(s) — '
        f'{datetime.now().strftime("%d/%m/%Y %H:%M")}</p>'
        f'<p style="color:#555;font-size:10px;">⚠️ No es asesoría financiera.</p>'
        f'</div>'
        f'{body}'
        f'<div style="text-align:center;color:#555;font-size:10px;padding:10px;'
        f'border-top:1px solid #222;margin-top:12px;">'
        f'Bot Trading v10.0 | Oro+Forex: SMA50/100 30min c/3min | BTC: EMA9/21 RSI 30min</div>'
        f'</div></body></html>'
    )


# ─────────────────────────────────────────────────────────────
# ENVÍO DE ALERTAS
# ─────────────────────────────────────────────────────────────
def enviar(res):
    try:
        img_cids  = {}
        img_bytes = {}

        for r in res:
            sym = r["sym"]
            df  = _df_cache.get(sym)
            if df is not None:
                print(f"  📈 Generando gráfico {sym}...", end=" ")
                data = generar_grafico(df, r)
                if data:
                    cid = f"chart_{sym.replace('=','_').replace('-','_').replace('/','_')}"
                    img_cids[sym]  = cid
                    img_bytes[sym] = data
                    print("✅")
                else:
                    print("⚠️")

        msg     = MIMEMultipart("related")
        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)

        cats = set(r["cat"] for r in res)
        msg["Subject"] = (f"🚨 {len(res)} Alerta(s) [{', '.join(cats)}] — "
                          f"{datetime.now().strftime('%d/%m/%Y %H:%M')}")
        msg["From"] = EMAIL_REMITENTE
        msg["To"]   = EMAIL_DESTINO

        txt = "\n".join([
            f"{r['nombre']}: ${r['precio']:,.5f} ({r['cd']:+.2f}%) → {r['direccion']} "
            f"| SL:${r['niveles']['sl']:,.5f} TP:${r['niveles']['tp']:,.5f}"
            for r in res
        ])
        msg_alt.attach(MIMEText(txt, "plain"))
        msg_alt.attach(MIMEText(construir_html(res, img_cids), "html"))

        for sym, data in img_bytes.items():
            cid = img_cids[sym]
            img_part = MIMEImage(data, "png")
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            msg.attach(img_part)

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.sendmail(EMAIL_REMITENTE, EMAIL_DESTINO, msg.as_string())
        print(f"  ✅ Correo enviado — {len(res)} alerta(s) con {len(img_bytes)} gráfico(s)")
    except Exception as e:
        print(f"  ❌ Error correo: {e}")


def enviar_cierre_trades(eventos):
    try:
        msg = MIMEMultipart("alternative")
        cierres_tp = [e for e in eventos if e["tipo"] == "TP_TOCADO"]
        cierres_sl = [e for e in eventos if e["tipo"] == "SL_TOCADO"]
        icono = ("✅" if cierres_tp and not cierres_sl
                 else "🛑" if cierres_sl and not cierres_tp else "📊")
        msg["Subject"] = (f"{icono} {len(eventos)} Trade(s) Cerrado(s) — "
                          f"{datetime.now().strftime('%d/%m/%Y %H:%M')}")
        msg["From"] = EMAIL_REMITENTE
        msg["To"]   = EMAIL_DESTINO

        filas = ""
        for ev in eventos:
            t = ev["trade"]
            es_tp    = ev["tipo"] == "TP_TOCADO"
            col_res  = "#00B074" if es_tp else "#E94560"
            icono_r  = "✅ GANANCIA" if es_tp else "🛑 PÉRDIDA"
            signo    = "+" if es_tp else "-"
            diff     = abs(ev["precio_cierre"] - t["entrada"])
            filas += (
                f'<div style="background:#16213E;border-radius:12px;padding:18px;margin:12px 0;'
                f'border-left:6px solid {col_res};">'
                f'<table width="100%"><tr>'
                f'<td><b style="font-size:18px;color:#fff;">{t["nombre"]}</b><br>'
                f'<span style="color:#aaa;font-size:11px;">Cerrado: {datetime.now().strftime("%d/%m/%Y %H:%M")}'
                f'</span></td>'
                f'<td align="right">'
                f'<span style="font-size:22px;font-weight:bold;color:{col_res};">{icono_r}</span><br>'
                f'<span style="font-size:16px;color:{col_res};font-weight:bold;">'
                f'{signo}{abs(ev["resultado_pct"]):.1f}%</span>'
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
                f'<td style="color:{col_res};font-weight:bold;padding:6px 10px;">${ev["precio_cierre"]:,.5f}</td>'
                f'<td style="color:{col_res};font-weight:bold;padding:6px 10px;">{signo}${diff:,.5f}</td>'
                f'</tr></table>'
                f'<div style="background:{col_res}22;border:1px solid {col_res};border-radius:8px;'
                f'padding:10px;text-align:center;margin-top:8px;">'
                f'<b style="color:{col_res};font-size:14px;">{ev["mensaje"]}</b>'
                f'</div>'
                f'<div style="color:#555;font-size:10px;margin-top:8px;text-align:center;">'
                f'Abierto: {t["hora"]} | SL: ${t["sl"]:,.5f} | TP: ${t["tp"]:,.5f}'
                f'</div></div>'
            )

        col_res = ("#00B074" if cierres_tp and not cierres_sl
                   else "#E94560" if cierres_sl and not cierres_tp else "#F5A623")
        html_body = (
            f'<html><body style="margin:0;padding:0;background:#0D0D1A;'
            f'font-family:Arial,sans-serif;color:#fff;">'
            f'<div style="max-width:640px;margin:0 auto;padding:20px;">'
            f'<div style="background:#1A1A2E;border-radius:16px;padding:20px;'
            f'text-align:center;margin-bottom:12px;">'
            f'<h1 style="margin:6px 0;font-size:20px;color:{col_res};">TRADE(S) CERRADO(S)</h1>'
            f'<p style="color:#aaa;font-size:12px;">'
            f'{"✅ " + str(len(cierres_tp)) + " Take Profit" if cierres_tp else ""} '
            f'{"🛑 " + str(len(cierres_sl)) + " Stop Loss" if cierres_sl else ""}'
            f'</p></div>{filas}'
            f'<div style="text-align:center;color:#555;font-size:10px;padding:10px;'
            f'border-top:1px solid #222;margin-top:12px;">'
            f'Bot Trading v10.0 — Registro automático</div>'
            f'</div></body></html>'
        )

        txt = "\n".join([f"{ev['trade']['nombre']}: {ev['mensaje']} | ${ev['precio_cierre']:,.5f}"
                         for ev in eventos])
        msg.attach(MIMEText(txt, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.sendmail(EMAIL_REMITENTE, EMAIL_DESTINO, msg.as_string())
        print(f"  ✅ Correo cierre enviado — {len(eventos)} trade(s)")
    except Exception as e:
        print(f"  ❌ Error correo cierre: {e}")


# ─────────────────────────────────────────────────────────────
# CONTROL DE REVISIÓN Y LOOP
# ─────────────────────────────────────────────────────────────
def debe_revisar(cat):
    if not sesion_activa(cat):
        return False
    cada = INTERVALOS_MERCADO.get(cat, {"cada_min": 30})["cada_min"] * 60
    ult  = _ultimo_chequeo.get(cat, 0)
    return (time.time() - ult) >= cada

def marcar_revisado(cat):
    _ultimo_chequeo[cat] = time.time()

def revisar():
    print(f"\n{'=' * 60}")
    print(f"  🔍 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'=' * 60}")

    cats_a_revisar = [c for c in ["ACCION", "ETF", "ORO", "CRYPTO", "FOREX", "INDICE"]
                      if debe_revisar(c)]
    activos_a_revisar = [s for s in TODOS if categoria(s) in cats_a_revisar]

    if not activos_a_revisar:
        print("  ⏳ Ningún mercado activo ahora")
        for cat in ["ACCION", "ETF", "ORO", "CRYPTO", "FOREX", "INDICE"]:
            cfg    = INTERVALOS_MERCADO.get(cat, {"cada_min": 30, "tf": "?"})
            ult    = _ultimo_chequeo.get(cat, 0)
            faltan = max(0, int((ult + cfg["cada_min"] * 60 - time.time()) / 60))
            activo = sesion_activa(cat)
            estado = "🟢 activo" if activo else "🔴 fuera de sesión"
            print(f"    {ICONOS.get(cat,'📊')} {cat:6} [{cfg['tf']}] {estado} — próx. en {faltan} min")
        return

    print(f"  📋 Revisando: {', '.join(cats_a_revisar)}")
    cr = get_crumb()
    print(f"  {'✅ Yahoo OK' if cr else '⚠️ Sin crumb'}")

    todos_resultados = []
    for sym in activos_a_revisar:
        cat = categoria(sym)
        tf  = INTERVALOS_MERCADO.get(cat, {}).get("tf", "?")
        print(f"  [{cat:6}|{tf}] {sym}...", end=" ", flush=True)
        r = analizar(sym, cr)
        if r:
            print(f"⚠️  señal → {r['direccion']} | "
                  f"Entrada:${r['niveles']['entrada']:,.4f} "
                  f"SL:${r['niveles']['sl']:,.4f} "
                  f"TP:${r['niveles']['tp']:,.4f}")
            todos_resultados.append(r)
        else:
            print("✅ Sin señal")
        time.sleep(2)

    for cat in cats_a_revisar:
        marcar_revisado(cat)

    eventos_cierre = verificar_trades_activos(todos_resultados)
    if eventos_cierre:
        print(f"\n  🔔 {len(eventos_cierre)} trade(s) cerrado(s)")
        for ev in eventos_cierre:
            print(f"    {ev['sym']}: {ev['mensaje']}")
        enviar_cierre_trades(eventos_cierre)

    senales_nuevas = filtrar_senales_nuevas(todos_resultados)

    if _trades_activos:
        print(f"\n  📂 Trades activos: {len(_trades_activos)}")
        for sym, t in _trades_activos.items():
            print(f"    {sym}: {t['direccion']} desde ${t['entrada']:,.4f} "
                  f"| SL:${t['sl']:,.4f} | TP:${t['tp']:,.4f}")

    print(f"\n  📊 {len(senales_nuevas)} señal(es) NUEVA(s) / {len(todos_resultados)} detectadas")
    if senales_nuevas:
        enviar(senales_nuevas)
    elif not eventos_cierre:
        print("  😴 Sin señales nuevas")


# ─────────────────────────────────────────────────────────────
# ENTRADA PRINCIPAL
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════════╗
║   🤖 BOT DE TRADING v10.0                               ║
╠══════════════════════════════════════════════════════════╣
║  ORO   (GC=F):    SMA50/SMA100 — 30min — c/3min         ║
║  FOREX ({len(FOREX)} pares): SMA50/SMA100 — 30min — c/3min         ║
║  BTC:             EMA9/21 + RSI Divergencia — 30min      ║
║  NAS100/SP500:    EMA9/21 + RSI + S/R — 15min            ║
║  Acciones ({len(ACCIONES)}): RSI + EMA + Volumen — 1d            ║
║  ETFs    ({len(ETFS)}): RSI + EMA — 1d                       ║
╠══════════════════════════════════════════════════════════╣
║  SEÑALES ORO/FOREX: SOLO cruce real SMA50/SMA100        ║
║  SEÑALES BTC:       EMA9 cruza EMA21 + RSI filtro       ║
║  Sin fallbacks — sin señales débiles                     ║
╠══════════════════════════════════════════════════════════╣
║  SL/TP: Forex 0.5/1% | Oro 1/2% | BTC 3/6%             ║
║         Acciones 2/4% | ETF 1.5/3% | Índices 1/2%      ║
╠══════════════════════════════════════════════════════════╣
║  Horarios ORO (CST): 06:00-10:00 | 18:00-20:30          ║
║  Forex: Lun-Vie 24h | BTC/Índices: 24h                  ║
╚══════════════════════════════════════════════════════════╝""")
    revisar()
    while True:
        time.sleep(60)   # Loop cada 60s — suficiente para revisión cada 3min
        revisar()
