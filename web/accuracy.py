"""Server-rendered accuracy pages: /accuracy/ (en) and /tarkkuus/ (fi), plus
one page per city under each.

Why server-side: the claim that matters most - Ilma vs the Finnish
Meteorological Institute and Foreca, verified nightly - used to exist only in
JavaScript, so search and AI crawlers (which do not run scripts) never saw it.
These pages put the same numbers in plain HTML, rendered on every request from
score.py's prospective_results.json, so they are never hard-coded and never
older than the last nightly run.

Claim rules (must match the client-side headline in index.html):
  * Read each pooled 1-7 cell's stored `significant` flag; never recompute it.
  * MAE cells (temperature, wind): Ilma is better when significant and diff < 0.
  * CSI cells (rain, `higher_better`): Ilma is better when significant and diff > 0.
  * The headline names only FMI and Foreca. Every other comparison, including
    the ones Ilma loses (Google on temperature), is shown in the table as is.
  * Per-city numbers are descriptive only; the tested claim is Finland-wide.
"""
import html
import json
import time
from pathlib import Path

BASE = "https://ilma.io"
PRODUCT = "blend_open"
HEADLINE = ["fmi_edited", "foreca"]
TABLE = ["fmi_edited", "foreca", "google_weather", "yr", "ecmwf_aifs025_single"]
VARS = [("t2m", "pairwise_t2m_blends_exploratory", "mae"),
        ("ws", "pairwise_ws_blends_exploratory", "mae"),
        ("rain", "pairwise_rain_blends_exploratory", "csi")]

FI_CITY_KEYS = ["helsinki", "tampere", "oulu", "rovaniemi", "turku", "jyvaskyla", "vaasa", "kuopio",
                "joensuu", "lappeenranta", "pori", "kajaani", "sodankyla", "mariehamn"]

