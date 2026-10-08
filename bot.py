"""Fussball-Analyse-Bot: holt Spiele, lässt Claude analysieren, baut docs/index.html, meldet per Telegram."""
import os, json, datetime as dt, requests, anthropic
from zoneinfo import ZoneInfo

FD = os.environ["FOOTBALL_DATA_TOKEN"]
COMPS = os.environ.get("COMPETITIONS", "BL1")          # z. B. BL1,PL,PD (Bundesliga, Premier League, La Liga)
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
SITE = os.environ.get("SITE_URL", "")
TG_TOKEN, TG_CHAT = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
TZ = ZoneInfo("Europe/Berlin")
client = anthropic.Anthropic()

def fd(path, **params):
    r = requests.get("https://api.football-data.org/v4" + path, params=params,
                     headers={"X-Auth-Token": FD}, timeout=30)
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

def main():
    state = load_state()
    today = dt.date.today()
    matches = fd("/matches", competitions=COMPS, dateFrom=today.isoformat(),
                 dateTo=(today + dt.timedelta(days=1)).isoformat()).get("matches", [])
    tables, new_ids = {}, []
    for m in matches:
        mid = str(m["id"])
        ko = dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(TZ)
        ft = m["score"]["fullTime"]
        entry = state["matches"].get(mid, {})
        entry.update({"liga": m["competition"]["name"], "zeit": ko.strftime("%d.%m. %H:%M"), "ts": ko.isoformat(),
                      "heim": m["homeTeam"]["name"], "gast": m["awayTeam"]["name"],
                      "score": f'{ft["home"]}:{ft["away"]}' if ft["home"] is not None else ""})
        if "analyse" not in entry:
            code = m["competition"]["code"]
            if code not in tables:
                tables[code] = fd(f"/competitions/{code}/standings")["standings"][0]["table"]
            try:
                entry.update(analyse(m, row(tables[code], m["homeTeam"]["id"]), row(tables[code], m["awayTeam"]["id"])))
                new_ids.append(mid)
            except Exception as e:
                print("Analyse fehlgeschlagen für", mid, e)
        state["matches"][mid] = entry
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=2)).isoformat()
    state["matches"] = {k: v for k, v in state["matches"].items() if v.get("ts", "") > cutoff}
    state["updated"] = dt.datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    state["new"] = new_ids
    json.dump(state, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.makedirs("docs", exist_ok=True)
    data = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    open("docs/index.html", "w", encoding="utf-8").write(HTML.replace("__DATA__", data))
    if new_ids and TG_TOKEN and TG_CHAT:
        text = f"⚽ {len(new_ids)} neue Analyse(n) verfügbar.\n{SITE}\n\nUnverbindlich, keine Garantie. Nur ab 18."
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": TG_CHAT, "text": text}, timeout=30)

HTML = """<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Anstoß Analyse</title>
<style>
:root{--bg:#F5F3FA;--card:#fff;--ink:#231942;--mute:#645C7D;--line:#DDD8EA;--teal:#0F8B8D;--soft:#E3F3F3;--warn:#FFE9A8;--wi:#4A3A00}
@media(prefers-color-scheme:dark){:root{--bg:#14102A;--card:#1E1940;--ink:#F0EDFA;--mute:#A9A2C4;--line:#322B5C;--teal:#4FD1D3;--soft:#1C3A4A;--warn:#4A3A00;--wi:#FFE9A8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:20px 16px 48px}h1{font:700 2.2rem/1.1 Georgia,serif;margin:6px 0}
.w{background:var(--warn);color:var(--wi);border-radius:10px;padding:10px 12px;font-size:.86rem;margin:10px 0}
.s{color:var(--mute);font-size:.88rem;margin:10px 0}.l{display:grid;gap:10px}
details{background:var(--card);border:1px solid var(--line);border-radius:14px}
summary{list-style:none;cursor:pointer;padding:14px;display:grid;gap:4px}summary::-webkit-details-marker{display:none}
.m{display:flex;justify-content:space-between;font-size:.82rem;color:var(--mute)}.n{background:var(--teal);color:var(--bg);border-radius:6px;padding:0 8px;font-weight:600}
.t{font:700 1.15rem/1.25 Georgia,serif}.k{font-size:.9rem;color:var(--teal);font-weight:600}
.d{padding:0 14px 16px;border-top:1px solid var(--line)}.d h3{font-size:1rem;margin:14px 0 6px}.d ul{margin:0;padding-left:20px}
.b{display:grid;grid-template-columns:90px 1fr 40px;gap:8px;align-items:center;font-size:.85rem;margin-top:6px}
.tr{height:10px;background:var(--soft);border-radius:6px;overflow:hidden}.f{height:100%;background:var(--teal)}
.tip{background:var(--soft);border-radius:10px;padding:10px 12px;margin-top:14px}.tip small{display:block;color:var(--mute)}
footer{margin-top:24px;font-size:.8rem;color:var(--mute)}
</style></head><body><main>
<h1>Anstoß Analyse</h1>
<div class="w">Nur ab 18. Unverbindliche Analysen einer KI auf Basis von Tabellendaten. Keine Garantie, Wetten können zu Verlusten führen.</div>
<div class="s" id="u"></div><div class="l" id="l"></div>
<footer>Keine Gewinnversprechen. Spielen kann süchtig machen. Hilfe: 0800 1 37 27 00 (BZgA), check-dein-spiel.de.</footer></main>
<script>
const D=__DATA__;
const E=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const L=["Heimsieg","Unentschieden","Auswärtssieg"];
document.getElementById("u").textContent="Zuletzt aktualisiert: "+D.updated+" Uhr. Neue Analyse jede Stunde.";
const items=Object.entries(D.matches).sort((a,b)=>a[1].ts<b[1].ts?-1:1).filter(x=>x[1].analyse);
document.getElementById("l").innerHTML=items.length?items.map(([id,m])=>`<details><summary>
<div class="m"><span>${E(m.liga)} · ${E(m.zeit)} Uhr${m.score?" · Ergebnis "+E(m.score):""}</span>${(D.new||[]).includes(id)?'<span class="n">Neu</span>':""}</div>
<div class="t">${E(m.heim)} – ${E(m.gast)}</div><div class="k">Unverbindliche Einschätzung: ${E(m.tipp)}</div></summary>
<div class="d"><h3>Fakten</h3><ul>${m.fakten.map(f=>`<li>${E(f)}</li>`).join("")}</ul>
<h3>Analyse</h3><p>${E(m.analyse)}</p><h3>Modellschätzung</h3>
${m.p.map((v,i)=>`<div class="b"><span>${L[i]}</span><div class="tr"><div class="f" style="width:${+v}%"></div></div><span>${+v} %</span></div>`).join("")}
<div class="tip"><b>${E(m.tipp)}</b> (${E(m.note)})<small>Unverbindlich, ohne Gewähr. Kein Ergebnis ist sicher.</small></div></div></details>`).join("")
:'<p class="s">Heute keine Spiele in den ausgewählten Ligen.</p>';
</script></body></html>"""

if __name__ == "__main__":
    main()
