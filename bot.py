"""
Bot de Alertas de Trading v9.1 — FIXES APLICADOS
─────────────────────────────────────────────────
FIX 1 (ORO):  analizar_ma_cross ahora detecta cruces en ventana de 3 velas
              para no perder señales por timing del bot.
              También usa SMA50/SMA100 fijas (igual que Pine Script) en vez
              de cambiar a EMA20/50 según cantidad de datos.

FIX 2 (FOREX): analizar_smc ahora también busca cruces recientes (3 velas)
               y añade modo "mejor oportunidad" igual que CRYPTO/INDICE,
               para que siempre haya una dirección cuando hay tendencia clara.

- Acciones/ETF: RSI + EMA + Volumen
- Forex: SMC (BOS, CHoCH, Order Block, FVG, Liquidity Sweep)
- Oro: MA Cross SMA50/SMA100 — ventana 3 velas — timeframes 15m/30m/1h/1d
- BTC / NAS100 / SP500: RSI + EMA + Soporte/Resistencia — TF 15m (24h)
- TODOS: Precio entrada, Stop Loss y Take Profit exactos
- v8: Imagen del gráfico con indicadores en el correo
- v9: NAS100 + SP500 añadidos | BTC/índices en TF 15min | modo "mejor oportunidad"
- v9.1: Fix timing MA Cross Oro | Fix SMC Forex siempre con dirección
"""
import requests, pandas as pd, numpy as np
import smtplib, time, os, random, io, base64
import matplotlib
matplotlib.use("Agg")   # Sin GUI — necesario en servidor
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
from matplotlib.lines import Line2D
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime

EMAIL_REMITENTE  = os.environ.get("EMAIL_REMITENTE",  "tu_correo@gmail.com")
EMAIL_CONTRASENA = os.environ.get("EMAIL_CONTRASENA", "xxxx xxxx xxxx xxxx")
EMAIL_DESTINO    = os.environ.get("EMAIL_DESTINO",    "tu_correo@gmail.com")
INTERVALO_MIN    = int(os.environ.get("INTERVALO_MINUTOS", "30"))

# ── Timeframes y periodos ────────────────────────────────────
FOREX_INTERVALO = os.environ.get("FOREX_INTERVALO", "1h")
FOREX_PERIODO   = int(os.environ.get("FOREX_PERIODO_DIAS", "30"))

INTERVALOS_MERCADO = {
    "FOREX":   {"tf": FOREX_INTERVALO, "dias": FOREX_PERIODO, "cada_min": 60},
    "ORO":     {"tf": "15m", "dias": 7,   "cada_min": 15},
    "CRYPTO":  {"tf": "15m", "dias": 7,   "cada_min": 15},
    "INDICE":  {"tf": "15m", "dias": 7,   "cada_min": 15},
    "ACCION":  {"tf": "1d",  "dias": 90,  "cada_min": 1440},
    "ETF":     {"tf": "1d",  "dias": 90,  "cada_min": 1440},
}

_ultimo_chequeo = {}
_trades_activos = {}

# ──────────────────────────────────────────────────────────────
# PALETA DE COLORES DEL GRÁFICO (tema oscuro profesional)
# ──────────────────────────────────────────────────────────────
CHART_STYLE = {
    "bg":         "#0D0D1A",
    "panel_bg":   "#16213E",
    "grid":       "#1e2a44",
    "text":       "#CCCCCC",
    "title":      "#F5A623",
    "bull":       "#00B074",
    "bear":       "#E94560",
    "ema_fast":   "#4488FF",
    "ema_slow":   "#FF8844",
    "sma50":      "#4488FF",
    "sma100":     "#44BB44",
    "rsi_line":   "#9B59B6",
    "rsi_ob":     "#E94560",
    "rsi_os":     "#00B074",
    "vol_up":     "#00B07466",
    "vol_dn":     "#E9456066",
    "sl_line":    "#E94560",
    "tp_line":    "#00B074",
    "entry_line": "#F5A623",
}

def _setup_ax(ax, title=""):
    ax.set_facecolor(CHART_STYLE["panel_bg"])
    ax.tick_params(colors=CHART_STYLE["text"], labelsize=8)
    ax.yaxis.label.set_color(CHART_STYLE["text"])
    ax.xaxis.label.set_color(CHART_STYLE["text"])
    for spine in ax.spines.values():
        spine.set_edgecolor(CHART_STYLE["grid"])
    ax.grid(True, color=CHART_STYLE["grid"], linewidth=0.5, alpha=0.7)
    if title:
        ax.set_title(title, color=CHART_STYLE["text"], fontsize=9, pad=4)

def _dibujar_velas(ax, df, n=60):
    sub = df.tail(n).copy().reset_index()
    xs  = range(len(sub))
    for i, row in sub.iterrows():
        o, c, h, l = row["Open"], row["Close"], row["High"], row["Low"]
        color = CHART_STYLE["bull"] if c >= o else CHART_STYLE["bear"]
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        body_h = abs(c - o) if abs(c - o) > 0 else (h - l) * 0.01
        rect = plt.Rectangle((i - 0.35, min(o, c)), 0.7, body_h,
                              color=color, zorder=3)
        ax.add_patch(rect)
    return sub, xs

def _lineas_sl_tp(ax, xs, niveles, y_min, y_max):
    e, sl, tp = niveles["entrada"], niveles["sl"], niveles["tp"]
    for val, col, lbl, ls in [
        (e,  CHART_STYLE["entry_line"], f'Entrada ${e:,.5g}', "--"),
        (sl, CHART_STYLE["sl_line"],    f'SL ${sl:,.5g}',     ":"),
        (tp, CHART_STYLE["tp_line"],    f'TP ${tp:,.5g}',     ":"),
    ]:
        if y_min <= val <= y_max * 1.1:
            ax.axhline(val, color=col, linewidth=1.2, linestyle=ls, alpha=0.9, zorder=5)
            ax.text(len(xs) - 0.5, val, f" {lbl}", color=col,
                    fontsize=7, va="center", ha="left", zorder=6,
                    bbox=dict(fc=CHART_STYLE["bg"], ec="none", pad=1))