T = {
    "en": dict(
        path="/accuracy/", other="/tarkkuus/", locale="en-GB",
        title="How accurate is Ilma? Verified every night against FMI and Foreca | Ilma",
        desc="Ilma's 1–7 day forecast checked every night against 14 Finnish weather stations, side by side with "
             "the Finnish Meteorological Institute, Foreca, Google and Yr. Matched hours, statistical tests, all numbers.",
        h1="How accurate is Ilma's forecast?",
        lead=lambda days, n, upd: (f"Measured over the last {days} days at 14 Finnish weather stations, for forecasts "
                                   f"1–7 days ahead: {n} matched forecast hours against the Finnish Meteorological "
                                   f"Institute. The table shows how many days each comparison covers. Updated {upd}."),
        names={"blend_open": "Ilma", "fmi_edited": "Finnish Meteorological Institute (FMI)", "foreca": "Foreca",
               "google_weather": "Google", "yr": "Yr", "ecmwf_aifs025_single": "ECMWF AIFS (best single model)",
               "ecmwf_ifs025": "ECMWF IFS", "ecmwf_aifs_ens_mean": "AIFS ensemble", "best_match": "Open-Meteo",
               "metno_nordic": "MET Nordic", "smhi": "SMHI", "dwd": "DWD", "nws": "US National Weather Service"},
        short={"fmi_edited": "FMI's", "foreca": "Foreca's"}, nom={"fmi_edited": "FMI", "foreca": "Foreca"},
        var={"t2m": "Temperature", "ws": "Wind speed", "rain": "Rain or no rain"},
        unit={"t2m": "°C average error", "ws": "m/s average error", "rain": "hit score, CSI"},
        head_mae=lambda var, lst: f"{var}: Ilma's error was lower than " + lst + ".",
        head_csi=lambda lst: "Rain: Ilma caught rain hours better than " + lst + ".",
        pct=lambda name, p: f"{name} (−{p} %)",
        csi=lambda name, a, b: f"{name} (hit score {a} vs {b})",
        sig_note="Each of these differences is statistically significant after correction for multiple comparisons.",
        none="No significant difference against FMI or Foreca yet.",
        th=["Compared with", "Variable", "Ilma", "Them", "Verdict"],
        better="Ilma better", worse="Ilma worse", tie="No significant difference", na="Too little data",
        leads_h="Temperature error by forecast day (°C, lower is better)", day="Day",
        honest="Where Ilma is not better, the table says so.", behind=lambda lst: f"Ilma has been significantly less accurate than: {lst}.", dshort="d",
        method_h="How this is measured",
        method=["Every 5 hours Ilma stores what each service forecast for the next two weeks. After the weather has "
                "happened, each forecast hour is compared with the Finnish Meteorological Institute's measurement at "
                "the same station and hour. Only hours that every compared service forecast count (matched pairs).",
                "Uncertainty comes from a block bootstrap over consecutive days (neighbouring days are not "
                "independent). A competitor is called beaten only when the difference stays significant after Holm "
                "correction for all comparisons made, and only with at least 20 days of data.",
                "Limits: the record is one late summer and autumn. From day 7 onward there is no reliable difference "
                "between the services. Ilma's forecast itself excludes Foreca and Google; they are compared, not used."],
        cities_h="Accuracy by city", city_link=lambda name: f"{name}",
        data_h="Raw data", data="All numbers as JSON",
        home="Forecast", lang_other="Suomeksi",
        city_title=lambda name: f"How accurate is the forecast in {name}? | Ilma",
        city_desc=lambda name: f"Average forecast error in {name}, 1–7 days ahead, for Ilma and every service it is compared with. Updated nightly.",
        city_h1=lambda name: f"Forecast accuracy in {name}",
        city_lead=lambda name, upd: (f"Average temperature and wind error at the {name} weather station, by forecast "
                                     f"day, for every service Ilma is compared with. Updated {upd}."),
        city_note="Per-city numbers are descriptive: the statistical test is made for all stations together.",
        city_back="All of Finland: the tested comparison", city_forecast=lambda name: f"{name} weather",
        ws_h="Wind error by forecast day (m/s, lower is better)", source="Service",
        nodata="No verified data for this city yet.",
    ),
    "fi": dict(
        path="/tarkkuus/", other="/accuracy/", locale="fi-FI",
        title="Kuinka tarkka Ilma on? Verrattu joka yö Ilmatieteen laitokseen ja Forecaan | Ilma",
        desc="Ilman 1–7 päivän ennuste tarkistetaan joka yö 14 suomalaisella sääasemalla rinnakkain Ilmatieteen "
             "laitoksen, Forecan, Googlen ja Yr:n kanssa. Samat tunnit, tilastolliset testit, kaikki luvut.",
        h1="Kuinka tarkka Ilman ennuste on?",
        lead=lambda days, n, upd: (f"Mitattu viimeisten {days} päivän ajalta 14 suomalaisella sääasemalla, ennusteille "
                                   f"1–7 päivää eteenpäin: {n} verrattua ennustetuntia Ilmatieteen laitosta vastaan. "
                                   f"Taulukko näyttää, kuinka monta päivää kukin vertailu kattaa. Päivitetty {upd}."),
        names={"blend_open": "Ilma", "fmi_edited": "Ilmatieteen laitos", "foreca": "Foreca", "google_weather": "Google",
               "yr": "Yr", "ecmwf_aifs025_single": "ECMWF AIFS (paras yksittäinen malli)", "ecmwf_ifs025": "ECMWF IFS",
               "ecmwf_aifs_ens_mean": "AIFS-parvi", "best_match": "Open-Meteo", "metno_nordic": "MET Nordic",
               "smhi": "SMHI", "dwd": "DWD", "nws": "Yhdysvaltain sääpalvelu NWS"},
        short={"fmi_edited": "Ilmatieteen laitoksen", "foreca": "Forecan"}, nom={"fmi_edited": "Ilmatieteen laitos", "foreca": "Foreca"},
        var={"t2m": "Lämpötila", "ws": "Tuulen nopeus", "rain": "Sataako vai ei"},
        unit={"t2m": "keskivirhe °C", "ws": "keskivirhe m/s", "rain": "osuvuus, CSI"},
        head_mae=lambda var, lst: f"{var}: Ilman virhe oli pienempi kuin " + lst + ".",
        head_csi=lambda lst: "Sade: Ilma osui sadetunteihin paremmin kuin " + lst + ".",
        pct=lambda name, p: f"{name} (−{p} %)",
        csi=lambda name, a, b: f"{name} (osuvuus {a} vs {b})",
        sig_note="Jokainen näistä eroista on tilastollisesti merkitsevä monivertailukorjauksen jälkeen.",
        none="Ei vielä merkitsevää eroa Ilmatieteen laitokseen tai Forecaan.",
        th=["Verrokki", "Suure", "Ilma", "Verrokki", "Tulos"],
        better="Ilma parempi", worse="Ilma huonompi", tie="Ei merkitsevää eroa", na="Liian vähän dataa",
        leads_h="Lämpötilan virhe ennustepäivittäin (°C, pienempi on parempi)", day="Päivä",
        honest="Kun Ilma ei ole parempi, taulukko kertoo sen.", behind=lambda lst: f"Ilma on ollut merkitsevästi epätarkempi kuin: {lst}.", dshort="pv",
        method_h="Miten tämä mitataan",
        method=["Ilma tallentaa viiden tunnin välein, mitä kukin palvelu ennustaa kahdeksi viikoksi eteenpäin. Kun sää "
                "on toteutunut, jokaista ennustetuntia verrataan Ilmatieteen laitoksen mittaukseen samalla asemalla "
                "samana tuntina. Mukaan otetaan vain tunnit, jotka kaikki verratut palvelut ennustivat.",
                "Epävarmuus arvioidaan peräkkäisten päivien lohkobootstrapilla (vierekkäiset päivät eivät ole "
                "riippumattomia). Kilpailija lasketaan voitetuksi vain, jos ero on merkitsevä Holmin korjauksen "
                "jälkeen kaikille tehdyille vertailuille, ja vasta kun dataa on vähintään 20 päivältä.",
                "Rajoitukset: aineisto kattaa yhden loppukesän ja syksyn. Seitsemännestä päivästä eteenpäin palveluiden "
                "välillä ei ole luotettavaa eroa. Ilman ennuste ei käytä Forecaa eikä Googlea; niitä verrataan, ei käytetä."],
        cities_h="Tarkkuus kaupungeittain", city_link=lambda name: f"{name}",
        data_h="Raakadata", data="Kaikki luvut JSON-muodossa",
        home="Ennuste", lang_other="In English",
        city_title=lambda name: f"Kuinka tarkka ennuste on: {name} | Ilma",
        city_desc=lambda name: f"Ennusteiden keskivirhe, {name}, 1–7 päivää eteenpäin, Ilmalle ja jokaiselle verratulle palvelulle. Päivittyy joka yö.",
        city_h1=lambda name: f"Ennusteiden tarkkuus: {name}",
        city_lead=lambda name, upd: (f"Lämpötilan ja tuulen keskivirhe havaintoasemalla ({name}) ennustepäivittäin, "
                                     f"kaikille palveluille, joihin Ilmaa verrataan. Päivitetty {upd}."),
        city_note="Kaupunkikohtaiset luvut ovat kuvailevia: tilastollinen testi tehdään kaikille asemille yhdessä.",
        city_back="Koko Suomi: testattu vertailu", city_forecast=lambda name: f"Sää {name}",
        ws_h="Tuulen virhe ennustepäivittäin (m/s, pienempi on parempi)", source="Palvelu",
        nodata="Tälle kaupungille ei ole vielä todennettua dataa.",
    ),
}

