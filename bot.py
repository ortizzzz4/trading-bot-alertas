"""
Bot de Alertas de Trading - Version Railway v3
Usa requests directo para evitar bloqueo de Yahoo Finance
"""

import requests
import pandas as pd
import numpy as np
import smtplib
import time
import os
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

EMAIL_REMITENTE  = os.environ.get("EMAIL_REMITENTE",  "tu_correo@gmail.com")
EMAIL_CONTRASENA = os.environ.get("EMAIL_CONTRASENA", "xxxx xxxx xxxx xxxx")
EMAIL_DESTINO    = os.environ.get("EMAIL_DESTINO",    "tu_correo@gmail.com")
INTERVALO_MIN    = int(os.environ.get("INTERVALO_MINUTOS", "30"))

ACTIVOS = [
    "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "GOOGL", "META",
    "BTC-USD", "ETH-USD", "SOL-USD",
    "EURUSD=X", "GBPUSD=X",
    "GC=F", "SI=F",
    "VTI", "VOO", "BND", "SDY",
]

CAMBIO_PRECIO_PCT = float(os.environ.get("CAMBIO_PRECIO_PCT", "2.0"))
RSI_SOBREVENDIDO  = float(os.environ.get("RSI_SOBREVENDIDO",  "30"))
RSI_SOBRECOMPRADO = float(os.environ.get("RSI_SOBRECOMPRADO", "70"))
EMA_CORTA         = int(os.environ.get("EMA_CORTA", "20"))
EMA_LARGA         = int(os.environ.get("EMA_LARGA", "50"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]

SESSION = requests.Session()

def obtener_crumb():
    try:
        h = {"User-Agent": random.choice(USER_AGENTS),
             "Accept": "text/html,application/xhtml+xml,*/*",
             "Accept-Language": "en-US,en;q=0.5"}
        SESSION.get("https://finance.yahoo.com", headers=h, timeout=15)
        r = SESSION.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=h, timeout=10)
        c = r.text.strip()
        return c if c and len(c) > 3 else None
    except:
        return None

def descargar_datos(simbolo, crumb=None):
    """Descarga con 3 reintentos y 2 servidores alternativos."""
    ahora  = int(time.time())
    inicio = ahora - (90 * 24 * 3600)
    servidores = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    for intento in range(3):
        try:
            h = {"User-Agent": random.choice(USER_AGENTS),
                 "Accept": "application/json,*/*",
                 "Accept-Language": "en-US,en;q=0.9",
                 "Referer": f"https://finance.yahoo.com/quote/{simbolo}"}
            servidor = servidores[intento % 2]
            url = (f"https://{servidor}/v8/finance/chart/{simbolo}"
                   f"?period1={inicio}&period2={ahora}&interval=1d&includeAdjustedClose=true")
            if crumb:
                url += f"&crumb={crumb}"
            resp = SESSION.get(url, headers=h, timeout=25)
            if not resp.text.strip() or resp.status_code != 200:
                time.sleep(3)
                continue
            data   = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                time.sleep(3)
                continue
            r      = result[0]
            ts     = r.get("timestamp", [])
            closes = r.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", [])
            vols   = r.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
            if not ts or not closes:
                continue
            df = pd.DataFrame({"timestamp": ts, "Close": closes,
                               "Volume": vols if vols else [None]*len(ts)}).dropna(subset=["Close"])
            df["Date"] = pd.to_datetime(df["timestamp"], unit="s")
            return df.set_index("Date").sort_index()
        except Exception as e:
            if intento < 2:
                time.sleep(4 + intento * 2)
            continue
    return None

def calcular_rsi(serie, periodos=14):
    delta    = serie.diff()
    ganancia = delta.where(delta > 0, 0.0)
    perdida  = -delta.where(delta < 0, 0.0)
    rs = ganancia.rolling(periodos).mean() / perdida.rolling(periodos).mean()
    return 100 - (100 / (1 + rs))

