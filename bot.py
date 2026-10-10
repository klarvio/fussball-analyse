"""Fussball-Bot: holt Spiele, zeigt Fakten aus der Tabelle, ergänzt KI-Analyse (falls Schlüssel vorhanden), baut docs/index.html, meldet per Telegram."""
import os, json, math, time, datetime as dt, requests
from zoneinfo import ZoneInfo

FD = os.environ["FOOTBALL_DATA_TOKEN"].strip()
COMPS = (os.environ.get("COMPETITIONS") or "BL1").replace(" ", "").strip()          # z. B. BL1,PL,PD
MODEL = os.environ.get("MODEL") or "claude-sonnet-5-5"
SITE = os.environ.get("SITE_URL", "")
TG_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
TG_CHAT = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
TZ = ZoneInfo("Europe/Berlin")

client = None
if os.environ.get("ANTHROPIC_API_KEY"):
    import anthropic
    client = anthropic.Anthropic()

def fd(path, **params):
    r = requests.get("https://api.football-data.org/v4" + path, params=params,
                     headers={"X-Auth-Token": FD}, timeout=30)
    time.sleep(6.5)  # Gratis-Tarif: max. 10 Abfragen pro Minute
    r.raise_for_status()
    return r.json()

def load_state():
    try:
        return json.load(open("data.json", encoding="utf-8"))
    except Exception:
        return {"matches": {}}

def row(table, team_id):
    for r in table:
        if r["team"]["id"] == team_id:
            return {k: r.get(k) for k in ("position", "playedGames", "won", "draw", "lost",
                                          "points", "goalsFor", "goalsAgainst", "form")}
    return None

def basic_facts(m, home, away):
    out = []
    for name, r in ((m["homeTeam"]["name"], home), (m["awayTeam"]["name"], away)):
        if r:
            out.append(f'{name}: Platz {r["position"]}, {r["points"]} Punkte aus {r["playedGames"]} Spielen, '
                       f'Tore {r["goalsFor"]}:{r["goalsAgainst"]}.')
    return out or ["Zu diesem Spiel liegen noch keine Tabellendaten vor."]

def d1(x):
    return f"{x:.1f}".replace(".", ",")

def pois(l, k):
    return math.exp(-l) * l ** k / math.factorial(k)

def model(m, home, away, rows, teams):
    """Poisson-Modell aus Tabelle (Tore pro Spiel) und Form. Kein KI-Dienst nötig."""
    pl = sum(r["playedGames"] for r in rows)
    nh, na = home["playedGames"] or 0, away["playedGames"] or 0
    if not pl or not nh or not na:
        return {}
    g = max(sum(r["goalsFor"] for r in rows) / pl, 0.5)
    def st(r, n, tid):
        w = n / (n + 6)
        att = 1 + (r["goalsFor"] / n / g - 1) * w
        dfn = 1 + (r["goalsAgainst"] / n / g - 1) * w
        f = teams.get(str(tid), {}).get("form") or []
        pf = sum(3 if x["r"] == "S" else 1 if x["r"] == "U" else 0 for x in f) / (3 * len(f)) if f else 0.5
        return att, dfn, 1 + 0.2 * (pf - 0.5), pf
    ah, dh, fh, pfh = st(home, nh, m["homeTeam"]["id"])
    aa, da, fa, pfa = st(away, na, m["awayTeam"]["id"])
    lh, la = g * 1.12 * ah * da * fh, g * 0.88 * aa * dh * fa
    P = [[pois(lh, i) * pois(la, j) for j in range(9)] for i in range(9)]
    tot = sum(map(sum, P))
    s = lambda c: sum(P[i][j] for i in range(9) for j in range(9) if c(i, j)) / tot
    p = [round(100 * s(lambda i, j: i > j)), round(100 * s(lambda i, j: i == j))]
    p.append(100 - sum(p))
    over, btts = s(lambda i, j: i + j >= 3), s(lambda i, j: i >= 1 and j >= 1)
    hn = m["homeTeam"].get("shortName") or m["homeTeam"]["name"]
    an = m["awayTeam"].get("shortName") or m["awayTeam"]["name"]
    mx = max(p)
    if mx >= 45:
        tipp = ["Heimsieg", "Unentschieden", "Auswärtssieg"][p.index(mx)]
    else:
        tipp = "Heimsieg oder Unentschieden (1X)" if p[0] >= p[2] else "Unentschieden oder Auswärtssieg (X2)"
    extra = []
    if over >= 0.58: extra.append(f"Über 2,5 Tore ({over*100:.0f} %)")
    if over <= 0.42: extra.append(f"Unter 2,5 Tore ({(1-over)*100:.0f} %)")
    if btts >= 0.60: extra.append(f"Beide Teams treffen ({btts*100:.0f} %)")
    if btts <= 0.40: extra.append(f"Nicht beide Teams treffen ({(1-btts)*100:.0f} %)")
    text = (f"Laut Modell werden {d1(lh)} Tore für {hn} und {d1(la)} für {an} erwartet. "
            f"{hn} erzielte bisher {d1(home['goalsFor']/nh)} Tore pro Spiel und kassierte {d1(home['goalsAgainst']/nh)}, "
            f"{an} erzielte {d1(away['goalsFor']/na)} und kassierte {d1(away['goalsAgainst']/na)}. "
            f"Punkteausbeute der letzten Spiele: {hn} {pfh*100:.0f} %, {an} {pfa*100:.0f} %. Heimvorteil ist eingerechnet.")
    return {"analyse": text, "p": p, "tipp": tipp, "extra": extra,
            "note": "Modellsicherheit: " + ("mittel" if mx >= 55 else "niedrig")}