CSS = """
@font-face{font-family:'Fraunces';font-style:normal;font-weight:300 900;font-display:swap;src:url(/fonts/fraunces-normal.woff2) format('woff2')}
@font-face{font-family:'Karla';font-style:normal;font-weight:300 700;font-display:swap;src:url(/fonts/karla-normal.woff2) format('woff2')}
:root{--paper:#FAF6EF;--paper-2:#F3EDE3;--ink:#181513;--ink-2:#544A42;--ink-3:#74685E;--rule:#DCD2C3;--good:#2F6B4A;--bad:#A3442A}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:Karla,system-ui,sans-serif;font-size:16px;line-height:1.65}
main{max-width:860px;margin:0 auto;padding:28px 16px 64px}
nav.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:28px;font-size:14px}
nav.top a{color:var(--ink-2)}
.brand{font-family:Fraunces,serif;font-size:22px;color:var(--ink);text-decoration:none}
h1{font-family:Fraunces,serif;font-weight:400;font-size:clamp(28px,4vw,38px);line-height:1.15;margin:0 0 14px}
h2{font-family:Fraunces,serif;font-weight:400;font-size:22px;margin:38px 0 10px}
p{margin:0 0 12px;color:var(--ink-2)}
.claims{list-style:none;padding:0;margin:18px 0 8px}
.claims li{padding:12px 0;border-top:1px solid var(--rule);font-size:17px;color:var(--ink)}
.small{font-size:13.5px;color:var(--ink-3)}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14.5px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:8px 6px;border-bottom:1px solid var(--rule)}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{font-weight:400;font-size:12.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
tr.us td{font-weight:600;color:var(--ink)}
.better{color:var(--good)}.worse{color:var(--bad)}
ul.cities{columns:2 180px;padding:0;list-style:none;line-height:2}
a{color:var(--ink)}
"""


