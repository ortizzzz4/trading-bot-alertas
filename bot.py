"""
Bot de Alertas de Trading v6 — Estrategias por Mercado
Acciones / ETF / Forex / Oro / BTC — cada uno con sus propios parametros
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

PARAMS = {
    "ACCION":{"rsi_b":30,"rsi_a":70,"cambio":2.0,"ema_c":20,"ema_l":50},
    "ETF":   {"rsi_b":35,"rsi_a":65,"cambio":1.5,"ema_c":20,"ema_l":50},
    "FOREX": {"rsi_b":35,"rsi_a":65,"cambio":0.5,"ema_c":10,"ema_l":21},
    "ORO":   {"rsi_b":35,"rsi_a":65,"cambio":1.0,"ema_c":10,"ema_l":21},
    "CRYPTO":{"rsi_b":30,"rsi_a":70,"cambio":4.0,"ema_c":20,"ema_l":50},
}

ICONOS = {"ACCION":"📈","ETF":"🗂️","FOREX":"💱","ORO":"🥇","CRYPTO":"₿"}

DESC_CAT = {
    "ACCION":"Acciones — Opera solo 9:30-16:00 ET. RSI y EMA son los indicadores clave.",
    "ETF":   "ETFs — Menos volatiles. RSI con umbrales mas estrechos (35/65).",
    "FOREX": "Forex — Mercado 24h. Movimientos de 0.5% ya son significativos. EMA corta (10/21).",
    "ORO":   "Oro XAUUSD — Activo refugio. Sube en crisis, inflacion y dolar debil. EMA corta (10/21).",
    "CRYPTO":"Bitcoin — Alta volatilidad. Movimiento de 4%+ para confirmar senal. Usa stop loss amplio.",
}

CONSEJOS_PAR = {
    "EURUSD=X":"EUR/USD: afectado por noticias BCE y FED. Sesiones Londres/NY son las mas activas.",
    "USDJPY=X":"USD/JPY: sensible a tasas de Japon (BOJ). Yen es activo refugio — baja en crisis.",
    "USDCAD=X":"USD/CAD: correlacionado con precio del petroleo. Petroleo sube = USD/CAD baja.",
    "AUDUSD=X":"AUD/USD: ligado a commodities y economia China. Risk-on = AUD sube.",
    "GC=F":    "XAUUSD: revisa el indice dolar (DXY). Dolar debil = Oro sube. Dolar fuerte = Oro baja.",
    "BTC-USD": "BTC lidera el mercado crypto. Opera con stop loss del 5-8% por la alta volatilidad.",
}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]
SES = requests.Session()

def crumb():
    try:
        h={"User-Agent":random.choice(UA),"Accept":"text/html,*/*","Accept-Language":"en-US,en;q=0.5"}
        SES.get("https://finance.yahoo.com",headers=h,timeout=15)
        r=SES.get("https://query1.finance.yahoo.com/v1/test/getcrumb",headers=h,timeout=10)
        c=r.text.strip()
        return c if c and len(c)>3 else None
    except: return None

def datos(sym,cr=None):
    now=int(time.time()); ini=now-(90*24*3600)
    for i in range(3):
        try:
            h={"User-Agent":random.choice(UA),"Accept":"application/json,*/*",
               "Accept-Language":"en-US,en;q=0.9","Referer":f"https://finance.yahoo.com/quote/{sym}"}
            srv=["query1","query2"][i%2]
            url=f"https://{srv}.finance.yahoo.com/v8/finance/chart/{sym}?period1={ini}&period2={now}&interval=1d&includeAdjustedClose=true"
            if cr: url+=f"&crumb={cr}"
            resp=SES.get(url,headers=h,timeout=25)
            if not resp.text.strip() or resp.status_code!=200: time.sleep(3);continue
            d=resp.json(); res=d.get("chart",{}).get("result",[])
            if not res: time.sleep(3);continue
            r=res[0]; ts=r.get("timestamp",[])
            cl=r.get("indicators",{}).get("adjclose",[{}])[0].get("adjclose",[])
            vo=r.get("indicators",{}).get("quote",[{}])[0].get("volume",[])
            if not ts or not cl: continue
            df=pd.DataFrame({"t":ts,"Close":cl,"Volume":vo if vo else [None]*len(ts)}).dropna(subset=["Close"])
            df["Date"]=pd.to_datetime(df["t"],unit="s")
            return df.set_index("Date").sort_index()
        except:
            if i<2: time.sleep(4+i*2)
    return None

def rsi(s,n=14):
    d=s.diff(); g=d.where(d>0,0.0); p=-d.where(d<0,0.0)
    return 100-(100/(1+(g.rolling(n).mean()/p.rolling(n).mean())))

def recomendacion(senales,rsi_val):
    pc=pv=0
    for s in senales:
        w=2 if s["f"]=="MUY FUERTE" else 1.5 if s["f"]=="FUERTE" else 1
        if "COMPRA" in s["a"]: pc+=w
        elif "VENTA" in s["a"]: pv+=w
    t=pc+pv
    if t==0: return {"d":"NEUTRAL","c":"#888","e":"⚪","t":"Sin señal clara","m":"Espera mejor oportunidad."}
    r=pc/t
    if r>=0.75: return {"d":"COMPRAR","c":"#00B074","e":"✅","t":"SEÑAL DE COMPRA","m":f"Mayoria alcista. RSI={rsi_val:.0f}. Considera abrir BUY (posicion larga)."}
    if r<=0.25: return {"d":"VENDER","c":"#E94560","e":"❌","t":"SEÑAL DE VENTA","m":f"Mayoria bajista. RSI={rsi_val:.0f}. Considera abrir SELL (posicion corta)."}
    if r>0.5:   return {"d":"PROB. COMPRA","c":"#F5A623","e":"⚡","t":"TENDENCIA COMPRA — Esperar confirmacion","m":f"Mas señales alcistas. RSI={rsi_val:.0f}. Espera vela de confirmacion."}
    return {"d":"PROB. VENTA","c":"#FF6B6B","e":"⚠️","t":"TENDENCIA VENTA — Cuidado","m":f"Mas señales bajistas. RSI={rsi_val:.0f}. Ajusta stop loss."}

def analizar(sym,cr=None):
    try:
        cat=categoria(sym); p=PARAMS[cat]
        df=datos(sym,cr)
        if df is None or len(df)<55: return None
        c=df["Close"].squeeze()
        pa=float(c.iloc[-1]); py=float(c.iloc[-2])
        p7=float(c.iloc[-6]) if len(c)>=6 else py
        cd=((pa-py)/py)*100; c7=((pa-p7)/p7)*100
        ec=c.ewm(span=p["ema_c"],adjust=False).mean()
        el=c.ewm(span=p["ema_l"],adjust=False).mean()
        rv=float(rsi(c).iloc[-1])
        sn=[]
        if cd>=p["cambio"]:  sn.append({"tipo":"📈 SUBIDA FUERTE","a":"⚡ POSIBLE COMPRA","desc":f"Subio {cd:.2f}% (umbral {p['cambio']}% en {cat})","f":"MEDIA"})
        elif cd<=-p["cambio"]: sn.append({"tipo":"📉 CAIDA FUERTE","a":"⚠️ POSIBLE VENTA","desc":f"Cayo {abs(cd):.2f}% (umbral {p['cambio']}% en {cat})","f":"MEDIA"})
        if rv<=p["rsi_b"]:   sn.append({"tipo":"🔵 RSI SOBREVENDIDO","a":"✅ SEÑAL DE COMPRA","desc":f"RSI={rv:.1f} bajo {p['rsi_b']} — muy barato tecnicamente","f":"FUERTE"})
        elif rv>=p["rsi_a"]: sn.append({"tipo":"🔴 RSI SOBRECOMPRADO","a":"❌ SEÑAL DE VENTA","desc":f"RSI={rv:.1f} sobre {p['rsi_a']} — muy caro tecnicamente","f":"FUERTE"})
        if float(ec.iloc[-2])<float(el.iloc[-2]) and float(ec.iloc[-1])>float(el.iloc[-1]):
            sn.append({"tipo":"⭐ GOLDEN CROSS","a":"✅ COMPRA FUERTE","desc":f"EMA{p['ema_c']} cruzo ARRIBA a EMA{p['ema_l']} — tendencia alcista confirmada","f":"MUY FUERTE"})
        elif float(ec.iloc[-2])>float(el.iloc[-2]) and float(ec.iloc[-1])<float(el.iloc[-1]):
            sn.append({"tipo":"💀 DEATH CROSS","a":"❌ VENTA FUERTE","desc":f"EMA{p['ema_c']} cruzo ABAJO a EMA{p['ema_l']} — tendencia bajista confirmada","f":"MUY FUERTE"})
        if cat!="FOREX" and "Volume" in df.columns:
            v=df["Volume"].dropna()
            if len(v)>20:
                va=float(v.iloc[-1]); vp=float(v.rolling(20).mean().iloc[-1])
                if vp>0 and va/vp>=2.0:
                    sn.append({"tipo":"🔊 VOLUMEN INUSUAL","a":"👀 PRESTAR ATENCION","desc":f"Volumen {va/vp:.1f}x el promedio — movimiento institucional posible","f":"MEDIA"})
        if cat=="ORO":
            mx=float(c.rolling(90).max().iloc[-1])
            if pa>=mx*0.98: sn.append({"tipo":"🏆 MAXIMO 90d","a":"⚠️ ZONA RESISTENCIA","desc":f"Precio cerca del maximo 90 dias (${mx:,.2f}) — posible rebote","f":"MEDIA"})
        if cat=="CRYPTO":
            mn=float(c.rolling(30).min().iloc[-1]); mx=float(c.rolling(30).max().iloc[-1])
            dm=((pa-mn)/mn)*100; dM=((mx-pa)/mx)*100
            if dm<=5:  sn.append({"tipo":"🛡️ CERCA SOPORTE","a":"✅ ZONA DE COMPRA","desc":f"BTC a {dm:.1f}% del minimo 30d (${mn:,.0f})","f":"FUERTE"})
            elif dM<=5: sn.append({"tipo":"🚧 CERCA RESISTENCIA","a":"❌ ZONA DE VENTA","desc":f"BTC a {dM:.1f}% del maximo 30d (${mx:,.0f})","f":"FUERTE"})
        if not sn: return None
        rec=recomendacion(sn,rv)
        return {"sym":sym,"nombre":NOMBRES.get(sym,sym),"cat":cat,
                "icono":ICONOS[cat],"desc_cat":DESC_CAT[cat],"consejo":CONSEJOS_PAR.get(sym,""),
                "precio":pa,"cd":cd,"c7":c7,"rsi":rv,
                "ec":float(ec.iloc[-1]),"el":float(el.iloc[-1]),
                "ec_n":p["ema_c"],"el_n":p["ema_l"],
                "sn":sn,"rec":rec,"hora":datetime.now().strftime("%d/%m/%Y %H:%M")}
    except Exception as e:
        print(f"  ⚠️ {sym}: {e}"); return None

def html(res):
    grupos={}
    for r in res: grupos.setdefault(r["cat"],[]).append(r)
    body=""
    for cat in ["ACCION","ETF","ORO","CRYPTO","FOREX"]:
        if cat not in grupos: continue
        nombres_cat={"ACCION":"ACCIONES","ETF":"ETFs","ORO":"ORO (XAUUSD)","CRYPTO":"CRYPTO (BTC)","FOREX":"FOREX"}
        body+=(f'<div style="background:#0F3460;border-radius:10px;padding:10px 16px;margin:20px 0 8px;">'
               f'<span style="font-size:16px;">{ICONOS[cat]}</span> '
               f'<b style="color:#F5A623;font-size:14px;">{nombres_cat[cat]}</b>'
               f'<div style="color:#aaa;font-size:11px;margin-top:2px;">{DESC_CAT[cat]}</div></div>')
        for r in grupos[cat]:
            col="#00B074" if r["cd"]>=0 else "#E94560"
            sig="▲" if r["cd"]>=0 else "▼"
            rc=r["rec"]
            bq=""
            for s in r["sn"]:
                bg="#1a3a2a" if "COMPRA" in s["a"] else "#3a1a1a" if "VENTA" in s["a"] else "#1a2a3a"
                bq+=(f'<div style="background:{bg};border-radius:8px;padding:7px 10px;margin:4px 0;">'
                     f'<b style="font-size:12px;">{s["tipo"]}</b> — '
                     f'<span style="color:#F5A623;font-size:12px;">{s["a"]}</span><br>'
                     f'<span style="color:#ccc;font-size:11px;">{s["desc"]}</span> '
                     f'<span style="background:#0F3460;padding:1px 5px;border-radius:6px;font-size:10px;">{s["f"]}</span></div>')
            rec_b=(f'<div style="background:{rc["c"]}22;border:2px solid {rc["c"]};border-radius:10px;'
                   f'padding:12px;margin:10px 0;text-align:center;">'
                   f'<div style="font-size:24px;">{rc["e"]}</div>'
                   f'<div style="font-size:16px;font-weight:bold;color:{rc["c"]};margin:4px 0;">{rc["t"]}</div>'
                   f'<div style="color:#ccc;font-size:12px;">{rc["m"]}</div></div>')
            consejo_b=""
            if r["consejo"]:
                consejo_b=(f'<div style="background:#0a1a2a;border-left:3px solid #F5A623;padding:7px 10px;'
                           f'margin:6px 0;border-radius:0 6px 6px 0;font-size:11px;color:#aaa;">💡 {r["consejo"]}</div>')
            body+=(f'<div style="background:#16213E;border-radius:12px;padding:16px;margin:8px 0;border-left:4px solid {rc["c"]};">'
                   f'<table width="100%"><tr>'
                   f'<td><b style="font-size:16px;color:#fff;">{r["nombre"]}</b><br>'
                   f'<span style="color:#aaa;font-size:10px;">{r["hora"]}</span></td>'
                   f'<td align="right"><b style="font-size:16px;color:#fff;">${r["precio"]:,.4f}</b><br>'
                   f'<span style="color:{col};font-size:13px;">{sig} {abs(r["cd"]):.2f}% hoy</span></td></tr></table>'
                   f'<table width="100%" style="margin:8px 0;background:#0F3460;border-radius:8px;">'
                   f'<tr><td style="color:#aaa;font-size:10px;padding:5px 8px;">RSI</td>'
                   f'<td style="color:#aaa;font-size:10px;padding:5px 8px;">EMA{r["ec_n"]}</td>'
                   f'<td style="color:#aaa;font-size:10px;padding:5px 8px;">EMA{r["el_n"]}</td>'
                   f'<td style="color:#aaa;font-size:10px;padding:5px 8px;">7 dias</td></tr>'
                   f'<tr><td style="color:#F5A623;font-weight:bold;padding:5px 8px;">{r["rsi"]:.1f}</td>'
                   f'<td style="color:#fff;padding:5px 8px;">${r["ec"]:,.2f}</td>'
                   f'<td style="color:#fff;padding:5px 8px;">${r["el"]:,.2f}</td>'
                   f'<td style="color:{"#00B074" if r["c7"]>=0 else "#E94560"};padding:5px 8px;">{r["c7"]:+.2f}%</td></tr></table>'
                   f'{rec_b}{consejo_b}'
                   f'<details><summary style="color:#F5A623;font-size:11px;cursor:pointer;">📊 Ver señales detalladas</summary>{bq}</details>'
                   f'</div>')
    return (f'<html><body style="margin:0;padding:0;background:#0D0D1A;font-family:Arial,sans-serif;color:#fff;">'
            f'<div style="max-width:640px;margin:0 auto;padding:20px;">'
            f'<div style="background:#1A1A2E;border-radius:16px;padding:20px;text-align:center;margin-bottom:8px;">'
            f'<div style="font-size:28px;">📊</div>'
            f'<h1 style="margin:6px 0;font-size:20px;color:#F5A623;">ALERTA DE TRADING</h1>'
            f'<p style="color:#aaa;font-size:12px;">{len(res)} activo(s) con señales — {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>'
            f'<p style="color:#555;font-size:10px;">⚠️ No es asesoria financiera. Analiza antes de operar.</p></div>'
            f'{body}'
            f'<div style="text-align:center;color:#555;font-size:10px;padding:10px;border-top:1px solid #222;margin-top:12px;">'
            f'Bot Railway 24/7 • Proxima revision en {INTERVALO_MIN} min</div>'
            f'</div></body></html>')

def enviar(res):
    try:
        msg=MIMEMultipart("alternative")
        cats=set(r["cat"] for r in res)
        msg["Subject"]=f"🚨 {len(res)} Alerta(s) [{', '.join(cats)}] — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        msg["From"]=EMAIL_REMITENTE; msg["To"]=EMAIL_DESTINO
        txt="\n".join([f"{r['nombre']}: ${r['precio']:,.4f} ({r['cd']:+.2f}%) → {r['rec']['d']}" for r in res])
        msg.attach(MIMEText(txt,"plain")); msg.attach(MIMEText(html(res),"html"))
        with smtplib.SMTP("smtp.gmail.com",587,timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_REMITENTE,EMAIL_CONTRASENA)
            s.sendmail(EMAIL_REMITENTE,EMAIL_DESTINO,msg.as_string())
        print(f"  ✅ Correo enviado — {len(res)} alerta(s)")
    except Exception as e:
        print(f"  ❌ Error correo: {e}")

def revisar():
    print(f"\n{'='*55}")
    print(f"  🔍 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} — {len(TODOS)} activos")
    print(f"{'='*55}")
    cr=crumb()
    print(f"  {'✅ Conectado a Yahoo' if cr else '⚠️ Sin crumb, intentando...'}")
    res=[]
    for sym in TODOS:
        cat=categoria(sym)
        print(f"  [{cat:6}] {sym}...",end=" ",flush=True)
        r=analizar(sym,cr)
        if r:
            print(f"⚠️  {len(r['sn'])} señal(es) → {r['rec']['d']}")
            res.append(r)
        else:
            print("✅")
        time.sleep(2)
    print(f"\n  📊 {len(res)}/{len(TODOS)} con señales")
    if res: enviar(res)
    else: print("  😴 Sin señales")

if __name__=="__main__":
    print(f"""
╔══════════════════════════════════════════════════════╗
║   🤖 BOT DE TRADING v6 — ESTRATEGIAS POR MERCADO    ║
╠══════════════════════════════════════════════════════╣
║  Acciones ({len(ACCIONES)}):  RSI 30/70  EMA 20/50  >2%        ║
║  ETFs     ({len(ETFS)}):  RSI 35/65  EMA 20/50  >1.5%      ║
║  Forex    ({len(FOREX)}):  RSI 35/65  EMA 10/21  >0.5%      ║
║  Oro      ({len(ORO)}):  RSI 35/65  EMA 10/21  >1.0%       ║
║  BTC      ({len(CRYPTO)}):  RSI 30/70  EMA 20/50  >4.0%       ║
║  Intervalo: cada {INTERVALO_MIN} minutos                        ║
╚══════════════════════════════════════════════════════╝""")
    revisar()
    while True:
        time.sleep(INTERVALO_MIN*60)
        revisar()