AI_MODEL = os.environ.get("AI_MODEL") or "claude-haiku-4-5-20251001"
AI_MAX_RUN = int(os.environ.get("AI_MAX_RUN") or 3)        # max. Recherchen pro Lauf
AI_MAX_MONTH = int(os.environ.get("AI_MAX_MONTH") or 100)  # harte Obergrenze pro Monat

LAGE_PROMPT = """Recherchiere im Web die aktuelle Lage zu diesem Fußballspiel: Verletzte und Gesperrte, voraussichtliche Aufstellung, Trainerwechsel, Spielverlegung, Wetter.
Regeln: Nutze nur, was du in Quellen findest. Schreibe alles in eigenen Worten (keine Zitate, keine Textübernahme). Keine Wettempfehlung. Wenn du nichts Verlässliches findest, gib eine leere Liste zurück.
Antworte danach NUR mit JSON: {"punkte":["höchstens 4 kurze Punkte auf Deutsch"],"unsicher":true oder false}
"unsicher" ist true, wenn wichtige Spieler fehlen oder sich die Lage deutlich geändert hat.
Spiel: """

def lage(e):
    msg = client.messages.create(
        model=AI_MODEL, max_tokens=800,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
        messages=[{"role": "user", "content": LAGE_PROMPT + f'{e["heim"]} gegen {e["gast"]} ({e["liga"]}), Anstoß {e["zeit"]} Uhr.'}])
    print("KI-Nutzung:", msg.usage.input_tokens, "Eingabe-Tokens,", msg.usage.output_tokens, "Ausgabe-Tokens")
    quellen, text = [], ""
    for b in msg.content:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            for r in b.content:
                if r.url.startswith("http") and r.url not in [q["u"] for q in quellen]:
                    quellen.append({"t": r.title[:80], "u": r.url})
        elif b.type == "text":
            text += b.text
    d = json.loads(text[text.index("{"): text.rindex("}") + 1])
    return {"punkte": [str(x)[:220] for x in d.get("punkte", [])][:4], "unsicher": bool(d.get("unsicher")),
            "quellen": quellen[:3], "ts": dt.datetime.now(TZ).isoformat(timespec="minutes")}

def safe(path, **p):
    try:
        return fd(path, **p)
    except Exception as e:
        print("API-Fehler", path, e)
        return {}