def esc(t):
    return html.escape(str(t), quote=True)


class Results:
    """prospective_results.json, re-read only when the nightly run replaces it."""

    def __init__(self, path: Path):
        self.path, self.mtime, self.data = path, 0.0, None

    def get(self):
        m = self.path.stat().st_mtime
        if m != self.mtime:
            self.data, self.mtime = json.loads(self.path.read_text()), m
        return self.data


def _fmt(v, kind):
    return "—" if v is None else (f"{v:.3f}" if kind == "csi" else f"{v:.2f}")


def verdict(cell, kind):
    """better / worse / tie / na from the stored flag and the sign convention."""
    if not cell or cell.get("significant") is None:
        return "na"
    if not cell["significant"]:
        return "tie"
    good = cell["diff"] > 0 if kind == "csi" else cell["diff"] < 0
    return "better" if good else "worse"


def _cell(d, fam, comp):
    return ((d.get(fam) or {}).get(f"{PRODUCT}__vs__{comp}") or {}).get("pooled_1_7")


def _vals(cell, kind):
    if kind == "csi":
        return cell.get("csi_cand"), cell.get("csi_comp")
    return cell.get("mae_cand"), cell.get("mae_comp")


def _updated(mtime, lang):
    t = time.gmtime(mtime)
    return f"{t.tm_mday}.{t.tm_mon}.{t.tm_year}" if lang == "fi" else time.strftime("%d %b %Y", t)


def _join(items, lang):
    word = "ja" if lang == "fi" else "and"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


def _head(L, lang, title, desc, url, alts, ld):
    alt = "".join(f'<link rel="alternate" hreflang="{l}" href="{u}">' for l, u in alts)
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(title)}</title><meta name=\"description\" content=\"{esc(desc)}\">"
            f'<link rel="canonical" href="{url}">{alt}'
            f'<meta name="robots" content="index,follow,max-image-preview:large">'
            f'<meta property="og:type" content="article"><meta property="og:site_name" content="Ilma">'
            f'<meta property="og:url" content="{url}"><meta property="og:title" content="{esc(title)}">'
            f'<meta property="og:description" content="{esc(desc)}"><meta property="og:image" content="{BASE}/og.jpg">'
            f'<meta name="twitter:card" content="summary_large_image">'
            f'<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
            f'<link rel="preload" href="/fonts/karla-normal.woff2" as="font" type="font/woff2" crossorigin>'
            f"<style>{CSS}</style>"
            f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></head><body><main>')


def _nav(L, lang, other_path):
    home = "/fi/" if lang == "fi" else "/"
    return (f'<nav class="top"><a class="brand" href="{home}">Ilma</a>'
            f'<span><a href="{home}">{L["home"]}</a> · <a href="{other_path}" hreflang="{"en" if lang == "fi" else "fi"}">'
            f'{L["lang_other"]}</a></span></nav>')


def _crumbs(items):
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": BASE + u} for i, (n, u) in enumerate(items)]}