# ──────────────────────────────────────────────────────────────
# GENERADORES DE GRÁFICO POR ESTRATEGIA
# ──────────────────────────────────────────────────────────────
def generar_grafico_general(df, resultado):
    """RSI + EMA + Volumen — para Acciones, ETF y Crypto."""
    try:
        cat    = resultado["cat"]
        nombre = resultado["nombre"]
        niv    = resultado["niveles"]
        dir_   = resultado["direccion"]
        ex     = resultado.get("extra", {})
        n_velas = 60 if cat == "CRYPTO" else 50

        fig = plt.figure(figsize=(10, 7), facecolor=CHART_STYLE["bg"])
        gs  = GridSpec(3, 1, figure=fig,
                       height_ratios=[3, 1, 1],
                       hspace=0.08, top=0.92, bottom=0.06,
                       left=0.07, right=0.90)

        ax_precio = fig.add_subplot(gs[0])
        ax_vol    = fig.add_subplot(gs[1], sharex=ax_precio)
        ax_rsi    = fig.add_subplot(gs[2], sharex=ax_precio)

        _setup_ax(ax_precio)
        sub, xs = _dibujar_velas(ax_precio, df, n_velas)

        c_full  = df["Close"]
        ec_name = ex.get("ec_n", 20)
        el_name = ex.get("el_n", 50)
        ema_c_full = c_full.ewm(span=ec_name, adjust=False).mean()
        ema_l_full = c_full.ewm(span=el_name, adjust=False).mean()
        ema_c_sub  = ema_c_full.iloc[-n_velas:].values
        ema_l_sub  = ema_l_full.iloc[-n_velas:].values
        ax_precio.plot(xs, ema_c_sub, color=CHART_STYLE["ema_fast"],
                       linewidth=1.3, label=f"EMA{ec_name}", zorder=4)
        ax_precio.plot(xs, ema_l_sub, color=CHART_STYLE["ema_slow"],
                       linewidth=1.3, label=f"EMA{el_name}", zorder=4)

        y_min = float(sub["Low"].min()) * 0.995
        y_max = float(sub["High"].max()) * 1.005
        ax_precio.set_ylim(y_min, y_max)
        _lineas_sl_tp(ax_precio, xs, niv, y_min, y_max)
        ax_precio.legend(loc="upper left", fontsize=7,
                         facecolor=CHART_STYLE["bg"],
                         labelcolor=CHART_STYLE["text"], framealpha=0.7)
        dir_col = CHART_STYLE["bull"] if dir_ == "COMPRA" else CHART_STYLE["bear"]
        fig.suptitle(
            f"{nombre}  —  {'▲ COMPRA' if dir_ == 'COMPRA' else '▼ VENTA'}",
            color=dir_col, fontsize=12, fontweight="bold", x=0.5
        )
        _tag_hora(ax_precio)

        _setup_ax(ax_vol, "Volumen")
        if "Volume" in df.columns:
            vol_sub = df["Volume"].iloc[-n_velas:].values
            cl_sub  = df["Close"].iloc[-n_velas:].values
            op_sub  = df["Open"].iloc[-n_velas:].values
            vcols   = [CHART_STYLE["vol_up"] if cl_sub[i] >= op_sub[i]
                       else CHART_STYLE["vol_dn"] for i in range(len(vol_sub))]
            ax_vol.bar(xs, vol_sub, color=vcols, width=0.7, zorder=3)
            vol_ma = pd.Series(vol_sub).rolling(20).mean()
            ax_vol.plot(xs, vol_ma, color=CHART_STYLE["title"],
                        linewidth=1, alpha=0.8)
        ax_vol.set_ylabel("Vol", fontsize=7)
        plt.setp(ax_vol.get_xticklabels(), visible=False)

        _setup_ax(ax_rsi, f"RSI(14) = {ex.get('rsi', 50):.1f}")
        rsi_full = _calc_rsi_serie(df["Close"], 14)
        rsi_sub  = rsi_full.iloc[-n_velas:].values
        ax_rsi.plot(xs, rsi_sub, color=CHART_STYLE["rsi_line"],
                    linewidth=1.2, zorder=4)
        ax_rsi.axhline(70, color=CHART_STYLE["rsi_ob"],
                       linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.axhline(30, color=CHART_STYLE["rsi_os"],
                       linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.fill_between(xs, rsi_sub, 70,
                            where=(rsi_sub >= 70),
                            color=CHART_STYLE["rsi_ob"], alpha=0.15)
        ax_rsi.fill_between(xs, rsi_sub, 30,
                            where=(rsi_sub <= 30),
                            color=CHART_STYLE["rsi_os"], alpha=0.15)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI", fontsize=7)
        _etiquetar_eje_x(ax_rsi, sub)

        return _fig_a_bytes(fig)
    except Exception as e:
        print(f"    ⚠️ Error gráfico general: {e}")
        return None


def generar_grafico_ma_cross(df, resultado):
    """MA Cross SMA50/SMA100 + RSI — para Oro."""
    try:
        nombre  = resultado["nombre"]
        niv     = resultado["niveles"]
        dir_    = resultado["direccion"]
        ex      = resultado.get("extra", {})
        n_velas = 120

        fig = plt.figure(figsize=(10, 6.5), facecolor=CHART_STYLE["bg"])
        gs  = GridSpec(2, 1, figure=fig,
                       height_ratios=[3, 1],
                       hspace=0.08, top=0.92, bottom=0.06,
                       left=0.07, right=0.90)
        ax_precio = fig.add_subplot(gs[0])
        ax_rsi    = fig.add_subplot(gs[1], sharex=ax_precio)

        _setup_ax(ax_precio)
        sub, xs = _dibujar_velas(ax_precio, df, n_velas)

        c_full   = df["Close"]
        sma50_f  = c_full.rolling(50).mean()
        sma100_f = c_full.rolling(100).mean()
        sma50_s  = sma50_f.iloc[-n_velas:].values
        sma100_s = sma100_f.iloc[-n_velas:].values

        ax_precio.plot(xs, sma50_s,  color=CHART_STYLE["sma50"],
                       linewidth=1.5, label="SMA50",  zorder=4)
        ax_precio.plot(xs, sma100_s, color=CHART_STYLE["sma100"],
                       linewidth=1.5, label="SMA100", zorder=4)

        # ── Marcar el punto del cruce con una X negra (igual que Pine Script) ──
        cruce_x = ex.get("cruce_vela_idx")
        cruce_y = ex.get("cruce_precio")
        if cruce_x is not None and cruce_y is not None:
            ax_precio.plot(cruce_x, cruce_y, 'x',
                           color='black', markersize=12, markeredgewidth=3,
                           zorder=8, label="Cruce MA")

        valid = ~(np.isnan(sma50_s) | np.isnan(sma100_s))
        ax_precio.fill_between(
            [i for i in xs if valid[i]],
            sma50_s[valid], sma100_s[valid],
            where=(sma50_s[valid] >= sma100_s[valid]),
            color=CHART_STYLE["bull"], alpha=0.10, label="Zona alcista")
        ax_precio.fill_between(
            [i for i in xs if valid[i]],
            sma50_s[valid], sma100_s[valid],
            where=(sma50_s[valid] < sma100_s[valid]),
            color=CHART_STYLE["bear"], alpha=0.10, label="Zona bajista")

        y_min = float(sub["Low"].min()) * 0.995
        y_max = float(sub["High"].max()) * 1.005
        ax_precio.set_ylim(y_min, y_max)
        _lineas_sl_tp(ax_precio, xs, niv, y_min, y_max)
        ax_precio.legend(loc="upper left", fontsize=7,
                         facecolor=CHART_STYLE["bg"],
                         labelcolor=CHART_STYLE["text"], framealpha=0.7)

        dir_col = CHART_STYLE["bull"] if dir_ == "COMPRA" else CHART_STYLE["bear"]
        r_nom = ex.get("r_nom", "SMA50")
        l_nom = ex.get("l_nom", "SMA100")
        velas_atras = ex.get("velas_desde_cruce", 0)
        titulo_extra = f" ({velas_atras}v atrás)" if velas_atras > 0 else ""
        fig.suptitle(
            f"{nombre}  —  MA Cross {'▲ COMPRA' if dir_ == 'COMPRA' else '▼ VENTA'}{titulo_extra}",
            color=dir_col, fontsize=12, fontweight="bold"
        )
        _tag_hora(ax_precio)

        _setup_ax(ax_rsi, f"RSI(14) = {ex.get('rsi', 50):.1f}")
        rsi_full = _calc_rsi_serie(c_full, 14)
        rsi_sub  = rsi_full.iloc[-n_velas:].values
        ax_rsi.plot(xs, rsi_sub, color=CHART_STYLE["rsi_line"], linewidth=1.2)
        ax_rsi.axhline(70, color=CHART_STYLE["rsi_ob"],  linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.axhline(30, color=CHART_STYLE["rsi_os"],  linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI", fontsize=7)
        _etiquetar_eje_x(ax_rsi, sub)

        return _fig_a_bytes(fig)
    except Exception as e:
        print(f"    ⚠️ Error gráfico MA Cross: {e}")
        return None


def generar_grafico_smc(df, resultado):
    """Gráfico SMC para Forex."""
    try:
        nombre  = resultado["nombre"]
        niv     = resultado["niveles"]
        dir_    = resultado["direccion"]
        senales = resultado.get("senales", [])
        n_velas = 80

        fig = plt.figure(figsize=(10, 7), facecolor=CHART_STYLE["bg"])
        gs  = GridSpec(2, 1, figure=fig,
                       height_ratios=[3, 1],
                       hspace=0.08, top=0.92, bottom=0.06,
                       left=0.07, right=0.90)
        ax_precio = fig.add_subplot(gs[0])
        ax_rsi    = fig.add_subplot(gs[1], sharex=ax_precio)

        _setup_ax(ax_precio)
        sub, xs = _dibujar_velas(ax_precio, df, n_velas)

        c_full = df["Close"]
        ema20_s = c_full.ewm(span=20, adjust=False).mean().iloc[-n_velas:].values
        ema50_s = c_full.ewm(span=50, adjust=False).mean().iloc[-n_velas:].values
        ax_precio.plot(xs, ema20_s, color=CHART_STYLE["ema_fast"],
                       linewidth=1.2, label="EMA20", zorder=4)
        ax_precio.plot(xs, ema50_s, color=CHART_STYLE["ema_slow"],
                       linewidth=1.2, label="EMA50", zorder=4)

        y_min = float(sub["Low"].min()) * 0.995
        y_max = float(sub["High"].max()) * 1.005
        ax_precio.set_ylim(y_min, y_max)
        _lineas_sl_tp(ax_precio, xs, niv, y_min, y_max)

        smc_icons = {
            "BOS":      ("▲" if dir_ == "COMPRA" else "▼", CHART_STYLE["bull"] if dir_ == "COMPRA" else CHART_STYLE["bear"]),
            "CHoCH":    ("⟳", "#FF8844"),
            "ORDER":    ("■", "#9B59B6"),
            "FVG":      ("◆", "#4488FF"),
            "LIQUIDITY":("✦", "#F5A623"),
        }
        label_patches = []
        ultimo_x = len(xs) - 1
        offset_y = y_max * 0.998
        for s in senales:
            key  = next((k for k in smc_icons if k in s["tipo"].upper()), None)
            if not key: continue
            icon, col = smc_icons[key]
            ax_precio.text(ultimo_x, offset_y, f" {icon} {s['tipo'][:22]}",
                           color=col, fontsize=6.5, va="top", ha="right",
                           bbox=dict(fc=CHART_STYLE["bg"], ec=col, pad=1.5,
                                     boxstyle="round,pad=0.3"), zorder=7)
            label_patches.append(mpatches.Patch(color=col, label=s["tipo"][:30]))
            offset_y -= (y_max - y_min) * 0.065

        h_full = df["High"]; l_full = df["Low"]
        max_20 = float(h_full.iloc[-21:-1].max())
        min_20 = float(l_full.iloc[-21:-1].min())
        for val, lbl, col in [(max_20, "Max 20d", "#F5A62366"),
                               (min_20, "Min 20d", "#4488FF66")]:
            if y_min <= val <= y_max:
                ax_precio.axhline(val, color=col, linewidth=1, linestyle="-.", alpha=0.6)

        ax_precio.legend(loc="upper left", fontsize=7,
                         facecolor=CHART_STYLE["bg"],
                         labelcolor=CHART_STYLE["text"], framealpha=0.7)

        dir_col = CHART_STYLE["bull"] if dir_ == "COMPRA" else CHART_STYLE["bear"]
        fig.suptitle(
            f"{nombre}  —  SMC {'▲ COMPRA' if dir_ == 'COMPRA' else '▼ VENTA'}",
            color=dir_col, fontsize=12, fontweight="bold"
        )
        _tag_hora(ax_precio)

        _setup_ax(ax_rsi, "RSI(14)")
        rsi_full = _calc_rsi_serie(c_full, 14)
        rsi_sub  = rsi_full.iloc[-n_velas:].values
        ax_rsi.plot(xs, rsi_sub, color=CHART_STYLE["rsi_line"], linewidth=1.2)
        ax_rsi.axhline(70, color=CHART_STYLE["rsi_ob"],  linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.axhline(30, color=CHART_STYLE["rsi_os"],  linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI", fontsize=7)
        _etiquetar_eje_x(ax_rsi, sub)

        return _fig_a_bytes(fig)
    except Exception as e:
        print(f"    ⚠️ Error gráfico SMC: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# HELPERS GRÁFICO
# ──────────────────────────────────────────────────────────────
def _calc_rsi_serie(s, n=14):
    d = s.diff(); g = d.where(d > 0, 0.0); p = -d.where(d < 0, 0.0)
    return 100 - (100 / (1 + (g.rolling(n).mean() / p.rolling(n).mean())))

def _tag_hora(ax):
    ax.text(0.99, 0.99, datetime.now().strftime("%d/%m/%Y %H:%M"),
            transform=ax.transAxes, color=CHART_STYLE["text"],
            fontsize=7, ha="right", va="top", alpha=0.6)

def _etiquetar_eje_x(ax, sub):
    n = len(sub)
    ticks = list(range(0, n, max(1, n // 8)))
    labels = []
    for i in ticks:
        try:
            d = sub["Date"].iloc[i] if "Date" in sub.columns else sub.index[i]
            labels.append(pd.Timestamp(d).strftime("%d/%m"))
        except:
            labels.append("")
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=30, fontsize=7,
                       color=CHART_STYLE["text"])

def _fig_a_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=CHART_STYLE["bg"])
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generar_grafico(df, resultado):
    cat = resultado.get("cat")
    try:
        if cat == "FOREX":
            return generar_grafico_smc(df, resultado)
        elif cat == "ORO":
            return generar_grafico_ma_cross(df, resultado)
        else:
            return generar_grafico_general(df, resultado)
    except Exception as e:
        print(f"    ⚠️ generar_grafico: {e}")
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
        "max_precio": resultado["niveles"]["entrada"],
        "min_precio": resultado["niveles"]["entrada"],
    }

def cerrar_trade(sym):
    if sym in _trades_activos:
        del _trades_activos[sym]

def verificar_trades_activos(resultados_nuevos):
    eventos = []
    precios = {r["sym"]: r["precio"] for r in resultados_nuevos}
    for sym in list(_trades_activos.keys()):
        trade = _trades_activos[sym]
        precio_actual = precios.get(sym)
        if precio_actual is None:
            continue
        dir_trade = trade["direccion"]
        entrada   = trade["entrada"]
        sl        = trade["sl"]
        tp        = trade["tp"]
        if dir_trade == "COMPRA" and precio_actual <= sl:
            eventos.append({"tipo": "SL_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio_actual,
                             "resultado_pct": -trade["sl_pct"],
                             "ganancia_usd": precio_actual - entrada,
                             "mensaje": f"🛑 STOP LOSS TOCADO — Perdida de {trade['sl_pct']}%"})
            cerrar_trade(sym)
        elif dir_trade == "VENTA" and precio_actual >= sl:
            eventos.append({"tipo": "SL_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio_actual,
                             "resultado_pct": -trade["sl_pct"],
                             "ganancia_usd": entrada - precio_actual,
                             "mensaje": f"🛑 STOP LOSS TOCADO — Perdida de {trade['sl_pct']}%"})
            cerrar_trade(sym)
        elif dir_trade == "COMPRA" and precio_actual >= tp:
            eventos.append({"tipo": "TP_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio_actual,
                             "resultado_pct": trade["tp_pct"],
                             "ganancia_usd": precio_actual - entrada,
                             "mensaje": f"✅ TAKE PROFIT ALCANZADO — Ganancia de {trade['tp_pct']}%"})
            cerrar_trade(sym)
        elif dir_trade == "VENTA" and precio_actual <= tp:
            eventos.append({"tipo": "TP_TOCADO", "sym": sym, "trade": trade,
                             "precio_cierre": precio_actual,
                             "resultado_pct": trade["tp_pct"],
                             "ganancia_usd": entrada - precio_actual,
                             "mensaje": f"✅ TAKE PROFIT ALCANZADO — Ganancia de {trade['tp_pct']}%"})
            cerrar_trade(sym)
    return eventos

def filtrar_señales_nuevas(resultados):
    nuevos = []
    for r in resultados:
        sym = r["sym"]
        if sym not in _trades_activos:
            nuevos.append(r)
            abrir_trade(sym, r)
        else:
            trade_actual = _trades_activos[sym]
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
TZ_ET  = timezone(timedelta(hours=-4))
TZ_HKT = timezone(timedelta(hours=+8))

def en_sesion_ny():
    ahora_et = datetime.now(TZ_ET)
    if ahora_et.weekday() >= 5:
        return False
    hora = ahora_et.hour + ahora_et.minute / 60
    return 9.5 <= hora <= 16.0

def en_sesion_oro():
    """
    Sesiones del Oro en hora de El Salvador (CST = UTC-6):
    - Mañana NY:  06:00 – 10:00 CST → TF 15min, revisa c/15min
    - Asia/tarde: 18:00 – 20:30 CST → TF 30min, revisa c/30min
    Retorna: (activo: bool, tf: str, cada_min: int, nombre_sesion: str)
    """
    TZ_CST = timezone(timedelta(hours=-6))
    ahora  = datetime.now(TZ_CST)
    if ahora.weekday() >= 5:
        return False, "15m", 15, "Fin de semana"
    hora = ahora.hour + ahora.minute / 60
    if 6.0 <= hora < 10.0:
        return True, "15m", 15, "🗽 NY Mañana (06:00–10:00 CST)"
    if 18.0 <= hora < 20.5:
        return True, "30m", 30, "🌏 Asia (18:00–20:30 CST)"
    return False, "15m", 15, "Fuera de sesión Oro"

def sesion_activa(cat):
    if cat in ("ACCION", "ETF"):
        return en_sesion_ny()
    if cat == "ORO":
        activo, tf, cada, nombre = en_sesion_oro()
        return activo
    return True

def config_oro_dinamica():
    return en_sesion_oro()

SL_TP = {
    "ACCION": {"sl": 2.0,  "tp": 4.0},
    "ETF":    {"sl": 1.5,  "tp": 3.0},
    "FOREX":  {"sl": 0.5,  "tp": 1.0},
    "ORO":    {"sl": 1.0,  "tp": 2.0},
    "CRYPTO": {"sl": 3.0,  "tp": 6.0},
    "INDICE": {"sl": 1.0,  "tp": 2.0},
}

ACCIONES = ["AAPL","NVDA","TSLA","MSFT","AMZN","GOOGL","META"]
ETFS     = ["VTI","VOO","BND","SDY"]
FOREX    = ["EURUSD=X","USDJPY=X","USDCAD=X","AUDUSD=X"]
ORO      = ["GC=F"]
CRYPTO   = ["BTC-USD"]
INDICES  = ["NQ=F", "ES=F"]
TODOS    = ACCIONES + ETFS + FOREX + ORO + CRYPTO + INDICES

NOMBRES = {
    "GC=F":    "XAUUSD — Oro",
    "BTC-USD": "BTC/USD — Bitcoin",
    "NQ=F":    "NAS100 — Nasdaq 100 Futures",
    "ES=F":    "SP500 — S&P 500 Futures",
    "EURUSD=X":"EUR/USD","USDJPY=X":"USD/JPY",
    "USDCAD=X":"USD/CAD","AUDUSD=X":"AUD/USD",
    "VTI":"VTI — ETF Mercado Total","VOO":"VOO — ETF S&P500",
    "BND":"BND — ETF Bonos","SDY":"SDY — ETF Dividendos",
}

def categoria(s):
    if s in FOREX:   return "FOREX"
    if s in ORO:     return "ORO"
    if s in CRYPTO:  return "CRYPTO"
    if s in INDICES: return "INDICE"
    if s in ETFS:    return "ETF"
    return "ACCION"

ICONOS = {"ACCION":"📈","ETF":"🗂️","FOREX":"💱","ORO":"🥇","CRYPTO":"₿","INDICE":"📊"}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]
SES = requests.Session()

def get_crumb():
    try:
        h = {"User-Agent": random.choice(UA), "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.5"}
        SES.get("https://finance.yahoo.com", headers=h, timeout=15)
        r = SES.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=h, timeout=10)
        c = r.text.strip()
        return c if c and len(c) > 3 else None
    except:
        return None

def get_datos(sym, cr=None, tf="1d", dias=120):
    now = int(time.time()); ini = now - (dias * 24 * 3600)
    for i in range(3):
        try:
            h = {"User-Agent": random.choice(UA), "Accept": "application/json,*/*",
                 "Accept-Language": "en-US,en;q=0.9",
                 "Referer": f"https://finance.yahoo.com/quote/{sym}"}
            srv = ["query1", "query2"][i % 2]
            url = (f"https://{srv}.finance.yahoo.com/v8/finance/chart/{sym}"
                   f"?period1={ini}&period2={now}&interval={tf}&includeAdjustedClose=true")
            if cr: url += f"&crumb={cr}"
            resp = SES.get(url, headers=h, timeout=25)
            if not resp.text.strip() or resp.status_code != 200:
                time.sleep(3); continue
            d = resp.json(); res = d.get("chart", {}).get("result", [])
            if not res: time.sleep(3); continue
            r = res[0]; ts = r.get("timestamp", [])
            cl = r.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", [])
            hi = r.get("indicators", {}).get("quote", [{}])[0].get("high", [])
            lo = r.get("indicators", {}).get("quote", [{}])[0].get("low", [])
            vo = r.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
            op = r.get("indicators", {}).get("quote", [{}])[0].get("open", [])
            if not ts or not cl: continue
            n = min(len(ts), len(cl),
                    len(hi) if hi else len(cl),
                    len(lo) if lo else len(cl))
            df = pd.DataFrame({
                "timestamp": ts[:n], "Close": cl[:n],
                "High": hi[:n] if hi else cl[:n],
                "Low":  lo[:n] if lo else cl[:n],
                "Open": op[:n] if op else cl[:n],
                "Volume": vo[:n] if vo else [None] * n
            }).dropna(subset=["Close"])
            df["Date"] = pd.to_datetime(df["timestamp"], unit="s")
            return df.set_index("Date").sort_index()
        except:
            if i < 2: time.sleep(4 + i * 2)
    return None

def calc_rsi(s, n=14):
    d = s.diff(); g = d.where(d > 0, 0.0); p = -d.where(d < 0, 0.0)
    return 100 - (100 / (1 + (g.rolling(n).mean() / p.rolling(n).mean())))

def calc_atr(df, n=14):
    h = df["High"]; l = df["Low"]; c = df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def calc_niveles(precio, direccion, cat, atr_val=None):
    sl_pct = SL_TP[cat]["sl"] / 100
    tp_pct = SL_TP[cat]["tp"] / 100
    entrada = precio
    if direccion == "COMPRA":
        sl = round(entrada * (1 - sl_pct), 5)
        tp = round(entrada * (1 + tp_pct), 5)
    else:
        sl = round(entrada * (1 + sl_pct), 5)
        tp = round(entrada * (1 - tp_pct), 5)
    rr = tp_pct / sl_pct
    return {"entrada": entrada, "sl": sl, "tp": tp, "rr": rr,
            "sl_pct": SL_TP[cat]["sl"], "tp_pct": SL_TP[cat]["tp"]}


# ─────────────────────────────────────────────────────────────
# ESTRATEGIA FOREX — SMC
# ─────────────────────────────────────────────────────────────
def analizar_smc(df, sym):
    if len(df) < 30:
        return None
    c = df["Close"]; h = df["High"]; l = df["Low"]; o = df["Open"]
    precio = float(c.iloc[-1]); senales = []; direccion = None
    ventana = 20
    prev_high_5 = float(h.iloc[-6:-1].max())
    prev_low_5  = float(l.iloc[-6:-1].min())
    closes = c.iloc[-ventana:].values
    tend   = "ALCISTA" if closes[-1] > closes[-10] else "BAJISTA"

    if precio > prev_high_5 and float(c.iloc[-2]) <= prev_high_5:
        senales.append({"tipo": "🔺 BOS ALCISTA (SMC)", "a": "✅ SEÑAL DE COMPRA",
                         "desc": f"Rompio estructura: precio supero maximo previo ${prev_high_5:,.5f}",
                         "f": "FUERTE"})
        direccion = "COMPRA"
    elif precio < prev_low_5 and float(c.iloc[-2]) >= prev_low_5:
        senales.append({"tipo": "🔻 BOS BAJISTA (SMC)", "a": "❌ SEÑAL DE VENTA",
                         "desc": f"Rompio estructura: precio bajo minimo previo ${prev_low_5:,.5f}",
                         "f": "FUERTE"})
        direccion = "VENTA"

    if tend == "BAJISTA" and precio > prev_high_5:
        senales.append({"tipo": "🔄 CHoCH — CAMBIO ESTRUCTURA (SMC)", "a": "✅ POSIBLE REVERSIÓN ALCISTA",
                         "desc": f"Tendencia bajista cambia a alcista. Rompio ${prev_high_5:,.5f}",
                         "f": "MUY FUERTE"})
        direccion = "COMPRA"
    elif tend == "ALCISTA" and precio < prev_low_5:
        senales.append({"tipo": "🔄 CHoCH — CAMBIO ESTRUCTURA (SMC)", "a": "❌ POSIBLE REVERSIÓN BAJISTA",
                         "desc": f"Tendencia alcista cambia a bajista. Rompio ${prev_low_5:,.5f}",
                         "f": "MUY FUERTE"})
        direccion = "VENTA"

    for i in range(-5, -1):
        vela_o = float(o.iloc[i]); vela_c = float(c.iloc[i])
        vela_h = float(h.iloc[i]); vela_l = float(l.iloc[i])
        sig_vela = float(c.iloc[i + 1]) - float(o.iloc[i + 1])
        if vela_c < vela_o and sig_vela > 0:
            mov = abs(sig_vela / vela_o) * 100
            if mov > 0.3 and vela_l <= precio <= vela_h:
                senales.append({"tipo": "📦 ORDER BLOCK ALCISTA (SMC)", "a": "✅ ZONA DE COMPRA",
                                  "desc": f"Precio en OB alcista (${vela_l:,.5f} - ${vela_h:,.5f}). Zona institucional.",
                                  "f": "FUERTE"})
                if not direccion: direccion = "COMPRA"
                break
        if vela_c > vela_o and sig_vela < 0:
            mov = abs(sig_vela / vela_o) * 100
            if mov > 0.3 and vela_l <= precio <= vela_h:
                senales.append({"tipo": "📦 ORDER BLOCK BAJISTA (SMC)", "a": "❌ ZONA DE VENTA",
                                  "desc": f"Precio en OB bajista (${vela_l:,.5f} - ${vela_h:,.5f}). Zona institucional.",
                                  "f": "FUERTE"})
                if not direccion: direccion = "VENTA"
                break

    for i in range(-6, -2):
        h1 = float(h.iloc[i]); l1 = float(l.iloc[i])
        h3 = float(h.iloc[i + 2]); l3 = float(l.iloc[i + 2])
        if l3 > h1:
            zona_mid = (l3 + h1) / 2
            if abs(precio - zona_mid) / zona_mid < 0.005:
                senales.append({"tipo": "⚡ FVG ALCISTA (SMC)", "a": "✅ ZONA DE COMPRA",
                                  "desc": f"Fair Value Gap alcista: hueco ${h1:,.5f} - ${l3:,.5f}.",
                                  "f": "MEDIA"})
                if not direccion: direccion = "COMPRA"
                break
        if h3 < l1:
            zona_mid = (h3 + l1) / 2
            if abs(precio - zona_mid) / zona_mid < 0.005:
                senales.append({"tipo": "⚡ FVG BAJISTA (SMC)", "a": "❌ ZONA DE VENTA",
                                  "desc": f"Fair Value Gap bajista: hueco ${h3:,.5f} - ${l1:,.5f}.",
                                  "f": "MEDIA"})
                if not direccion: direccion = "VENTA"
                break

    max_20 = float(h.iloc[-21:-1].max()); min_20 = float(l.iloc[-21:-1].min())
    if float(l.iloc[-1]) < min_20 and precio > min_20:
        senales.append({"tipo": "🎯 LIQUIDITY SWEEP ALCISTA (SMC)", "a": "✅ SEÑAL DE COMPRA",
                         "desc": f"Barro minimos de 20 dias (${min_20:,.5f}) y revirtio. Trampa bajista.",
                         "f": "MUY FUERTE"})
        if not direccion: direccion = "COMPRA"
    elif float(h.iloc[-1]) > max_20 and precio < max_20:
        senales.append({"tipo": "🎯 LIQUIDITY SWEEP BAJISTA (SMC)", "a": "❌ SEÑAL DE VENTA",
                         "desc": f"Barro maximos de 20 dias (${max_20:,.5f}) y revirtio. Trampa alcista.",
                         "f": "MUY FUERTE"})
        if not direccion: direccion = "VENTA"

    # ── FIX v9.1: Modo "mejor oportunidad" para Forex ─────────────
    # Si no hay señal SMC estricta, usar tendencia EMA como fallback
    if not senales or not direccion:
        ema20_h = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50_h = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
        rsi_val = float(calc_rsi(c).iloc[-1])
        if ema20_h > ema50_h:
            dir_forzada = "COMPRA"
            motivo = f"EMA20 ({ema20_h:,.5f}) sobre EMA50 ({ema50_h:,.5f}) — tendencia alcista"
        else:
            dir_forzada = "VENTA"
            motivo = f"EMA20 ({ema20_h:,.5f}) bajo EMA50 ({ema50_h:,.5f}) — tendencia bajista"
        rsi_desc = ("RSI neutro" if 45 <= rsi_val <= 55
                    else f"RSI {'sobrecomprado' if rsi_val > 55 else 'sobrevendido'} ({rsi_val:.0f})")
        senales.append({
            "tipo": "📡 MEJOR OPORTUNIDAD DISPONIBLE (SMC)",
            "a":    "✅ COMPRA SUGERIDA" if dir_forzada == "COMPRA" else "❌ VENTA SUGERIDA",
            "desc": f"{motivo} | {rsi_desc} | Sin señal SMC estricta en este momento.",
            "f":    "BAJA"
        })
        direccion = dir_forzada

    niveles = calc_niveles(precio, direccion, "FOREX")
    return {"senales": senales, "direccion": direccion, "niveles": niveles}


# ─────────────────────────────────────────────────────────────
# ESTRATEGIA ORO — MA Cross SMA50/SMA100 con ventana de 3 velas
# ─────────────────────────────────────────────────────────────
def analizar_ma_cross(df, sym):
    """
    FIX v9.1: Detecta cruces SMA50/SMA100 en las últimas 3 velas.
    Esto soluciona el problema de que el bot revise DESPUÉS del cruce exacto
    y no detecte la señal. La ventana de 3 velas = 45 min en TF 15min.

    Siempre usa SMA50/SMA100 igual que el Pine Script de TradingView,
    independientemente de cuántas velas haya en el DataFrame.
    Necesita mínimo 105 velas (100 para SMA100 + 5 de margen).
    """
    c = df["Close"]
    precio = float(c.iloc[-1])
    n = len(c)

    # Necesitamos al menos 105 velas para SMA50/SMA100 confiables
    if n < 105:
        print(f"    ⚠️ ORO: solo {n} velas, necesita 105 para SMA50/SMA100")
        return None

    # ── Siempre SMA50 / SMA100 (igual que Pine Script) ──
    ma_r  = c.rolling(50).mean()
    ma_l  = c.rolling(100).mean()
    r_nom = "SMA50"
    l_nom = "SMA100"

    rsi_val = float(calc_rsi(c).iloc[-1])
    senales = []
    direccion = None

    # ── FIX: Buscar cruce en las últimas 3 velas ──────────────
    # Índices: -4 → -3 → -2 → -1 (vela actual)
    # Revisamos los 3 pares de velas consecutivas más recientes
    cruce_vela_idx   = None   # posición en el sub-array para el gráfico
    cruce_precio     = None
    velas_desde_cruce = 0

    for offset in range(1, 4):   # offset=1 → última vela, offset=3 → hace 3 velas
        idx_ant = -(offset + 1)
        idx_act = -offset
        mr_ant = float(ma_r.iloc[idx_ant])
        ml_ant = float(ma_l.iloc[idx_ant])
        mr_act = float(ma_r.iloc[idx_act])
        ml_act = float(ma_l.iloc[idx_act])

        # Cruce alcista: SMA50 pasa de ABAJO a ARRIBA de SMA100
        if mr_ant < ml_ant and mr_act >= ml_act:
            velas_desde_cruce = offset - 1
            cruce_precio = float(c.iloc[idx_act])
            senales.append({
                "tipo": f"⭐ MA CROSS ALCISTA — {r_nom} cruzó ARRIBA {l_nom}",
                "a":    "✅ SEÑAL DE COMPRA",
                "desc": (f"{r_nom}=${mr_act:,.2f} cruzó ARRIBA {l_nom}=${ml_act:,.2f}. "
                         f"Zona verde activada."
                         + (f" (hace {velas_desde_cruce} vela{'s' if velas_desde_cruce != 1 else ''})"
                            if velas_desde_cruce > 0 else "")),
                "f":    "MUY FUERTE"
            })
            direccion = "COMPRA"
            break

        # Cruce bajista: SMA50 pasa de ARRIBA a ABAJO de SMA100
        elif mr_ant > ml_ant and mr_act <= ml_act:
            velas_desde_cruce = offset - 1
            cruce_precio = float(c.iloc[idx_act])
            senales.append({
                "tipo": f"💀 MA CROSS BAJISTA — {r_nom} cruzó ABAJO {l_nom}",
                "a":    "❌ SEÑAL DE VENTA",
                "desc": (f"{r_nom}=${mr_act:,.2f} cruzó ABAJO {l_nom}=${ml_act:,.2f}. "
                         f"Zona roja activada."
                         + (f" (hace {velas_desde_cruce} vela{'s' if velas_desde_cruce != 1 else ''})"
                            if velas_desde_cruce > 0 else "")),
                "f":    "MUY FUERTE"
            })
            direccion = "VENTA"
            break

    # ── Sin cruce reciente — verificar cruce inminente ────────
    if not senales:
        mr_hoy = float(ma_r.iloc[-1])
        ml_hoy = float(ma_l.iloc[-1])
        dist = abs(mr_hoy - ml_hoy) / ml_hoy * 100
        estado = (f"{r_nom} > {l_nom} — tendencia alcista"
                  if mr_hoy > ml_hoy
                  else f"{r_nom} < {l_nom} — tendencia bajista")
        if dist < 0.3:
            senales.append({
                "tipo": f"⚠️ CRUCE INMINENTE {r_nom}/{l_nom}",
                "a":    "👀 PREPARAR ENTRADA",
                "desc": f"Medias separadas solo {dist:.3f}%. {estado}. Cruce puede ocurrir en la próxima vela.",
                "f":    "MEDIA"
            })
            direccion = "COMPRA" if mr_hoy > ml_hoy else "VENTA"

    if not senales or not direccion:
        return None

    # ── RSI como filtro ──
    if direccion == "COMPRA" and rsi_val > 70:
        senales.append({
            "tipo": "⚠️ FILTRO RSI",
            "a":    "👀 PRECAUCION",
            "desc": f"MA Cross dice COMPRA pero RSI={rsi_val:.0f} sobrecomprado. Considera esperar retroceso.",
            "f":    "MEDIA"
        })
    elif direccion == "VENTA" and rsi_val < 30:
        senales.append({
            "tipo": "⚠️ FILTRO RSI",
            "a":    "👀 PRECAUCION",
            "desc": f"MA Cross dice VENTA pero RSI={rsi_val:.0f} sobrevendido. Considera esperar rebote.",
            "f":    "MEDIA"
        })

    # Calcular posición del cruce en el sub-array del gráfico (últimas 120 velas)
    n_velas_grafico = 120
    if cruce_precio is not None:
        cruce_vela_idx = n_velas_grafico - 1 - (velas_desde_cruce)

    mr_hoy = float(ma_r.iloc[-1])
    ml_hoy = float(ma_l.iloc[-1])

    niveles = calc_niveles(precio, direccion, "ORO")
    return {
        "senales":          senales,
        "direccion":        direccion,
        "niveles":          niveles,
        "sma50":            mr_hoy,
        "sma100":           ml_hoy,
        "r_nom":            r_nom,
        "l_nom":            l_nom,
        "rsi":              rsi_val,
        "cruce_vela_idx":   cruce_vela_idx,
        "cruce_precio":     cruce_precio,
        "velas_desde_cruce": velas_desde_cruce,
    }


# ─────────────────────────────────────────────────────────────
# ESTRATEGIA GENERAL — Acciones, ETF, BTC, NAS100, SP500
# ─────────────────────────────────────────────────────────────
def analizar_general(df, sym, cat):
    PARAMS = {
        "ACCION": {"rsi_b": 30, "rsi_a": 70, "cambio": 2.0, "ema_c": 20, "ema_l": 50},
        "ETF":    {"rsi_b": 35, "rsi_a": 65, "cambio": 1.5, "ema_c": 20, "ema_l": 50},
        "CRYPTO": {"rsi_b": 35, "rsi_a": 65, "cambio": 1.5, "ema_c": 9,  "ema_l": 21},
        "INDICE": {"rsi_b": 35, "rsi_a": 65, "cambio": 0.5, "ema_c": 9,  "ema_l": 21},
    }
    p = PARAMS.get(cat, PARAMS["ACCION"])
    c = df["Close"]; precio = float(c.iloc[-1])
    precio_ayer = float(c.iloc[-2])
    precio_7d   = float(c.iloc[-6]) if len(c) >= 6 else precio_ayer
    cd = ((precio - precio_ayer) / precio_ayer) * 100
    c7 = ((precio - precio_7d)   / precio_7d)   * 100
    ema_c = c.ewm(span=p["ema_c"], adjust=False).mean()
    ema_l = c.ewm(span=p["ema_l"], adjust=False).mean()
    rsi_v = float(calc_rsi(c).iloc[-1])
    senales = []; direccion = None

    if cd >= p["cambio"]:
        senales.append({"tipo": "📈 SUBIDA FUERTE", "a": "⚡ POSIBLE COMPRA",
                         "desc": f"Subio {cd:.2f}% en vela anterior (umbral {p['cambio']}%)", "f": "MEDIA"})
        direccion = "COMPRA"
    elif cd <= -p["cambio"]:
        senales.append({"tipo": "📉 CAIDA FUERTE", "a": "⚠️ POSIBLE VENTA",
                         "desc": f"Cayo {abs(cd):.2f}% en vela anterior (umbral {p['cambio']}%)", "f": "MEDIA"})
        direccion = "VENTA"

    if rsi_v <= p["rsi_b"]:
        senales.append({"tipo": "🔵 RSI SOBREVENDIDO", "a": "✅ SEÑAL DE COMPRA",
                         "desc": f"RSI={rsi_v:.1f} bajo {p['rsi_b']} — zona de compra técnica", "f": "FUERTE"})
        direccion = "COMPRA"
    elif rsi_v >= p["rsi_a"]:
        senales.append({"tipo": "🔴 RSI SOBRECOMPRADO", "a": "❌ SEÑAL DE VENTA",
                         "desc": f"RSI={rsi_v:.1f} sobre {p['rsi_a']} — zona de venta técnica", "f": "FUERTE"})
        direccion = "VENTA"

    ec_h = float(ema_c.iloc[-1]); ec_a = float(ema_c.iloc[-2])
    el_h = float(ema_l.iloc[-1]); el_a = float(ema_l.iloc[-2])
    if ec_a < el_a and ec_h > el_h:
        senales.append({"tipo": "⭐ GOLDEN CROSS", "a": "✅ COMPRA FUERTE",
                         "desc": f"EMA{p['ema_c']} cruzo ARRIBA a EMA{p['ema_l']}", "f": "MUY FUERTE"})
        direccion = "COMPRA"
    elif ec_a > el_a and ec_h < el_h:
        senales.append({"tipo": "💀 DEATH CROSS", "a": "❌ VENTA FUERTE",
                         "desc": f"EMA{p['ema_c']} cruzo ABAJO a EMA{p['ema_l']}", "f": "MUY FUERTE"})
        direccion = "VENTA"

    if cat != "FOREX" and "Volume" in df.columns:
        v = df["Volume"].dropna()
        if len(v) > 20:
            va = float(v.iloc[-1]); vp = float(v.rolling(20).mean().iloc[-1])
            if vp > 0 and va / vp >= 2.0:
                senales.append({"tipo": "🔊 VOLUMEN INUSUAL", "a": "👀 PRESTAR ATENCION",
                                  "desc": f"Volumen {va / vp:.1f}x el promedio de 20 velas", "f": "MEDIA"})

    if cat in ("CRYPTO", "INDICE"):
        ventana_sr = 50
        if len(c) >= ventana_sr:
            mn = float(c.rolling(ventana_sr).min().iloc[-1])
            mx = float(c.rolling(ventana_sr).max().iloc[-1])
            dm = ((precio - mn) / mn) * 100
            dM = ((mx - precio) / mx) * 100
            if dm <= 1.5:
                senales.append({"tipo": "🛡️ CERCA SOPORTE 50v", "a": "✅ ZONA DE COMPRA",
                                  "desc": f"A solo {dm:.2f}% del soporte de 50 velas (${mn:,.2f}). Rebote probable.",
                                  "f": "FUERTE"})
                direccion = "COMPRA"
            elif dM <= 1.5:
                senales.append({"tipo": "🚧 CERCA RESISTENCIA 50v", "a": "❌ ZONA DE VENTA",
                                  "desc": f"A solo {dM:.2f}% de la resistencia de 50 velas (${mx:,.2f}). Rechazo probable.",
                                  "f": "FUERTE"})
                direccion = "VENTA"

    if not senales and cat in ("CRYPTO", "INDICE"):
        if ec_h > el_h:
            dir_forzada = "COMPRA"
            motivo = f"EMA{p['ema_c']} ({ec_h:,.2f}) sobre EMA{p['ema_l']} ({el_h:,.2f}) — tendencia alcista"
        else:
            dir_forzada = "VENTA"
            motivo = f"EMA{p['ema_c']} ({ec_h:,.2f}) bajo EMA{p['ema_l']} ({el_h:,.2f}) — tendencia bajista"
        rsi_desc = ("RSI neutro" if 45 <= rsi_v <= 55
                    else f"RSI {'sobrecomprado' if rsi_v > 55 else 'sobrevendido'} ({rsi_v:.0f})")
        senales.append({
            "tipo": "📡 MEJOR OPORTUNIDAD DISPONIBLE",
            "a":    "✅ COMPRA SUGERIDA" if dir_forzada == "COMPRA" else "❌ VENTA SUGERIDA",
            "desc": f"{motivo} | {rsi_desc} | Cambio vela: {cd:+.2f}%",
            "f":    "MEDIA"
        })
        direccion = dir_forzada

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
_df_cache = {}

def analizar(sym, cr=None):
    try:
        cat = categoria(sym)
        if cat == "ORO":
            activo, oro_tf, oro_cada, oro_sesion = en_sesion_oro()
            # Para tener 105+ velas: 15m → 8 días (~768 velas), 30m → 16 días (~768 velas)
            oro_dias = 8 if oro_tf == "15m" else 16
            df = get_datos(sym, cr, tf=oro_tf, dias=oro_dias)
            print(f"    [{oro_sesion}|{oro_tf}]", end=" ")
        else:
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
CAT_LABEL = {
    "ACCION": "ACCIONES",
    "ETF":    "ETFs",
    "ORO":    "ORO — Estrategia MA Cross SMA50/SMA100",
    "CRYPTO": "CRYPTO — BTC/USD (15min)",
    "INDICE": "ÍNDICES — NAS100 & SP500 Futures (15min)",
    "FOREX":  "FOREX — Estrategia SMC (Smart Money Concepts)",
}
CAT_DESC = {
    "ACCION": "RSI + EMA + Volumen. Opera solo 9:30-16:00 ET.",
    "ETF":    "RSI + EMA. Menos volatiles, ideales largo plazo.",
    "ORO":    "MA Cross SMA50/SMA100. Sesiones: 🗽 06:00–10:00 CST (15min) | 🌏 18:00–20:30 CST (30min). SL 1% / TP 2%.",
    "CRYPTO": "RSI + EMA9/21 + Soporte/Resistencia. TF 15min. SL 3% / TP 6%. 24h.",
    "INDICE": "EMA9/21 + RSI + S/R. TF 15min. NQ=NAS100, ES=SP500. SL 1% / TP 2%.",
    "FOREX":  "BOS, CHoCH, Order Block, FVG, Liquidity Sweep. SL 0.5% / TP 1%. Opera 24h.",
}

def construir_html(resultados, img_cids):
    grupos = {}
    for r in resultados:
        grupos.setdefault(r["cat"], []).append(r)
    body = ""

    for cat in ["ACCION", "ETF", "ORO", "CRYPTO", "INDICE", "FOREX"]:
        if cat not in grupos: continue
        body += (f'<div style="background:#0F3460;border-radius:10px;padding:10px 16px;margin:20px 0 8px;">'
                 f'<span style="font-size:16px;">{ICONOS[cat]}</span> '
                 f'<b style="color:#F5A623;font-size:14px;">{CAT_LABEL[cat]}</b>'
                 f'<div style="color:#aaa;font-size:11px;margin-top:2px;">{CAT_DESC[cat]}</div></div>')

        for r in grupos[cat]:
            col     = "#00B074" if r["cd"] >= 0 else "#E94560"
            sig     = "▲" if r["cd"] >= 0 else "▼"
            dir_col = "#00B074" if r["direccion"] == "COMPRA" else "#E94560"
            dir_emoji = "✅" if r["direccion"] == "COMPRA" else "❌"
            niv     = r["niveles"]

            sym  = r["sym"]
            cid  = img_cids.get(sym)
            img_blk = ""
            if cid:
                img_blk = (
                    f'<div style="margin:10px 0;border-radius:10px;overflow:hidden;">'
                    f'<img src="cid:{cid}" alt="Grafico {r["nombre"]}" '
                    f'style="width:100%;max-width:600px;display:block;border-radius:8px;" />'
                    f'</div>'
                )

            bq = ""
            for s in r["senales"]:
                bg = "#1a3a2a" if "COMPRA" in s["a"] else "#3a1a1a" if "VENTA" in s["a"] else "#1a2a3a"
                bq += (f'<div style="background:{bg};border-radius:8px;padding:7px 10px;margin:4px 0;">'
                       f'<b style="font-size:12px;">{s["tipo"]}</b> — '
                       f'<span style="color:#F5A623;font-size:12px;">{s["a"]}</span><br>'
                       f'<span style="color:#ccc;font-size:11px;">{s["desc"]}</span> '
                       f'<span style="background:#0F3460;padding:1px 5px;border-radius:6px;font-size:10px;">{s["f"]}</span>'
                       f'</div>')

            op_html = (
                f'<div style="background:#0a1a0a;border:2px solid {dir_col};border-radius:12px;padding:16px;margin:10px 0;">'
                f'<div style="text-align:center;margin-bottom:12px;">'
                f'<span style="font-size:22px;">{dir_emoji}</span> '
                f'<b style="font-size:18px;color:{dir_col};">{r["direccion"]}</b>'
                f'</div>'
                f'<table width="100%" style="border-collapse:collapse;">'
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">🎯 PRECIO DE ENTRADA</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#fff;">${niv["entrada"]:,.5f}</td>'
                f'</tr>'
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">🛑 STOP LOSS ({niv["sl_pct"]}%)</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#E94560;">${niv["sl"]:,.5f}</td>'
                f'</tr>'
                f'<tr style="border-bottom:1px solid #1a2a1a;">'
                f'<td style="padding:8px;color:#aaa;font-size:12px;">✅ TAKE PROFIT ({niv["tp_pct"]}%)</td>'
                f'<td style="padding:8px;text-align:right;font-size:15px;font-weight:bold;color:#00B074;">${niv["tp"]:,.5f}</td>'
                f'</tr>'
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

            info_extra = ""
            ex = r.get("extra", {})
            if cat == "ORO" and "sma50" in ex:
                r_nom = ex.get("r_nom", "SMA50")
                l_nom = ex.get("l_nom", "SMA100")
                _, oro_tf, _, oro_ses = en_sesion_oro()
                velas_txt = ""
                if ex.get("velas_desde_cruce", 0) > 0:
                    velas_txt = f' | Cruce hace <b style="color:#E94560;">{ex["velas_desde_cruce"]} vela(s)</b>'
                info_extra = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                    f'🕐 Sesión: <b style="color:#F5A623;">{oro_ses}</b> | TF: <b>{oro_tf}</b> | '
                    f'{r_nom}: <b style="color:#4488ff;">${ex["sma50"]:,.2f}</b> | '
                    f'{l_nom}: <b style="color:#44bb44;">${ex["sma100"]:,.2f}</b> | '
                    f'RSI: <b style="color:#F5A623;">{ex["rsi"]:.1f}</b>{velas_txt}'
                    f'</div>'
                )
            elif cat == "FOREX":
                info_extra = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                    f'Estrategia: <b style="color:#F5A623;">Smart Money Concepts (SMC)</b> | '
                    f'Señales detectadas: <b>{len(r["senales"])}</b>'
                    f'</div>'
                )
            elif cat == "INDICE":
                rsi_val = ex.get("rsi", 0)
                ec_n = ex.get("ec_n", 9); el_n = ex.get("el_n", 21)
                info_extra = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px;margin:6px 0;font-size:11px;color:#aaa;">'
                    f'TF: <b>15min</b> | EMA{ec_n}: <b style="color:#4488ff;">${ex.get("ec",0):,.2f}</b> | '
                    f'EMA{el_n}: <b style="color:#FF8844;">${ex.get("el",0):,.2f}</b> | '
                    f'RSI: <b style="color:#F5A623;">{rsi_val:.1f}</b>'
                    f'</div>'
                )

            body += (
                f'<div style="background:#16213E;border-radius:12px;padding:16px;margin:8px 0;border-left:4px solid {dir_col};">'
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
        f'Bot Railway 24/7 v9.1 | Forex SMC: cada 60min en {FOREX_INTERVALO} | Oro: SMA50/100 ventana 3 velas</div>'
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
                print(f"  📈 Generando gráfico para {sym}...", end=" ")
                data = generar_grafico(df, r)
                if data:
                    cid_name = f"chart_{sym.replace('=', '_').replace('-', '_').replace('/', '_')}"
                    img_cids[sym]  = cid_name
                    img_bytes[sym] = data
                    print("✅")
                else:
                    print("⚠️ sin imagen")

        msg = MIMEMultipart("related")
        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)

        cats = set(r["cat"] for r in res)
        msg["Subject"] = f"🚨 {len(res)} Alerta(s) [{', '.join(cats)}] — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        msg["From"] = EMAIL_REMITENTE
        msg["To"]   = EMAIL_DESTINO

        txt = "\n".join([
            f"{r['nombre']}: ${r['precio']:,.5f} ({r['cd']:+.2f}%) → {r['direccion']} "
            f"| SL:${r['niveles']['sl']:,.5f} TP:${r['niveles']['tp']:,.5f}"
            for r in res
        ])
        msg_alt.attach(MIMEText(txt, "plain"))
        html = construir_html(res, img_cids)
        msg_alt.attach(MIMEText(html, "html"))

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
        asunto_icon = ("✅" if cierres_tp and not cierres_sl
                       else "🛑" if cierres_sl and not cierres_tp else "📊")
        msg["Subject"] = f"{asunto_icon} {len(eventos)} Trade(s) Cerrado(s) — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        msg["From"] = EMAIL_REMITENTE
        msg["To"]   = EMAIL_DESTINO

        filas = ""
        for ev in eventos:
            t = ev["trade"]; es_tp = ev["tipo"] == "TP_TOCADO"
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

        resumen_color = ("#00B074" if cierres_tp and not cierres_sl
                         else "#E94560" if cierres_sl and not cierres_tp else "#F5A623")
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

        txt = "\n".join([f"{ev['trade']['nombre']}: {ev['mensaje']} | Cierre: ${ev['precio_cierre']:,.5f}"
                         for ev in eventos])
        msg.attach(MIMEText(txt, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.sendmail(EMAIL_REMITENTE, EMAIL_DESTINO, msg.as_string())
        print(f"  ✅ Correo de cierre enviado — {len(eventos)} trade(s)")
    except Exception as e:
        print(f"  ❌ Error correo cierre: {e}")


# ─────────────────────────────────────────────────────────────
# CONTROL DE SESIONES Y LOOP
# ─────────────────────────────────────────────────────────────
def debe_revisar(cat):
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
    print(f"\n{'=' * 55}")
    print(f"  🔍 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'=' * 55}")

    cats_a_revisar    = [cat for cat in ["ACCION", "ETF", "ORO", "CRYPTO", "FOREX"] if debe_revisar(cat)]
    activos_a_revisar = [s for s in TODOS if categoria(s) in cats_a_revisar]

    if not activos_a_revisar:
        print("  ⏳ Ningún mercado activo ahora")
        for cat in ["ACCION", "ETF", "ORO", "CRYPTO", "FOREX"]:
            cada   = INTERVALOS_MERCADO.get(cat, {"cada_min": 30})["cada_min"]
            ult    = _ultimo_chequeo.get(cat, 0)
            faltan = max(0, int((ult + cada * 60 - time.time()) / 60))
            if cat == "ORO":
                activo, tf, cada_oro, nombre_ses = en_sesion_oro()
                estado = f"🟢 {nombre_ses}" if activo else "🔴 Fuera de sesión"
            else:
                tf     = INTERVALOS_MERCADO.get(cat, {}).get("tf", "1d")
                activo = sesion_activa(cat)
                estado = "🟢 Sesión activa" if activo else "🔴 Fuera de sesión"
            print(f"    {ICONOS.get(cat, '📊')} {cat:6} [{tf}] {estado} — próxima revisión en {faltan} min")
        return

    print(f"  📋 Revisando: {', '.join(cats_a_revisar)}")
    for cat in cats_a_revisar:
        tf = INTERVALOS_MERCADO.get(cat, {}).get("tf", "1d")
        print(f"    {ICONOS.get(cat, '📊')} {cat} — timeframe {tf}")

    cr = get_crumb()
    print(f"  {'✅ Conectado a Yahoo' if cr else '⚠️ Sin crumb...'}")

    todos_resultados = []
    for sym in activos_a_revisar:
        cat = categoria(sym)
        tf  = INTERVALOS_MERCADO.get(cat, {}).get("tf", "1d")
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
        print(f"\n  🔔 {len(eventos_cierre)} trade(s) cerrado(s):")
        for ev in eventos_cierre:
            print(f"    {ev['sym']}: {ev['mensaje']}")
        enviar_cierre_trades(eventos_cierre)

    senales_nuevas = filtrar_señales_nuevas(todos_resultados)

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


if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════╗
║   🤖 BOT DE TRADING v9.1 — MA CROSS FIX             ║
╠══════════════════════════════════════════════════════╣
║  Forex  ({len(FOREX)}):  SMC + fallback EMA20/50             ║
║  Oro    ({len(ORO)}):  SMA50/100 — ventana 3 velas        ║
║  BTC    ({len(CRYPTO)}):  RSI + Soporte/Resistencia         ║
║  Acc    ({len(ACCIONES)}):  RSI + EMA + Volumen               ║
║  ETF    ({len(ETFS)}):  RSI + EMA                         ║
╠══════════════════════════════════════════════════════╣
║  FIXES v9.1:                                         ║
║  • Oro: detecta cruces en ventana de 3 velas        ║
║  • Oro: siempre SMA50/SMA100 (= Pine Script)        ║
║  • Forex: siempre retorna dirección (fallback EMA)  ║
║  • Gráfico Oro: marca X en punto exacto del cruce  ║
╠══════════════════════════════════════════════════════╣
║  SL/TP: Forex 0.5/1% | Oro 1/2% | BTC 3/6%         ║
║         Acciones 2/4% | ETF 1.5/3% | Índices 1/2%  ║
╚══════════════════════════════════════════════════════╝""")
    revisar()
    while True:
        time.sleep(5 * 60)
        revisar()