def main():
    state = load_state()
    state.setdefault("comps", {}); state.setdefault("teams", {})
    today = dt.date.today()
    days = int(os.environ.get("DAYS") or 7)
    resp = fd("/matches", competitions=COMPS, dateFrom=today.isoformat(),
              dateTo=(today + dt.timedelta(days=days)).isoformat())
    matches = resp.get("matches", [])
    print("DIAGNOSE Ligen:", COMPS, "| Spiele:", len(matches), "| Zeitraum:", resp.get("filters"))
    tables, new_ids, names = {}, [], {}
    for m in matches:
        names[m["competition"]["code"]] = m["competition"]["name"]
    for code, cname in names.items():
        rows = [r for s in safe(f"/competitions/{code}/standings").get("standings", []) for r in s["table"]]
        tables[code] = rows
        sc = safe(f"/competitions/{code}/scorers", limit=10).get("scorers", [])
        state["comps"][code] = {
            "name": cname,
            "table": [{"id": r["team"]["id"], "n": r["team"].get("shortName") or r["team"]["name"],
                       "pos": r["position"], "pl": r["playedGames"], "pts": r["points"],
                       "gd": r.get("goalDifference")} for r in rows],
            "scorers": [{"n": s["player"]["name"], "t": s["team"].get("shortName") or s["team"]["name"],
                         "g": s.get("goals"), "a": s.get("assists")} for s in sc]}
        fin = safe(f"/competitions/{code}/matches", status="FINISHED",
                   dateFrom=(today - dt.timedelta(days=35)).isoformat(), dateTo=today.isoformat()).get("matches", [])
        for x in sorted(fin, key=lambda z: z["utcDate"]):
            ft = x["score"]["fullTime"]
            if ft["home"] is None:
                continue
            for side, other, mine, theirs in (("homeTeam", "awayTeam", ft["home"], ft["away"]),
                                              ("awayTeam", "homeTeam", ft["away"], ft["home"])):
                t = state["teams"].setdefault(str(x[side]["id"]), {})
                t.update({"name": x[side].get("shortName") or x[side]["name"]})
                t.setdefault("_f", []).append({"opp": x[other].get("shortName") or x[other]["name"], "sc": f"{mine}:{theirs}",
                                              "r": "S" if mine > theirs else "N" if mine < theirs else "U",
                                              "home": side == "homeTeam"})
    for t in state["teams"].values():
        t["form"] = t.pop("_f", t.get("form", []))[-5:]
    for m in matches:
        mid = str(m["id"])
        is_new = mid not in state["matches"]
        ko = dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(TZ)
        ft = m["score"]["fullTime"]
        near = ko < dt.datetime.now(TZ) + dt.timedelta(hours=48)
        entry = state["matches"].get(mid, {})
        entry.update({"liga": m["competition"]["name"], "code": m["competition"]["code"], "md": m.get("matchday"),
                      "zeit": ko.strftime("%d.%m. %H:%M"), "ts": ko.isoformat(), "st": m["status"],
                      "heim": m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                      "gast": m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                      "hid": m["homeTeam"]["id"], "gid": m["awayTeam"]["id"],
                      "score": f'{ft["home"]}:{ft["away"]}' if ft["home"] is not None else ""})
        for side in ("homeTeam", "awayTeam"):
            tid = str(m[side]["id"])
            t = state["teams"].setdefault(tid, {"name": m[side]["name"]})
            if near and "squad" not in t:
                d = safe(f"/teams/{tid}")
                t.update({"venue": d.get("venue"), "coach": (d.get("coach") or {}).get("name"),
                          "squad": [{"n": p["name"], "p": p.get("position")} for p in d.get("squad", [])]})
        if near and "h2h" not in entry:
            h = safe(f"/matches/{mid}/head2head", limit=5).get("matches", [])
            entry["h2h"] = [{"d": x["utcDate"][:10], "h": x["homeTeam"].get("shortName") or x["homeTeam"]["name"],
                             "a": x["awayTeam"].get("shortName") or x["awayTeam"]["name"],
                             "s": f'{x["score"]["fullTime"]["home"]}:{x["score"]["fullTime"]["away"]}'} for x in h]
        home, away = row(tables.get(entry["code"], []), m["homeTeam"]["id"]), row(tables.get(entry["code"], []), m["awayTeam"]["id"])
        if "fakten" not in entry:
            entry["fakten"] = basic_facts(m, home, away)
        ai_added = False
        if home and away and m["status"] in ("TIMED", "SCHEDULED"):
            had = "tipp" in entry
            entry.update(model(m, home, away, tables.get(entry["code"], []), state["teams"]))
            ai_added = not had and "tipp" in entry
        state["matches"][mid] = entry
        if is_new or ai_added:
            new_ids.append(mid)
    if client:
        ai = state.setdefault("ai", {})
        mon = today.strftime("%Y-%m")
        if ai.get("m") != mon:
            ai.update({"m": mon, "n": 0})
        now = dt.datetime.now(TZ)
        order = {c: i for i, c in enumerate(COMPS.split(","))}
        cand = []
        for mid, e in state["matches"].items():
            ko = dt.datetime.fromisoformat(e["ts"])
            if e.get("st") in ("TIMED", "SCHEDULED") and now < ko < now + dt.timedelta(hours=24):
                old = e.get("lage")
                age = (now - dt.datetime.fromisoformat(old["ts"])).total_seconds() / 3600 if old else 99
                if age >= 12:
                    cand.append((1 if old else 0, order.get(e["code"], 99), e["ts"], mid))
        for *_, mid in sorted(cand)[:AI_MAX_RUN]:
            if ai["n"] >= AI_MAX_MONTH:
                print("KI-Monatslimit erreicht:", ai["n"])
                break
            ai["n"] += 1
            try:
                state["matches"][mid]["lage"] = lage(state["matches"][mid])
            except Exception as ex:
                print("Lage-Recherche fehlgeschlagen für", mid, ex)
                state["matches"][mid]["lage"] = {"punkte": [], "quellen": [], "unsicher": False, "ts": now.isoformat(timespec="minutes")}
        print("KI-Recherchen diesen Monat:", ai["n"], "von", AI_MAX_MONTH)
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=2)).isoformat()
    state["matches"] = {k: v for k, v in state["matches"].items() if v.get("ts", "") > cutoff}
    state["updated"] = dt.datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    state["new"] = new_ids
    json.dump(state, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    os.makedirs("docs", exist_ok=True)
    data = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    open("docs/index.html", "w", encoding="utf-8").write(HTML.replace("__DATA__", data))

    def tg(text):
        if not (TG_TOKEN and TG_CHAT):
            print("TELEGRAM: Token oder Chat-ID fehlt")
            return
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": text}, timeout=30)
        print("TELEGRAM:", r.status_code, r.text[:200])
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        tg("✅ Testnachricht: Der Bot läuft.")
    if new_ids:
        tg(f"⚽ {len(new_ids)} neue(s) Spiel(e) bzw. Analyse(n) verfügbar.\n{SITE}\n\nUnverbindlich, keine Garantie. Nur ab 18.")

HTML = """<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Klarvio Fußball</title>
<style>
:root{--bg:#0B0F1A;--card:#141A2B;--card2:#1B2338;--ink:#F1F4FB;--mute:#8A94AD;--line:#232C45;--blue:#3D7BFF;--g:#2FD07E;--r:#FF5468;--y:#FFD24A;
padding-top:env(safe-area-inset-top,0px)}
@media(prefers-color-scheme:light){:root{--bg:#F3F5FA;--card:#fff;--card2:#EEF1F8;--ink:#0B1020;--mute:#64708C;--line:#E0E5F0;--blue:#1F5BFF;--g:#12A560;--r:#E23A50;--y:#C99A00}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 16px 8px;border-bottom:1px solid var(--line)}
.top{max-width:640px;margin:0 auto}.logo{display:flex;align-items:center;gap:10px;font-weight:800;font-size:1.3rem;letter-spacing:-.02em}.logo .lg{width:34px;height:34px;flex:none}.logo b{color:var(--mute);font-weight:600}
.days{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin-top:10px}
.dy{border:0;background:var(--card);color:var(--mute);border-radius:12px;padding:6px 0 7px;font:600 .72rem inherit;cursor:pointer;text-align:center;display:flex;flex-direction:column;align-items:center;gap:1px;min-width:0}
.dy b{font-size:.78rem;color:var(--ink)}.dy i{width:5px;height:5px;border-radius:50%;background:var(--g);margin-top:2px}
.dy[aria-pressed=true]{background:linear-gradient(135deg,#2B5BFF,#7A5CFF);color:#fff}.dy[aria-pressed=true] b{color:#fff}.dy[aria-pressed=true] i{background:#fff}
.hero{display:flex;align-items:center;justify-content:space-between;gap:10px;background:linear-gradient(135deg,#2B5BFF,#6B4DFF);color:#fff;border-radius:22px;padding:18px;margin-bottom:12px;overflow:hidden}
.hero h1{font-size:1.5rem;line-height:1.1;margin:0 0 6px;font-weight:800;letter-spacing:-.02em}.hero p{margin:0;opacity:.85;font-size:.85rem}.hero svg{width:110px;flex:none}
main{max-width:640px;margin:0 auto;padding:12px 16px calc(96px + env(safe-area-inset-bottom,0px))}
.w{background:var(--card2);color:var(--mute);border-radius:12px;padding:9px 12px;font-size:.78rem;margin-bottom:12px}
.lg{display:flex;align-items:center;gap:8px;font-weight:700;margin:16px 0 6px;font-size:.85rem;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.box{background:var(--card);border-radius:16px;overflow:hidden}
.mr{display:grid;grid-template-columns:48px 1fr auto;gap:10px;align-items:center;padding:12px 14px;border-top:1px solid var(--line)}
.box>:first-child{border-top:0}
.mt{font-weight:700;font-size:.9rem;text-align:center;color:var(--mute)}
.tn{display:flex;align-items:center;gap:8px;margin:2px 0;font-weight:600}.tn em{margin-left:auto;font-style:normal;font-weight:800}
.cr{width:22px;height:22px;object-fit:contain;flex:none}.ph{background:var(--card2);border-radius:50%;display:grid;place-items:center;font-size:.58rem;font-weight:700}
.pk{font-size:.75rem;color:var(--blue);font-weight:600}.nw{background:var(--r);color:#fff;border-radius:5px;padding:0 6px;font-size:.65rem;margin-left:6px}
.star{border:0;background:none;color:var(--mute);font-size:1.4rem;cursor:pointer;padding:4px}.star.on{color:var(--y)}
nav{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--line);display:flex;justify-content:center;padding-bottom:env(safe-area-inset-bottom,0px)}
nav a{flex:1;max-width:160px;text-align:center;padding:10px 4px;color:var(--mute);font-size:.75rem;font-weight:600}
nav a.on{color:var(--blue)}nav a span{display:block;font-size:1.3rem}
.hd{background:var(--card);border-radius:20px;padding:18px 14px;text-align:center;margin-bottom:12px}
.hd .sc{font-size:2.2rem;font-weight:800;letter-spacing:.02em}.hd .row{display:flex;justify-content:space-around;align-items:center;gap:6px}
.hd .cr{width:54px;height:54px}.hd a{display:block;font-weight:700;font-size:.9rem}
.tb{display:flex;gap:6px;overflow-x:auto;margin:0 0 12px}.tb button{flex:none;border:0;background:var(--card);color:var(--mute);border-radius:999px;padding:7px 14px;font:600 .85rem inherit;cursor:pointer}.tb button[aria-pressed=true]{background:var(--ink);color:var(--bg)}
.sec{background:var(--card);border-radius:16px;padding:14px;margin-bottom:12px}.sec h3{margin:0 0 8px;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}.sec ul{margin:0;padding-left:18px}
.fm{display:flex;gap:5px}.fm i{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;font-style:normal;font-weight:800;font-size:.72rem;color:#fff}
.S{background:var(--g)}.U{background:var(--mute)}.N{background:var(--r)}
.pb{display:flex;height:10px;border-radius:5px;overflow:hidden;gap:2px;margin-top:6px}.pl{display:flex;justify-content:space-between;font-size:.78rem;color:var(--mute);margin-top:4px}
.tip{background:var(--card2);border-left:4px solid var(--blue);border-radius:10px;padding:9px 12px;margin-top:10px}.tip small{display:block;color:var(--mute)}
table{width:100%;border-collapse:collapse;font-size:.88rem}td,th{padding:7px 4px;text-align:right}td:nth-child(2),th:nth-child(2){text-align:left}th{color:var(--mute);font-weight:600;font-size:.72rem}tr.me{background:var(--card2)}td b{font-weight:800}
.tt{display:flex;align-items:center;gap:7px}.no{color:var(--mute);font-size:.88rem}
footer{margin-top:20px;font-size:.75rem;color:var(--mute)}
</style></head><body>
<header><div class="top"><div class="logo"><svg class="lg" viewBox="0 0 36 36" aria-hidden="true"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3D7BFF"/><stop offset="1" stop-color="#8A5CFF"/></linearGradient></defs><rect width="36" height="36" rx="10" fill="url(#g)"/><circle cx="18" cy="18" r="11" fill="none" stroke="#fff" stroke-width="2"/><polygon points="18,13 22.8,16.5 20.9,22.1 15.1,22.1 13.2,16.5" fill="#fff"/><g stroke="#fff" stroke-width="1.6" stroke-linecap="round"><path d="M18 13V7M22.8 16.5L28.5 14.6M20.9 22.1L24.4 26.9M15.1 22.1L11.6 26.9M13.2 16.5L7.5 14.6"/></g><circle cx="29" cy="7" r="3" fill="#2FD07E"/></svg><span>klarvio<b>.fussball</b></span></div><div class="days" id="dys"></div></div></header>
<main id="v"></main>
<nav id="nv"></nav>
<script>
const D=__DATA__,E=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const M=D.matches||{},T=D.teams||{},C=D.comps||{};
const ms=Object.entries(M).sort((a,b)=>a[1].ts<b[1].ts?-1:1);
let day=null,fav=[],mtab="u";
try{fav=JSON.parse(localStorage.getItem("fav")||"[]")}catch(e){}
const sv=()=>{try{localStorage.setItem("fav",JSON.stringify(fav))}catch(e){}};
const cr=(u,n)=>{n=n||"?";const h=[...n].reduce((a,c)=>a+c.charCodeAt(0),0)%360;return `<span class="cr ph" style="background:hsl(${h} 55% 38%);color:#fff">${E(n.replace(/[^A-Za-zÄÖÜäöüß ]/g,"").split(" ").filter(Boolean).map(w=>w[0]).join("").slice(0,3).toUpperCase()||"?")}</span>`};
const iso=d=>d.toLocaleDateString("sv-SE",{timeZone:"Europe/Berlin"}),k0=iso(new Date());
const dn=k=>k===k0?"Heute":k===iso(new Date(Date.now()+864e5))?"Morgen":new Date(k+"T12:00:00").toLocaleDateString("de-DE",{weekday:"short",day:"2-digit",month:"2-digit"});
const keys=Array.from({length:7},(_,i)=>iso(new Date(Date.now()+i*864e5)));day=k0;
const cnt=k=>ms.filter(x=>x[1].ts.slice(0,10)===k).length;
const wd=k=>k===k0?"Heute":new Date(k+"T12:00:00").toLocaleDateString("de-DE",{weekday:"short"}).replace(".","");
const pos={Goalkeeper:"Torhüter",Defence:"Abwehr",Midfield:"Mittelfeld",Offence:"Angriff"};
const form=id=>{const f=(T[id]||{}).form||[];return f.length?`<div class="fm">${f.map(x=>`<i class="${x.r}" title="${E(x.opp)} ${E(x.sc)}">${x.r}</i>`).join("")}</div>`:'<span class="no">Noch keine Formdaten.</span>'};
const star=(k)=>`<button class="star ${fav.includes(k)?"on":""}" data-f="${k}" aria-label="Favorit">${fav.includes(k)?"★":"☆"}</button>`;
function row([id,m]){const s=m.score?m.score.split(":"):null,t=m.zeit.slice(-5);
return `<a class="mr" href="#m/${id}"><div class="mt">${s?(m.st==="FINISHED"?"FT":"Live"):E(t)}</div>
<div><div class="tn">${cr(m.hc,m.heim)}<span>${E(m.heim)}</span>${s?`<em>${s[0]}</em>`:""}</div><div class="tn">${cr(m.gc,m.gast)}<span>${E(m.gast)}</span>${s?`<em>${s[1]}</em>`:""}</div>
${m.tipp?`<div class="pk">Einschätzung: ${E(m.tipp)}</div>`:""}</div><div>${(D.new||[]).includes(id)?'<span class="nw">NEU</span>':""}</div></a>`}
function list(arr){const g={};arr.forEach(x=>(g[x[1].liga]=g[x[1].liga]||[]).push(x));
return Object.keys(g).map(l=>`<div class="lg">${E(l)}</div><div class="box">${g[l].map(row).join("")}</div>`).join("")||'<p class="no">Keine Spiele.</p>'}
const disc='<footer>Nur ab 18. Keine Garantie, keine Gewinnversprechen, Wetten können zu Verlusten führen. Spielen kann süchtig machen. Hilfe: 0800 1 37 27 00 (BZgA), check-dein-spiel.de. Stand: '+E(D.updated)+'</footer>';
function table(code,hl){const c=C[code];if(!c)return'<p class="no">Keine Tabelle.</p>';
return `<table><tr><th>#</th><th>Team</th><th>Sp</th><th>TD</th><th>Pkt</th></tr>${c.table.map(r=>`<tr class="${hl.includes(r.id)?"me":""}"><td>${r.pos}</td><td><a class="tt" href="#t/${r.id}">${cr(r.c,r.n)}${E(r.n)}</a></td><td>${r.pl}</td><td>${r.gd>0?"+":""}${r.gd}</td><td><b>${r.pts}</b></td></tr>`).join("")}</table>`}
const lg=m=>{const l=m.lage;if(!l)return"";return `<div class="sec"><h3>Aktuelle Lage</h3>${(l.punkte||[]).length?`<ul>${l.punkte.map(x=>`<li>${E(x)}</li>`).join("")}</ul>`:'<p class="no">Keine aktuellen Meldungen gefunden.</p>'}${l.unsicher?'<div class="tip"><b>Vorsicht:</b> Wichtige Ausfälle oder Änderungen gemeldet. Der Tipp ist unsicherer als üblich.</div>':""}${(l.quellen||[]).length?`<p class="no">Quellen: ${l.quellen.map(q=>`<a href="${E(q.u)}" target="_blank" rel="noopener noreferrer">${E(q.t)}</a>`).join(" · ")}</p>`:""}<p class="no">KI-Zusammenfassung von Webquellen, Stand ${E(l.ts.slice(5,16).replace("T"," "))}. Kann Fehler enthalten.</p></div>`};
function vMatch(id){const m=M[id];if(!m)return'<p class="no">Spiel nicht gefunden.</p>';const s=m.score?m.score.split(":"):null;
const tabs=[["u","Übersicht"],["h","Direktvergleich"],["t","Tabelle"],["a","Analyse"]];
let b="";
if(mtab==="u")b=`<div class="sec"><h3>Fakten</h3><ul>${(m.fakten||[]).map(f=>`<li>${E(f)}</li>`).join("")}</ul></div><div class="sec"><h3>Form</h3><p>${E(m.heim)}</p>${form(m.hid)}<p>${E(m.gast)}</p>${form(m.gid)}</div>
<div class="sec"><h3>Info</h3><ul><li>${E(m.liga)}${m.md?", Spieltag "+m.md:""}</li><li>Anstoß: ${E(m.zeit)} Uhr</li>${(T[m.hid]||{}).venue?`<li>Stadion: ${E(T[m.hid].venue)}</li>`:""}</ul></div>`;
if(mtab==="h")b=`<div class="sec"><h3>Letzte Duelle</h3>${(m.h2h||[]).length?`<ul>${m.h2h.map(x=>`<li>${E(x.d)}: ${E(x.h)} ${E(x.s)} ${E(x.a)}</li>`).join("")}</ul>`:'<p class="no">Wird kurz vor dem Spiel geladen.</p>'}</div>`;
if(mtab==="t")b=`<div class="sec">${table(m.code,[m.hid,m.gid])}</div>`;
if(mtab==="a")b=m.analyse?`<div class="sec"><h3>Analyse</h3><p>${E(m.analyse)}</p><div class="pb"><i style="width:${+m.p[0]}%;background:var(--blue)"></i><i style="width:${+m.p[1]}%;background:var(--mute)"></i><i style="width:${+m.p[2]}%;background:var(--y)"></i></div>
<div class="pl"><span>${E(m.heim)} ${+m.p[0]}%</span><span>X ${+m.p[1]}%</span><span>${E(m.gast)} ${+m.p[2]}%</span></div><div class="tip"><b>${E(m.tipp)}</b> (${E(m.note)})<small>Unverbindlich, ohne Gewähr.</small></div>${(m.extra||[]).length?`<h3 style="margin-top:12px">Weitere Einschätzungen</h3><ul>${m.extra.map(x=>`<li>${E(x)}</li>`).join("")}</ul>`:""}<p class="no" style="margin-top:10px">Berechnet aus Tabelle, Torverhältnis und Form (Poisson-Modell), stündlich neu. Keine Garantie.</p></div>`:'<div class="sec"><p class="no">Für dieses Spiel liegen noch keine Daten für eine Berechnung vor.</p></div>';
return `<p><a href="#">‹ Zurück</a></p><div class="hd"><div class="no">${E(m.liga)}</div><div class="row"><a href="#t/${m.hid}">${cr(m.hc,m.heim)}${E(m.heim)}</a><div class="sc">${s?E(m.score):E(m.zeit.slice(-5))}</div><a href="#t/${m.gid}">${cr(m.gc,m.gast)}${E(m.gast)}</a></div></div>
<div class="tb">${tabs.map(t=>`<button data-mt="${t[0]}" aria-pressed="${mtab===t[0]}">${t[1]}</button>`).join("")}</div>${mtab==="a"?lg(m):""}${b}`}
function vTeam(id){const t=T[id];if(!t)return'<p class="no">Team nicht gefunden.</p>';const g={};(t.squad||[]).forEach(p=>(g[p.p||"Sonstige"]=g[p.p||"Sonstige"]||[]).push(p.n));
const own=ms.filter(x=>x[1].hid==id||x[1].gid==id);
return `<p><a href="#">‹ Zurück</a></p><div class="hd"><div class="row"><span></span><a>${cr(t.crest,t.name)}${E(t.name)}</a>${star("t"+id)}</div>${t.coach?`<div class="no">Trainer: ${E(t.coach)}</div>`:""}${t.venue?`<div class="no">Stadion: ${E(t.venue)}</div>`:""}</div>
<div class="sec"><h3>Form (letzte 5)</h3>${form(id)}</div><div class="sec"><h3>Nächste Spiele</h3>${own.length?`<div class="box">${own.map(row).join("")}</div>`:'<p class="no">Keine Spiele in den nächsten Tagen.</p>'}</div>
<div class="sec"><h3>Kader</h3>${Object.keys(g).map(k=>`<p><b>${E(pos[k]||k)}</b></p><ul>${g[k].map(n=>`<li>${E(n)}</li>`).join("")}</ul>`).join("")||'<p class="no">Kein Kader verfügbar.</p>'}</div>`}
const hero=()=>`<div class="hero"><div><h1>Dein Spieltag.<br>Klar analysiert.</h1><p>${cnt(day)} Spiele · ${E(dn(day))} · Tipps stündlich neu</p></div><svg viewBox="0 0 120 100" aria-hidden="true"><rect x="6" y="8" width="108" height="84" rx="10" fill="none" stroke="#fff" stroke-opacity=".4" stroke-width="2"/><path d="M60 8V92" stroke="#fff" stroke-opacity=".4" stroke-width="2"/><circle cx="60" cy="50" r="16" fill="none" stroke="#fff" stroke-opacity=".4" stroke-width="2"/><polyline points="14,76 36,62 56,68 78,38 104,22" fill="none" stroke="#2FD07E" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/><circle cx="104" cy="22" r="6" fill="#2FD07E"/></svg></div>`;
function render(){const h=location.hash.slice(1).split("/"),v=document.getElementById("v"),top=!h[0]||["tab","tor","fav"].includes(h[0]);
document.getElementById("dys").style.display=!h[0]?"grid":"none";
document.getElementById("dys").innerHTML=keys.map(k=>`<button class="dy" data-d="${k}" aria-pressed="${k===day}"><b>${wd(k)}</b><span>${k.slice(8)}.${k.slice(5,7)}.</span>${cnt(k)?"<i></i>":""}</button>`).join("");
document.getElementById("nv").innerHTML=[["","⚽","Spiele"],["tab","📊","Tabellen"],["tor","🥇","Torjäger"],["fav","★","Favoriten"]].map(x=>`<a href="#${x[0]}" class="${(h[0]||"")===x[0]?"on":""}"><span>${x[1]}</span>${x[2]}</a>`).join("");
let b="";
if(h[0]==="m")b=vMatch(h[1]);else if(h[0]==="t")b=vTeam(h[1]);
else if(h[0]==="tab")b=Object.keys(C).map(k=>`<div class="lg">${E(C[k].name)}</div><div class="sec">${table(k,[])}</div>`).join("")||'<p class="no">Noch keine Tabellen.</p>';
else if(h[0]==="tor")b=Object.keys(C).map(k=>`<div class="lg">${E(C[k].name)}</div><div class="sec">${(C[k].scorers||[]).length?`<table><tr><th>#</th><th>Spieler</th><th>Tore</th><th>Vorl.</th></tr>${C[k].scorers.map((s,i)=>`<tr><td>${i+1}</td><td>${E(s.n)} <span class="no">${E(s.t)}</span></td><td><b>${s.g??"-"}</b></td><td>${s.a??"-"}</td></tr>`).join("")}</table>`:'<p class="no">Keine Daten.</p>'}</div>`).join("")||'<p class="no">Noch keine Daten.</p>';
else if(h[0]==="fav"){const f=ms.filter(x=>fav.includes("t"+x[1].hid)||fav.includes("t"+x[1].gid));b=(fav.length?"":'<p class="no">Tippe auf einer Teamseite auf den Stern, um Favoriten zu speichern.</p>')+(f.length?list(f):"")}
else b=hero()+'<div class="w">Nur ab 18. Analysen sind unverbindlich und ohne Gewähr.</div>'+list(ms.filter(x=>x[1].ts.slice(0,10)===day));
v.innerHTML=b+disc;if(h[0]!=="m"||mtab==="u")window.scrollTo(0,0)}
document.addEventListener("click",e=>{const t=e.target.closest("[data-d],[data-mt],[data-f]");if(!t)return;
if(t.dataset.d){day=t.dataset.d}if(t.dataset.mt)mtab=t.dataset.mt;
if(t.dataset.f){const k=t.dataset.f;fav=fav.includes(k)?fav.filter(x=>x!==k):[...fav,k];sv()}render()});
window.addEventListener("hashchange",()=>{mtab="u";render()});render();
</script></body></html>"""

if __name__ == "__main__":
    main()