def headline(d, lang):
    """Claim sentences for FMI and Foreca only, one per variable, gated."""
    L = T[lang]
    out = []
    for var, fam, kind in VARS:
        won = []
        for comp in HEADLINE:
            c = _cell(d, fam, comp)
            if verdict(c, kind) == "better":
                won.append((comp, c))
        if not won:
            continue
        if kind == "csi":
            out.append(L["head_csi"](_join([L["csi"](L["nom"][comp], _fmt(c["csi_cand"], "csi"), _fmt(c["csi_comp"], "csi"))
                                            for comp, c in won], lang)))
        else:
            parts = [L["pct"](L["short"][comp], round(abs(c["diff"]) / c["mae_comp"] * 100)) for comp, c in won]
            out.append(L["head_mae"](L["var"][var], _join(parts, lang)))
    return out


def national(d, mtime, lang):
    L = T[lang]
    url = BASE + L["path"]
    ref = _cell(d, "pairwise_t2m_blends_exploratory", "fmi_edited") or {}
    days, n = ref.get("days", 0), ref.get("n", 0)
    upd = _updated(mtime, lang)
    claims = headline(d, lang)
    rows, behind = [], []
    for comp in TABLE:
        for var, fam, kind in VARS:
            c = _cell(d, fam, comp)
            if not c:
                continue
            v = verdict(c, kind)
            a, b = _vals(c, kind)
            rows.append(f'<tr><td>{esc(L["names"][comp])}</td><td>{L["var"][var]} <span class="small">'
                        f'({L["unit"][var]})</span></td><td>{_fmt(a, kind)}</td><td>{_fmt(b, kind)}</td>'
                        f'<td class="{v}">{L[v]} <span class="small">({c["days"]} {L["dshort"]})</span></td></tr>')
            if v == "worse":
                behind.append(f'{L["names"][comp]} ({L["var"][var].lower()})')
    leads = ["1", "2", "3", "5", "7"]
    board = d.get("hourly_t2m", {})
    lead_rows = "".join(
        f'<tr class="{"us" if s == PRODUCT else ""}"><td>{esc(L["names"].get(s, s))}</td><td></td>'
        + "".join(f'<td>{_fmt((board.get(s, {}).get(l) or {}).get("mae"), "mae")}</td>' for l in leads) + "</tr>"
        for s in [PRODUCT] + TABLE if s in board)
    cities = (('<ul class="cities">' + "".join(
        f'<li><a href="{L["path"]}{k}/">{esc(_city_name(k, lang))}</a></li>' for k in FI_CITY_KEYS) + "</ul>")
        if d.get("cities") else "")
    dataset = {"@context": "https://schema.org", "@type": "Dataset", "name": L["title"].split(" | ")[0],
               "description": L["desc"], "url": url, "inLanguage": lang, "isAccessibleForFree": True,
               "creator": {"@type": "Organization", "name": "Ilma", "url": BASE + "/"},
               "dateModified": time.strftime("%Y-%m-%d", time.gmtime(mtime)),
               "spatialCoverage": {"@type": "Place", "name": "Finland"},
               "measurementTechnique": "Matched-pair forecast verification against FMI weather stations, "
                                       "circular block bootstrap, Holm correction",
               "variableMeasured": ["2 m temperature mean absolute error", "10 m wind speed mean absolute error",
                                    "hourly rain occurrence critical success index"],
               "distribution": {"@type": "DataDownload", "encodingFormat": "application/json",
                                "contentUrl": BASE + "/api/stats"}}
    trail = [("Ilma", "/fi/" if lang == "fi" else "/"), (L["h1"], L["path"])]
    alts = [("en", BASE + "/accuracy/"), ("fi", BASE + "/tarkkuus/"), ("x-default", BASE + "/accuracy/")]
    body = (_nav(L, lang, L["other"]) + f'<h1>{esc(L["h1"])}</h1><p>{esc(L["lead"](days, f"{n:,}".replace(",", " "), upd))}</p>'
            + ('<ul class="claims">' + "".join(f"<li>{esc(c)}</li>" for c in claims) + "</ul>"
               f'<p class="small">{esc(L["sig_note"])}</p>' if claims else f"<p>{esc(L['none'])}</p>")
            + f'<div class="tw"><table><thead><tr>{"".join(f"<th>{h}</th>" for h in L["th"])}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div><p class="small">{esc(L["honest"])}'
            + (" " + esc(L["behind"](_join(behind, lang))) if behind else "") + "</p>"
            + f'<h2>{esc(L["leads_h"])}</h2><div class="tw"><table><thead><tr><th>{L["source"]}</th><th></th>'
            + "".join(f'<th>{L["day"]} {l}</th>' for l in leads) + f"</tr></thead><tbody>{lead_rows}</tbody></table></div>"
            + f'<h2>{esc(L["method_h"])}</h2>' + "".join(f"<p>{esc(p)}</p>" for p in L["method"])
            + (f'<h2>{esc(L["cities_h"])}</h2>{cities}' if cities else "")
            + f'<h2>{esc(L["data_h"])}</h2><p><a href="/api/stats">{esc(L["data"])}</a></p>'
            + "</main></body></html>")
    return _head(L, lang, L["title"], L["desc"], url, alts, [dataset, _crumbs(trail)]) + body


