"""Fussball-Bot: holt Spiele, zeigt Fakten aus der Tabelle, ergänzt KI-Analyse (falls Schlüssel vorhanden), baut docs/index.html, meldet per Telegram."""
import os, json, time, datetime as dt, requests
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

PROMPT = """Du bist ein vorsichtiger Fußball-Analyst. Nutze AUSSCHLIESSLICH die folgenden Daten.
Erfinde keine Verletzungen, Aufstellungen, Nachrichten oder Statistiken, die nicht in den Daten stehen.
Antworte NUR mit JSON, ohne Text davor oder danach, in diesem Format:
{"fakten":["3 kurze Fakten aus den Daten"],"analyse":"3-4 Sätze auf Deutsch","p":[Heimsieg%,Unentschieden%,Auswärtssieg%],"tipp":"kurze unverbindliche Einschätzung","note":"Sicherheit: niedrig/mittel"}
Die drei Zahlen in p sind ganze Zahlen und ergeben zusammen 100. Keine Gewinnversprechen. Wenn die Datenlage dünn ist, sage das in der Analyse.
Daten: """

def analyse(m, home, away):
    data = {"wettbewerb": m["competition"]["name"], "heim": m["homeTeam"]["name"], "gast": m["awayTeam"]["name"],
            "tabelle_heim": home, "tabelle_gast": away}
    msg = client.messages.create(model=MODEL, max_tokens=800,
                                 messages=[{"role": "user", "content": PROMPT + json.dumps(data, ensure_ascii=False)}])
    text = msg.content[0].text.strip().strip("`").removeprefix("json").strip()
    return json.loads(text)

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
    days = int(os.environ.get("DAYS") or 4)
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
            "table": [{"id": r["team"]["id"], "n": r["team"].get("shortName") or r["team"]["name"], "c": r["team"].get("crest"),
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
                t.update({"name": x[side].get("shortName") or x[side]["name"], "crest": x[side].get("crest")})
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
        entry = state["matches"].get(mid, {})
        entry.update({"liga": m["competition"]["name"], "code": m["competition"]["code"], "md": m.get("matchday"),
                      "zeit": ko.strftime("%d.%m. %H:%M"), "ts": ko.isoformat(), "st": m["status"],
                      "heim": m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                      "gast": m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                      "hid": m["homeTeam"]["id"], "gid": m["awayTeam"]["id"],
                      "hc": m["homeTeam"].get("crest"), "gc": m["awayTeam"].get("crest"),
                      "score": f'{ft["home"]}:{ft["away"]}' if ft["home"] is not None else ""})
        for side in ("homeTeam", "awayTeam"):
            tid = str(m[side]["id"])
            t = state["teams"].setdefault(tid, {"name": m[side]["name"], "crest": m[side].get("crest")})
            if "squad" not in t:
                d = safe(f"/teams/{tid}")
                t.update({"venue": d.get("venue"), "coach": (d.get("coach") or {}).get("name"),
                          "squad": [{"n": p["name"], "p": p.get("position")} for p in d.get("squad", [])]})
        if "h2h" not in entry:
            h = safe(f"/matches/{mid}/head2head", limit=5).get("matches", [])
            entry["h2h"] = [{"d": x["utcDate"][:10], "h": x["homeTeam"].get("shortName") or x["homeTeam"]["name"],
                             "a": x["awayTeam"].get("shortName") or x["awayTeam"]["name"],
                             "s": f'{x["score"]["fullTime"]["home"]}:{x["score"]["fullTime"]["away"]}'} for x in h]
        home, away = row(tables.get(entry["code"], []), m["homeTeam"]["id"]), row(tables.get(entry["code"], []), m["awayTeam"]["id"])
        if "fakten" not in entry:
            entry["fakten"] = basic_facts(m, home, away)
        ai_added = False
        if client and "analyse" not in entry:
            try:
                entry.update(analyse(m, home, away))
                ai_added = True
            except Exception as e:
                print("Analyse fehlgeschlagen für", mid, e)
        state["matches"][mid] = entry
        if is_new or ai_added:
            new_ids.append(mid)
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
.top{max-width:640px;margin:0 auto}.logo{font-weight:800;font-size:1.25rem;letter-spacing:-.02em}.logo b{color:var(--blue)}
.days{display:flex;gap:6px;overflow-x:auto;margin-top:10px}
.dy{flex:none;border:0;background:var(--card);color:var(--mute);border-radius:12px;padding:6px 12px;font:600 .85rem inherit;cursor:pointer;text-align:center}
.dy[aria-pressed=true]{background:var(--blue);color:#fff}
main{max-width:640px;margin:0 auto;padding:12px 16px calc(96px + env(safe-area-inset-bottom,0px))}
.w{background:var(--card2);color:var(--mute);border-radius:12px;padding:9px 12px;font-size:.78rem;margin-bottom:12px}
.lg{display:flex;align-items:center;gap:8px;font-weight:700;margin:16px 0 6px;font-size:.85rem;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.box{background:var(--card);border-radius:16px;overflow:hidden}
.mr{display:grid;grid-template-columns:48px 1fr auto;gap:10px;align-items:center;padding:12px 14px;border-top:1px solid var(--line)}
.box>:first-child{border-top:0}
.mt{font-weight:700;font-size:.9rem;text-align:center;color:var(--mute)}
.tn{display:flex;align-items:center;gap:8px;margin:2px 0;font-weight:600}.tn em{margin-left:auto;font-style:normal;font-weight:800}
.cr{width:22px;height:22px;object-fit:contain;flex:none}.ph{background:var(--card2);border-radius:50%;display:grid;place-items:center;font-size:.7rem;font-weight:700}
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
<header><div class="top"><div class="logo">klarvio<b>.</b>fussball</div><div class="days" id="dys"></div></div></header>
<main id="v"></main>
<nav id="nv"></nav>
<script>
const D=__DATA__,E=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const M=D.matches||{},T=D.teams||{},C=D.comps||{};
const ms=Object.entries(M).sort((a,b)=>a[1].ts<b[1].ts?-1:1);
let day=null,fav=[],mtab="u";
try{fav=JSON.parse(localStorage.getItem("fav")||"[]")}catch(e){}
const sv=()=>{try{localStorage.setItem("fav",JSON.stringify(fav))}catch(e){}};
const cr=(u,n)=>u?`<img class="cr" src="${E(u)}" alt="" loading="lazy">`:`<span class="cr ph">${E((n||"?")[0])}</span>`;
const iso=d=>d.toLocaleDateString("sv-SE"),k0=iso(new Date());
const dn=k=>k===k0?"Heute":k===iso(new Date(Date.now()+864e5))?"Morgen":new Date(k+"T12:00:00").toLocaleDateString("de-DE",{weekday:"short",day:"2-digit",month:"2-digit"});
const keys=[...new Set(ms.map(x=>x[1].ts.slice(0,10)))];
day=keys.includes(k0)?k0:keys[0];
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
function vMatch(id){const m=M[id];if(!m)return'<p class="no">Spiel nicht gefunden.</p>';const s=m.score?m.score.split(":"):null;
const tabs=[["u","Übersicht"],["h","Direktvergleich"],["t","Tabelle"],["a","Analyse"]];
let b="";
if(mtab==="u")b=`<div class="sec"><h3>Fakten</h3><ul>${(m.fakten||[]).map(f=>`<li>${E(f)}</li>`).join("")}</ul></div><div class="sec"><h3>Form</h3><p>${E(m.heim)}</p>${form(m.hid)}<p>${E(m.gast)}</p>${form(m.gid)}</div>
<div class="sec"><h3>Info</h3><ul><li>${E(m.liga)}${m.md?", Spieltag "+m.md:""}</li><li>Anstoß: ${E(m.zeit)} Uhr</li>${(T[m.hid]||{}).venue?`<li>Stadion: ${E(T[m.hid].venue)}</li>`:""}</ul></div>`;
if(mtab==="h")b=`<div class="sec"><h3>Letzte Duelle</h3>${(m.h2h||[]).length?`<ul>${m.h2h.map(x=>`<li>${E(x.d)}: ${E(x.h)} ${E(x.s)} ${E(x.a)}</li>`).join("")}</ul>`:'<p class="no">Keine Daten verfügbar.</p>'}</div>`;
if(mtab==="t")b=`<div class="sec">${table(m.code,[m.hid,m.gid])}</div>`;
if(mtab==="a")b=m.analyse?`<div class="sec"><h3>Analyse</h3><p>${E(m.analyse)}</p><div class="pb"><i style="width:${+m.p[0]}%;background:var(--blue)"></i><i style="width:${+m.p[1]}%;background:var(--mute)"></i><i style="width:${+m.p[2]}%;background:var(--y)"></i></div>
<div class="pl"><span>${E(m.heim)} ${+m.p[0]}%</span><span>X ${+m.p[1]}%</span><span>${E(m.gast)} ${+m.p[2]}%</span></div><div class="tip"><b>${E(m.tipp)}</b> (${E(m.note)})<small>Unverbindlich, ohne Gewähr.</small></div></div>`:'<div class="sec"><p class="no">Die KI-Analyse ist noch nicht aktiviert.</p></div>';
return `<p><a href="#">‹ Zurück</a></p><div class="hd"><div class="no">${E(m.liga)}</div><div class="row"><a href="#t/${m.hid}">${cr(m.hc,m.heim)}${E(m.heim)}</a><div class="sc">${s?E(m.score):E(m.zeit.slice(-5))}</div><a href="#t/${m.gid}">${cr(m.gc,m.gast)}${E(m.gast)}</a></div></div>
<div class="tb">${tabs.map(t=>`<button data-mt="${t[0]}" aria-pressed="${mtab===t[0]}">${t[1]}</button>`).join("")}</div>${b}`}
function vTeam(id){const t=T[id];if(!t)return'<p class="no">Team nicht gefunden.</p>';const g={};(t.squad||[]).forEach(p=>(g[p.p||"Sonstige"]=g[p.p||"Sonstige"]||[]).push(p.n));
const own=ms.filter(x=>x[1].hid==id||x[1].gid==id);
return `<p><a href="#">‹ Zurück</a></p><div class="hd"><div class="row"><span></span><a>${cr(t.crest,t.name)}${E(t.name)}</a>${star("t"+id)}</div>${t.coach?`<div class="no">Trainer: ${E(t.coach)}</div>`:""}${t.venue?`<div class="no">Stadion: ${E(t.venue)}</div>`:""}</div>
<div class="sec"><h3>Form (letzte 5)</h3>${form(id)}</div><div class="sec"><h3>Nächste Spiele</h3>${own.length?`<div class="box">${own.map(row).join("")}</div>`:'<p class="no">Keine Spiele in den nächsten Tagen.</p>'}</div>
<div class="sec"><h3>Kader</h3>${Object.keys(g).map(k=>`<p><b>${E(pos[k]||k)}</b></p><ul>${g[k].map(n=>`<li>${E(n)}</li>`).join("")}</ul>`).join("")||'<p class="no">Kein Kader verfügbar.</p>'}</div>`}
function render(){const h=location.hash.slice(1).split("/"),v=document.getElementById("v"),top=!h[0]||["tab","tor","fav"].includes(h[0]);
document.getElementById("dys").style.display=!h[0]?"flex":"none";
document.getElementById("dys").innerHTML=keys.map(k=>`<button class="dy" data-d="${k}" aria-pressed="${k===day}">${dn(k)}</button>`).join("");
document.getElementById("nv").innerHTML=[["","⚽","Spiele"],["tab","📊","Tabellen"],["tor","🥇","Torjäger"],["fav","★","Favoriten"]].map(x=>`<a href="#${x[0]}" class="${(h[0]||"")===x[0]?"on":""}"><span>${x[1]}</span>${x[2]}</a>`).join("");
let b="";
if(h[0]==="m")b=vMatch(h[1]);else if(h[0]==="t")b=vTeam(h[1]);
else if(h[0]==="tab")b=Object.keys(C).map(k=>`<div class="lg">${E(C[k].name)}</div><div class="sec">${table(k,[])}</div>`).join("")||'<p class="no">Noch keine Tabellen.</p>';
else if(h[0]==="tor")b=Object.keys(C).map(k=>`<div class="lg">${E(C[k].name)}</div><div class="sec">${(C[k].scorers||[]).length?`<table><tr><th>#</th><th>Spieler</th><th>Tore</th><th>Vorl.</th></tr>${C[k].scorers.map((s,i)=>`<tr><td>${i+1}</td><td>${E(s.n)} <span class="no">${E(s.t)}</span></td><td><b>${s.g??"-"}</b></td><td>${s.a??"-"}</td></tr>`).join("")}</table>`:'<p class="no">Keine Daten.</p>'}</div>`).join("")||'<p class="no">Noch keine Daten.</p>';
else if(h[0]==="fav"){const f=ms.filter(x=>fav.includes("t"+x[1].hid)||fav.includes("t"+x[1].gid));b=(fav.length?"":'<p class="no">Tippe auf einer Teamseite auf den Stern, um Favoriten zu speichern.</p>')+(f.length?list(f):"")}
else b='<div class="w">Nur ab 18. Analysen sind unverbindlich und ohne Gewähr.</div>'+list(ms.filter(x=>x[1].ts.slice(0,10)===day));
v.innerHTML=b+disc;if(h[0]!=="m"||mtab==="u")window.scrollTo(0,0)}
document.addEventListener("click",e=>{const t=e.target.closest("[data-d],[data-mt],[data-f]");if(!t)return;
if(t.dataset.d){day=t.dataset.d}if(t.dataset.mt)mtab=t.dataset.mt;
if(t.dataset.f){const k=t.dataset.f;fav=fav.includes(k)?fav.filter(x=>x!==k):[...fav,k];sv()}render()});
window.addEventListener("hashchange",()=>{mtab="u";render()});render();
</script></body></html>"""

if __name__ == "__main__":
    main()