def analizar_activo(simbolo, crumb=None):
    try:
        datos = descargar_datos(simbolo, crumb)
        if datos is None or len(datos) < 55:
            return None
        close = datos["Close"].squeeze()
        precio_actual  = float(close.iloc[-1])
        precio_ayer    = float(close.iloc[-2])
        precio_hace_7d = float(close.iloc[-6]) if len(close) >= 6 else precio_ayer
        cambio_dia = ((precio_actual - precio_ayer)    / precio_ayer)    * 100
        cambio_7d  = ((precio_actual - precio_hace_7d) / precio_hace_7d) * 100
        ema_c = close.ewm(span=EMA_CORTA, adjust=False).mean()
        ema_l = close.ewm(span=EMA_LARGA, adjust=False).mean()
        rsi_actual = float(calcular_rsi(close).iloc[-1])
        senales = []
        if cambio_dia >= CAMBIO_PRECIO_PCT:
            senales.append({"tipo": "📈 SUBIDA FUERTE",    "accion": "⚡ POSIBLE COMPRA",
                             "desc": f"Subio {cambio_dia:.2f}% hoy", "fuerza": "MEDIA"})
        elif cambio_dia <= -CAMBIO_PRECIO_PCT:
            senales.append({"tipo": "📉 CAIDA FUERTE",     "accion": "⚠️ POSIBLE VENTA",
                             "desc": f"Cayo {abs(cambio_dia):.2f}% hoy", "fuerza": "MEDIA"})
        if rsi_actual <= RSI_SOBREVENDIDO:
            senales.append({"tipo": "🔵 RSI SOBREVENDIDO",  "accion": "✅ SEÑAL DE COMPRA",
                             "desc": f"RSI={rsi_actual:.1f} muy barato", "fuerza": "FUERTE"})
        elif rsi_actual >= RSI_SOBRECOMPRADO:
            senales.append({"tipo": "🔴 RSI SOBRECOMPRADO", "accion": "❌ SEÑAL DE VENTA",
                             "desc": f"RSI={rsi_actual:.1f} muy caro", "fuerza": "FUERTE"})
        if (float(ema_c.iloc[-2]) < float(ema_l.iloc[-2])) and (float(ema_c.iloc[-1]) > float(ema_l.iloc[-1])):
            senales.append({"tipo": "⭐ GOLDEN CROSS", "accion": "✅ COMPRA FUERTE",
                             "desc": f"EMA{EMA_CORTA} cruzo ARRIBA a EMA{EMA_LARGA}", "fuerza": "MUY FUERTE"})
        elif (float(ema_c.iloc[-2]) > float(ema_l.iloc[-2])) and (float(ema_c.iloc[-1]) < float(ema_l.iloc[-1])):
            senales.append({"tipo": "💀 DEATH CROSS",   "accion": "❌ VENTA FUERTE",
                             "desc": f"EMA{EMA_CORTA} cruzo ABAJO a EMA{EMA_LARGA}", "fuerza": "MUY FUERTE"})
        if "Volume" in datos.columns:
            vol = datos["Volume"].dropna()
            if len(vol) > 20:
                vol_actual   = float(vol.iloc[-1])
                vol_promedio = float(vol.rolling(20).mean().iloc[-1])
                if vol_promedio > 0 and vol_actual / vol_promedio >= 2.0:
                    senales.append({"tipo": "🔊 VOLUMEN INUSUAL", "accion": "👀 PRESTAR ATENCION",
                                     "desc": f"Volumen {vol_actual/vol_promedio:.1f}x el promedio", "fuerza": "MEDIA"})
        if not senales:
            return None
        return {"simbolo": simbolo, "precio": precio_actual, "cambio_dia": cambio_dia,
                "cambio_7d": cambio_7d, "rsi": rsi_actual, "ema_corta": float(ema_c.iloc[-1]),
                "ema_larga": float(ema_l.iloc[-1]), "senales": senales,
                "hora": datetime.now().strftime("%d/%m/%Y %H:%M")}
    except Exception as e:
        print(f"  ⚠️ Error en {simbolo}: {e}")
        return None