_NAMES = None


def _city_name(key, lang):
    global _NAMES
    if _NAMES is None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from seo import CITIES as C
        _NAMES = C
    return _NAMES[key][0] if key in _NAMES else key


def is_city(key):
    _city_name(key, "en")
    return key in _NAMES


def city(d, mtime, lang, key):
    L = T[lang]
    name = _city_name(key, lang)
    path = f'{L["path"]}{key}/'
    url = BASE + path
    fi_city = _NAMES[key][4] == "fi"
    other = f'{L["other"]}{key}/' if fi_city else L["other"]
    c = (d.get("cities") or {}).get(key) or {}
    leads = [str(i) for i in range(1, 8)]

    def table(board):
        srcs = sorted((s for s in board if s == PRODUCT or not s.startswith("blend_")),
                      key=lambda s: (s != PRODUCT, (board[s].get("1") or {}).get("mae", 99)))
        if not srcs:
            return f"<p>{esc(L['nodata'])}</p>"
        return ('<div class="tw"><table><thead><tr><th>' + L["source"] + "</th><th></th>"
                + "".join(f'<th>{L["day"]} {l}</th>' for l in leads) + "</tr></thead><tbody>"
                + "".join(f'<tr class="{"us" if s == PRODUCT else ""}"><td>{esc(L["names"].get(s, s))}</td><td></td>'
                          + "".join(f'<td>{_fmt((board[s].get(l) or {}).get("mae"), "mae")}</td>' for l in leads)
                          + "</tr>" for s in srcs) + "</tbody></table></div>")

    forecast = (f'/saa/{key}/' if lang == "fi" and fi_city else f'/weather/{key}/')
    alts = ([("en", BASE + f"/accuracy/{key}/"), ("fi", BASE + f"/tarkkuus/{key}/")] if fi_city else [])
    alts += [("x-default", BASE + f"/accuracy/{key}/")]
    trail = [("Ilma", "/fi/" if lang == "fi" else "/"), (L["h1"], L["path"]), (L["city_h1"](name), path)]
    page = {"@context": "https://schema.org", "@type": "WebPage", "name": L["city_title"](name), "url": url,
            "inLanguage": lang, "dateModified": time.strftime("%Y-%m-%d", time.gmtime(mtime)),
            "isPartOf": {"@type": "WebSite", "name": "Ilma", "url": BASE + "/"}}
    body = (_nav(L, lang, other) + f'<h1>{esc(L["city_h1"](name))}</h1>'
            f'<p>{esc(L["city_lead"](name, _updated(mtime, lang)))}</p>'
            f'<h2>{esc(L["leads_h"])}</h2>{table(c.get("hourly_t2m") or {})}'
            f'<h2>{esc(L["ws_h"])}</h2>{table(c.get("hourly_ws") or {})}'
            f'<p class="small">{esc(L["city_note"])}</p>'
            f'<p><a href="{L["path"]}">{esc(L["city_back"])}</a> · <a href="{forecast}">{esc(L["city_forecast"](name))}</a></p>'
            "</main></body></html>")
    return _head(L, lang, L["city_title"](name), L["city_desc"](name), url, alts, [page, _crumbs(trail)]) + body