def construir_html(resultados):
    filas = ""
    for r in resultados:
        color = "#00B074" if r["cambio_dia"] >= 0 else "#E94560"
        signo = "▲" if r["cambio_dia"] >= 0 else "▼"
        bloques = ""
        for s in r["senales"]:
            bg = "#1a3a2a" if "COMPRA" in s["accion"] else "#3a1a1a" if "VENTA" in s["accion"] else "#1a2a3a"
            bloques += f'<div style="background:{bg};border-radius:8px;padding:10px 14px;margin:6px 0;"><div style="font-size:15px;font-weight:bold;">{s["tipo"]}</div><div style="color:#F5A623;font-weight:bold;">{s["accion"]}</div><div style="color:#ccc;font-size:13px;">{s["desc"]}</div><span style="background:#0F3460;padding:2px 8px;border-radius:10px;font-size:11px;">Fuerza: {s["fuerza"]}</span></div>'
        filas += f'<div style="background:#16213E;border-radius:12px;padding:20px;margin:16px 0;border-left:4px solid #F5A623;"><table width="100%"><tr><td><span style="font-size:22px;font-weight:bold;color:#fff;">{r["simbolo"]}</span><br><span style="color:#aaa;font-size:12px;">{r["hora"]}</span></td><td align="right"><span style="font-size:20px;font-weight:bold;color:#fff;">${r["precio"]:,.4f}</span><br><span style="color:{color};font-weight:bold;">{signo} {abs(r["cambio_dia"]):.2f}%</span></td></tr></table><table width="100%" style="margin:12px 0;background:#0F3460;border-radius:8px;"><tr><td style="color:#aaa;font-size:12px;padding:6px 10px;">RSI</td><td style="color:#aaa;font-size:12px;padding:6px 10px;">EMA{EMA_CORTA}</td><td style="color:#aaa;font-size:12px;padding:6px 10px;">EMA{EMA_LARGA}</td><td style="color:#aaa;font-size:12px;padding:6px 10px;">7d</td></tr><tr><td style="color:#F5A623;font-weight:bold;padding:6px 10px;">{r["rsi"]:.1f}</td><td style="color:#fff;padding:6px 10px;">${r["ema_corta"]:,.2f}</td><td style="color:#fff;padding:6px 10px;">${r["ema_larga"]:,.2f}</td><td style="color:{"#00B074" if r["cambio_7d"]>=0 else "#E94560"};padding:6px 10px;">{r["cambio_7d"]:+.2f}%</td></tr></table><b style="color:#F5A623;">🚨 SEÑALES:</b>{bloques}</div>'
    return f'<html><body style="margin:0;padding:0;background:#0D0D1A;font-family:Arial,sans-serif;color:#fff;"><div style="max-width:620px;margin:0 auto;padding:20px;"><div style="background:#1A1A2E;border-radius:16px;padding:24px;text-align:center;margin-bottom:20px;"><div style="font-size:32px;">📊</div><h1 style="margin:8px 0;font-size:22px;color:#F5A623;">ALERTA DE TRADING</h1><p style="color:#aaa;font-size:13px;">{len(resultados)} activo(s) con señales</p><p style="color:#555;font-size:11px;">⚠️ No es asesoría financiera.</p></div>{filas}<div style="text-align:center;color:#555;font-size:11px;padding:12px;border-top:1px solid #222;">Bot Railway 24/7 • Próxima revisión en {INTERVALO_MIN} min</div></div></body></html>'

def enviar_correo(resultados):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🚨 {len(resultados)} Alerta(s) Trading — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        msg["From"] = EMAIL_REMITENTE
        msg["To"]   = EMAIL_DESTINO
        texto = "\n".join([f"{r['simbolo']}: ${r['precio']:,.4f} ({r['cambio_dia']:+.2f}%)" for r in resultados])
        msg.attach(MIMEText(texto, "plain"))
        msg.attach(MIMEText(construir_html(resultados), "html"))
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.sendmail(EMAIL_REMITENTE, EMAIL_DESTINO, msg.as_string())
        print(f"  ✅ Correo enviado — {len(resultados)} alerta(s)")
    except Exception as e:
        print(f"  ❌ Error correo: {e}")

def revisar_mercados():
    print(f"\n{'='*50}")
    print(f"  🔍 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — {len(ACTIVOS)} activos")
    print(f"{'='*50}")
    print("  🍪 Conectando con Yahoo Finance...")
    crumb = obtener_crumb()
    print(f"  {'✅ Conectado' if crumb else '⚠️ Sin crumb, intentando igual...'}")
    resultados = []
    for simbolo in ACTIVOS:
        print(f"  {simbolo}...", end=" ", flush=True)
        r = analizar_activo(simbolo, crumb)
        if r:
            print(f"⚠️  {len(r['senales'])} señal(es)")
            resultados.append(r)
        else:
            print("✅")
        time.sleep(2)
    print(f"\n  📊 {len(resultados)}/{len(ACTIVOS)} con señales")
    if resultados:
        enviar_correo(resultados)
    else:
        print("  😴 Sin señales")

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════╗
║     🤖 BOT DE TRADING — RAILWAY v3               ║
║     {len(ACTIVOS)} activos monitoreados                      ║
║     Cada {INTERVALO_MIN} minutos                             ║
╚══════════════════════════════════════════════════╝""")
    revisar_mercados()
    while True:
        time.sleep(INTERVALO_MIN * 60)
        revisar_mercados()
