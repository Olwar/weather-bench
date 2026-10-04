"""Generate the SEO surface of ilma.io from web/static/index.html.

The app is one HTML file that renders everything from the API, so crawlers
saw a title and nothing else. This script writes:

  web/static/saa/<city>/index.html      Finnish landing page, Finnish cities
  web/static/weather/<city>/index.html  English landing page, every city
  web/static/fi/index.html              Finnish home page
  web/static/saa/index.html, weather/index.html   city hubs (fi / en)
  web/static/sitemap.xml, robots.txt, llms.txt
  and refreshes index.html itself: English head + about block, the city link
  list (between <!--cities--> markers) and the static UI strings.

Static UI strings (labels, placeholders, "loading…") are taken from the page's
own I18N table, so a crawler that does not run JavaScript sees the same words a
visitor sees, in the page's language - never stale English on a Finnish page.

Each landing page IS the app (same markup and script) with a different head,
`data-*` attributes on <html> that make the bootstrap load that city in that
language, and a static, visible text block so the page has content before any
script runs. Run after every index.html change:  python3 web/seo.py
Generated pages are gitignored; deploy/site.sh runs this before deploying.
"""
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent / "static"
SRC = ROOT / "index.html"
BASE = "https://ilma.io"

# key -> (name, genitive (fi), region/country fi, region/country en, country code)
CITIES = {
    "helsinki":     ("Helsinki", "Helsingin", "Uusimaa", "Uusimaa, Finland", "fi"),
    "tampere":      ("Tampere", "Tampereen", "Pirkanmaa", "Pirkanmaa, Finland", "fi"),
    "oulu":         ("Oulu", "Oulun", "Pohjois-Pohjanmaa", "North Ostrobothnia, Finland", "fi"),
    "rovaniemi":    ("Rovaniemi", "Rovaniemen", "Lappi", "Lapland, Finland", "fi"),
    "turku":        ("Turku", "Turun", "Varsinais-Suomi", "Southwest Finland", "fi"),
    "jyvaskyla":    ("Jyväskylä", "Jyväskylän", "Keski-Suomi", "Central Finland", "fi"),
    "vaasa":        ("Vaasa", "Vaasan", "Pohjanmaa", "Ostrobothnia, Finland", "fi"),
    "kuopio":       ("Kuopio", "Kuopion", "Pohjois-Savo", "North Savo, Finland", "fi"),
    "joensuu":      ("Joensuu", "Joensuun", "Pohjois-Karjala", "North Karelia, Finland", "fi"),
    "lappeenranta": ("Lappeenranta", "Lappeenrannan", "Etelä-Karjala", "South Karelia, Finland", "fi"),
    "pori":         ("Pori", "Porin", "Satakunta", "Satakunta, Finland", "fi"),
    "kajaani":      ("Kajaani", "Kajaanin", "Kainuu", "Kainuu, Finland", "fi"),
    "sodankyla":    ("Sodankylä", "Sodankylän", "Lappi", "Lapland, Finland", "fi"),
    "mariehamn":    ("Maarianhamina", "Maarianhaminan", "Ahvenanmaa", "Åland, Finland", "fi"),
    "stockholm":    ("Stockholm", "Tukholman", "Ruotsi", "Sweden", "se"),
    "goteborg":     ("Göteborg", "Göteborgin", "Ruotsi", "Sweden", "se"),
    "malmo":        ("Malmö", "Malmön", "Ruotsi", "Sweden", "se"),
    "lulea":        ("Luleå", "Luulajan", "Ruotsi", "Sweden", "se"),
    "kobenhavn":    ("Copenhagen", "Kööpenhaminan", "Tanska", "Denmark", "dk"),
    "aarhus":       ("Aarhus", "Aarhusin", "Tanska", "Denmark", "dk"),
    "aalborg":      ("Aalborg", "Aalborgin", "Tanska", "Denmark", "dk"),
    "berlin":       ("Berlin", "Berliinin", "Saksa", "Germany", "de"),
    "hamburg":      ("Hamburg", "Hampurin", "Saksa", "Germany", "de"),
    "munchen":      ("Munich", "Münchenin", "Saksa", "Germany", "de"),
    "frankfurt":    ("Frankfurt", "Frankfurtin", "Saksa", "Germany", "de"),
    "koln":         ("Cologne", "Kölnin", "Saksa", "Germany", "de"),
    "newyork":      ("New York", "New Yorkin", "Yhdysvallat", "United States", "us"),
    "chicago":      ("Chicago", "Chicagon", "Yhdysvallat", "United States", "us"),
    "houston":      ("Houston", "Houstonin", "Yhdysvallat", "United States", "us"),
    "denver":       ("Denver", "Denverin", "Yhdysvallat", "United States", "us"),
    "seattle":      ("Seattle", "Seattlen", "Yhdysvallat", "United States", "us"),
    "miami":        ("Miami", "Miamin", "Yhdysvallat", "United States", "us"),
}


def registry():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from common import CITIES as C
    return {c["key"]: c for c in C}


# Competitors actually collected per country (collect.py) - stated on the
# city pages as plain facts; the numbers live on the accuracy pages.
COMPETITORS = {
    "fi": (["Ilmatieteen laitos", "Foreca", "Google", "Yr"],
           ["the Finnish Meteorological Institute", "Foreca", "Google", "Yr"]),
    "se": (["SMHI", "Yr", "Google"], ["SMHI", "Yr", "Google"]),
    "dk": (["Yr", "Google"], ["Yr", "Google"]),
    "de": (["DWD", "Yr", "Google"], ["DWD", "Yr", "Google"]),
    "us": (["Yhdysvaltain sääpalvelu NWS", "Yr", "Google"], ["the US National Weather Service", "Yr", "Google"]),
}


def join(items, word):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


# element id -> I18N key for every static string in the markup
STATIC_IDS = {"radarLabel": "radarLabel", "radarAttr": "radarAttr", "chartLabel": "chartLabel",
              "twoWeeks": "twoWeeks", "trusthead": "trustHead", "trustmeta": "trustMeta", "footerTxt": "footer"}


def i18n(src):
    """{lang: {key: string}} for the plain string entries of the page's I18N table."""
    out = {}
    for lang, a, b in (("en", "\nen:{", "\nfi:{"), ("fi", "\nfi:{", "\n}};")):
        i = src.index(a)
        blk = src[i:src.index(b, i + 1)]
        out[lang] = {k: v for k, v in re.findall(r'(?:^|[\s,{])([A-Za-z]\w*):"((?:[^"\\]|\\.)*)"', blk)}
    return out


def localize(html, T):
    """Static UI strings in the page's language, straight from I18N."""
    def text(v):
        return v.replace("&", "&amp;").replace("<", "&lt;")
    for el, key in STATIC_IDS.items():
        html, n = re.subn(rf'(id="{el}"[^>]*>)[^<]*(<)', lambda m: m.group(1) + text(T[key]) + m.group(2), html, count=1)
        assert n == 1, el
    for pat, key in ((r'(id="q"[^>]*placeholder=")[^"]*(")', "searchPh"),
                     (r'(id="radarPlay" aria-label=")[^"]*(")', "radarPlay")):
        html, n = re.subn(pat, lambda m: m.group(1) + esc(T[key]) + m.group(2), html, count=1)
        assert n == 1, key
    return re.sub(r'(class="load">)[^<]*(<)', lambda m: m.group(1) + text(T["loading"]) + m.group(2), html)


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def head_block(lang, title, desc, url, alternates, jsonld):
    alt = "".join(f'<link rel="alternate" hreflang="{l}" href="{u}">\n' for l, u in alternates)
    return (f"<title>{esc(title)}</title>\n"
            f'<meta name="description" content="{esc(desc)}">\n'
            f'<link rel="canonical" href="{url}">\n{alt}'
            f'<meta name="robots" content="index,follow,max-image-preview:large">\n'
            f'<meta name="theme-color" content="#FAF6EF">\n'
            f'<meta property="og:type" content="website">\n'
            f'<meta property="og:site_name" content="Ilma">\n'
            f'<meta property="og:locale" content="{"fi_FI" if lang == "fi" else "en_GB"}">\n'
            f'<meta property="og:url" content="{url}">\n'
            f'<meta property="og:title" content="{esc(title)}">\n'
            f'<meta property="og:description" content="{esc(desc)}">\n'
            f'<meta property="og:image" content="{BASE}/og.jpg">\n'
            f'<meta property="og:image:type" content="image/jpeg">\n'
            f'<meta property="og:image:width" content="1200">\n'
            f'<meta property="og:image:height" content="630">\n'
            f'<meta name="twitter:card" content="summary_large_image">\n'
            f'<meta name="twitter:title" content="{esc(title)}">\n'
            f'<meta name="twitter:description" content="{esc(desc)}">\n'
            f'<meta name="twitter:image" content="{BASE}/og.jpg">\n'
            f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>\n')


FAQ_FI = [
    ("Mihin Ilman ennuste perustuu?",
     "Ilma laskee keskiarvon kuudesta ennustemallista: ECMWF AIFS ja sen parvi, ECMWF IFS, MET Nordic, Ilmatieteen laitoksen toimitettu ennuste ja Open-Meteo. Varjostettu alue kaaviossa näyttää, kuinka paljon mallit ovat eri mieltä."),
    ("Kuinka tarkka ennuste on?",
     "Tarkkuus todennetaan joka yö 14 suomalaisella sääasemalla, ja tulokset näkyvät sivulla. Sivu väittää olevansa tarkempi kuin kilpailija vain, kun ero on tilastollisesti merkitsevä."),
    ("Onko Ilmassa sadetutka?",
     "On, Suomessa. Kartta näyttää Ilmatieteen laitoksen uusimman tutkakuvan ja Ilman arvion siitä, minne sade liikkuu seuraavan tunnin aikana, viiden minuutin välein. Jokaisessa tulevassa kuvassa näkyy, kuinka usein arvio on viime viikon aikana osunut oikeaan."),
]
FAQ_EN = [
    ("What is the forecast based on?",
     "Ilma averages six forecast models: ECMWF AIFS and its ensemble, ECMWF IFS, MET Nordic, the Finnish Meteorological Institute's edited forecast and Open-Meteo. The shaded band on the chart shows how much the models disagree."),
    ("How accurate is it?",
     "Accuracy is verified every night against 14 Finnish weather stations, and the results are shown on the page. The site claims to beat a competitor only when the difference is statistically significant."),
    ("Does Ilma have a rain radar?",
     "Yes, for Finland. The map shows the newest radar scan from the Finnish Meteorological Institute and Ilma's estimate of where the rain moves in the next hour, in 5-minute steps. Each future frame shows how often that estimate has been right over the last week."),
]


def about_html(lang, h1, intro, faq, extra=""):
    q = "".join(f"<details><summary>{esc(a)}</summary><p>{esc(b)}</p></details>" for a, b in faq)
    return (f'<section class="about" id="about"><h1>{esc(h1)}</h1>'
            + "".join(f"<p>{esc(p)}</p>" for p in intro) + extra
            + f'<div class="faq">{q}</div></section>')


def crumbs(items):
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": BASE + u} for i, (n, u) in enumerate(items)]}


ORG = {"@context": "https://schema.org", "@type": "Organization", "name": "Ilma", "url": BASE + "/",
       "logo": BASE + "/logo.png"}

HOME = {
    "en": dict(path="/", title="Ilma – a weather forecast that shows how much to trust it",
               desc="Hourly forecasts for any place, 7 days ahead, and a rain radar for Finland. Ilma blends six "
                    "weather models, shows how much they disagree, and verifies its accuracy against real stations every night.",
               h1="Ilma – a weather forecast that shows how much to trust it",
               intro=["Ilma is a weather forecast for any place on earth, seven days ahead, hour by hour. It averages six "
                      "forecast models and shows the band the models fall in: a narrow band means a confident forecast, a "
                      "wide one means the models disagree and you should plan for both outcomes.",
                      "The forecast's accuracy is verified every night against 14 Finnish weather stations, and the results "
                      "are published on this page. The site claims to be better than a named competitor only when the "
                      "difference is statistically significant."]),
    "fi": dict(path="/fi/", title="Ilma – sääennuste, joka kertoo kuinka paljon siihen voi luottaa",
               desc="Tunneittainen sääennuste mihin tahansa paikkaan 7 päivää eteenpäin ja sadetutka Suomeen. Ilma "
                    "yhdistää kuusi säämallia, näyttää kuinka paljon ne ovat eri mieltä ja todentaa tarkkuutensa joka yö "
                    "oikeilla sääasemilla.",
               h1="Ilma – sääennuste, joka kertoo kuinka paljon siihen voi luottaa",
               intro=["Ilma on sääennuste mihin tahansa paikkaan maailmassa, seitsemän päivää eteenpäin, tunti tunnilta. "
                      "Se laskee keskiarvon kuudesta ennustemallista ja näyttää haarukan, jonka sisällä mallit ovat: kapea "
                      "haarukka tarkoittaa varmaa ennustetta, leveä sitä, että mallit ovat eri mieltä ja kannattaa varautua "
                      "molempiin.",
                      "Ennusteen tarkkuus todennetaan joka yö 14 suomalaisella sääasemalla, ja tulokset julkaistaan tällä "
                      "sivulla. Sivu väittää olevansa tarkempi kuin nimetty kilpailija vain, kun ero on tilastollisesti "
                      "merkitsevä."]),
}


def faq_ld(faq):
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": a, "acceptedAnswer": {"@type": "Answer", "text": b}} for a, b in faq]}


def city_links():
    fi = [k for k, v in CITIES.items() if v[4] == "fi"]
    rest = [k for k, v in CITIES.items() if v[4] != "fi"]
    a = "".join(f'<a href="/saa/{k}/">Sää {CITIES[k][0]}</a>' for k in fi)
    b = "".join(f'<a href="/weather/{k}/">{CITIES[k][0]} weather</a>' for k in rest)
    return (f'<nav class="cities" aria-label="Cities"><div class="label">Sää Suomessa · Weather elsewhere</div>'
            f'<div class="row">{a}</div><div class="row">{b}</div></nav>')


def write(path, html):
    out = ROOT / path.strip("/") / "index.html" if path != "/" else SRC
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)


def page(src, T, lang, attrs, head, about, radar=True):
    html = src.replace('<html lang="en">', f'<html lang="{lang}"{attrs}>', 1)
    if not radar:   # Finland-only feature: absent from foreign city pages until a Finnish place is chosen
        html = html.replace('<section class="radar" id="radarSec">', '<section class="radar" id="radarSec" hidden>', 1)
    html = re.sub(r"<!--seo-->.*?<!--/seo-->", lambda m: "<!--seo-->\n" + head + "<!--/seo-->", html, flags=re.S)
    html = re.sub(r"<!--about-->.*?<!--/about-->", lambda m: "<!--about-->" + about + "<!--/about-->", html, flags=re.S)
    return localize(html, T[lang])


def main():
    src = SRC.read_text()
    src = re.sub(r"<!--cities-->.*?<!--/cities-->", f"<!--cities-->{city_links()}<!--/cities-->", src, flags=re.S)
    T = i18n(src)
    today = date.today().isoformat()
    reg = registry()
    urls = []

    # 1. home pages: "/" (en, x-default) and "/fi/"
    home_alts = [("en", BASE + "/"), ("fi", BASE + "/fi/"), ("x-default", BASE + "/")]
    for lang in ("en", "fi"):
        h = HOME[lang]
        faq = FAQ_FI if lang == "fi" else FAQ_EN
        ld = [{"@context": "https://schema.org", "@type": "WebSite", "name": "Ilma", "url": BASE + "/",
               "inLanguage": ["en", "fi"], "description": h["desc"]},
              ORG,
              {"@context": "https://schema.org", "@type": "WebApplication", "name": "Ilma", "url": BASE + h["path"],
               "applicationCategory": "WeatherApplication", "operatingSystem": "Any",
               "browserRequirements": "Requires JavaScript", "inLanguage": lang,
               "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"},
               "featureList": ["Six-model blended forecast", "Model disagreement band",
                               "Rain radar and one-hour nowcast for Finland",
                               "Nightly verification against weather stations"]},
              faq_ld(faq)]
        head = head_block(lang, h["title"], h["desc"], BASE + h["path"], home_alts, ld)
        about = about_html(lang, h["h1"], h["intro"], faq)
        attrs = "" if lang == "en" else ' data-lang="fi"'
        html = page(src, T, lang, attrs, head, about)
        if lang == "en":
            src = html            # index.html is also the template for every other page
        write(h["path"], html)
        urls.append(BASE + h["path"])

    # 2. city pages
    for key, (name, gen, reg_fi, reg_en, cc) in CITIES.items():
        c = reg[key]
        lat, lon = c["lat"], c["lon"]
        pages = [("en", f"/weather/{key}/")] + ([("fi", f"/saa/{key}/")] if cc == "fi" else [])
        alts = [(l, BASE + p) for l, p in pages] + [("x-default", BASE + f"/weather/{key}/")]
        comp_fi, comp_en = COMPETITORS[cc]
        for lang, path in pages:
            url = BASE + path
            if lang == "fi":
                title = f"Sää {name} – 7 päivän ennuste, jonka tarkkuus on todennettu | Ilma"
                desc = (f"{gen} sää tunneittain 7 päivää eteenpäin. Ilma yhdistää kuusi säämallia ja näyttää, "
                        f"kuinka paljon ne ovat eri mieltä. Tarkkuus todennetaan joka yö oikeilla sääasemilla.")
                h1 = f"Sää {name}"
                intro = [f"{gen} sää seuraaville seitsemälle päivälle, tunti tunnilta. Ilma laskee keskiarvon kuudesta "
                         f"ennustemallista ja näyttää haarukan, jonka sisällä mallit ovat: kapea haarukka tarkoittaa "
                         f"varmaa ennustetta, leveä epävarmaa.",
                         f"Ennusteen tarkkuus mitataan joka yö Ilmatieteen laitoksen havaintoasemalla ({name}). "
                         f"Ilman ennustetta verrataan samoina tunteina näiden ennusteisiin: {join(comp_fi, 'ja')}. "
                         f"Sadetutka ja seuraavan tunnin sadearvio näkyvät kartalla."]
                faq = FAQ_FI
                trail = [("Ilma", "/fi/"), ("Sää Suomessa", "/saa/"), (f"Sää {name}", path)]
            else:
                title = f"{name} weather – 7-day forecast with verified accuracy | Ilma"
                desc = (f"Hourly weather for {name}, {reg_en}, 7 days ahead. Ilma blends six forecast models, shows "
                        f"how much they disagree, and verifies its accuracy against real weather stations every night.")
                h1 = f"{name} weather"
                truth = (f"the Finnish Meteorological Institute's weather station in {name}" if cc == "fi"
                         else f"the airport weather station {c['metar']}")
                intro = [f"The weather in {name}, {reg_en}, for the next seven days, hour by hour. Ilma averages six "
                         f"forecast models and shows the band the models fall in: a narrow band means a confident "
                         f"forecast, a wide one means uncertainty.",
                         f"Every night the forecast is checked against {truth}, hour by hour, side by side with the "
                         f"forecasts of {join(comp_en, 'and')}."
                         + (" The map shows the rain radar and Ilma's estimate for the next hour." if cc == "fi" else "")]
                faq = FAQ_EN
                trail = [("Ilma", "/"), ("Weather by city", "/weather/"), (f"{name} weather", path)]
            ld = [{"@context": "https://schema.org", "@type": "WebPage", "name": title, "url": url,
                   "inLanguage": lang, "isPartOf": {"@type": "WebSite", "name": "Ilma", "url": BASE + "/"},
                   "about": {"@type": "Place", "name": name,
                             "geo": {"@type": "GeoCoordinates", "latitude": lat, "longitude": lon}}},
                  crumbs(trail), faq_ld(faq)]
            attrs = (f' data-lang="{lang}" data-city="{key}" data-lat="{lat}" data-lon="{lon}" '
                     f'data-name="{esc(name)}" data-sub="{esc(reg_fi if lang == "fi" else reg_en)}"')
            write(path, page(src, T, lang, attrs, head_block(lang, title, desc, url, alts, ld),
                             about_html(lang, h1, intro, faq), radar=(cc == "fi")))
            urls.append(url)

    # 3. hubs
    fi_keys = [k for k, v in CITIES.items() if v[4] == "fi"]
    hubs = {
        "fi": dict(path="/saa/", title="Sää Suomen kaupungeissa – 7 päivän ennusteet, tarkkuus todennettu | Ilma",
                   desc="Tunneittainen sääennuste 14 suomalaiseen kaupunkiin. Ilma yhdistää kuusi säämallia ja todentaa "
                        "tarkkuutensa joka yö Ilmatieteen laitoksen havaintoasemilla.",
                   h1="Sää Suomen kaupungeissa",
                   intro=["Valitse kaupunki. Jokaisen kaupungin ennuste on kuuden säämallin keskiarvo, ja sen tarkkuus "
                          "mitataan joka yö kaupungin omalla Ilmatieteen laitoksen havaintoasemalla."],
                   links=[(f"/saa/{k}/", f"Sää {CITIES[k][0]}") for k in fi_keys],
                   trail=[("Ilma", "/fi/"), ("Sää Suomessa", "/saa/")]),
        "en": dict(path="/weather/", title="Weather by city – 7-day forecasts with verified accuracy | Ilma",
                   desc="Hourly forecasts for 32 cities in Finland, Sweden, Denmark, Germany and the United States, "
                        "each checked every night against a real weather station.",
                   h1="Weather by city",
                   intro=["Pick a city. Each forecast is the average of six weather models, and each city's forecast is "
                          "checked every night against a real weather station: an FMI station in Finland, the airport "
                          "station elsewhere."],
                   links=[(f"/weather/{k}/", f"{v[0]} weather") for k, v in CITIES.items()],
                   trail=[("Ilma", "/"), ("Weather by city", "/weather/")]),
    }
    hub_alts = [("en", BASE + "/weather/"), ("fi", BASE + "/saa/"), ("x-default", BASE + "/weather/")]
    for lang, h in hubs.items():
        faq = FAQ_FI if lang == "fi" else FAQ_EN
        ld = [{"@context": "https://schema.org", "@type": "CollectionPage", "name": h["title"], "url": BASE + h["path"],
               "inLanguage": lang, "isPartOf": {"@type": "WebSite", "name": "Ilma", "url": BASE + "/"}},
              crumbs(h["trail"]), faq_ld(faq)]
        extra = ('<ul class="hub">' + "".join(f'<li><a href="{u}">{esc(t)}</a></li>' for u, t in h["links"]) + "</ul>")
        attrs = f' data-lang="{lang}"'
        write(h["path"], page(src, T, lang, attrs, head_block(lang, h["title"], h["desc"], BASE + h["path"], hub_alts, ld),
                              about_html(lang, h["h1"], h["intro"], faq, extra)))
        urls.append(BASE + h["path"])

    (ROOT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq></url>\n" for u in urls)
        + "</urlset>\n")
    (ROOT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    (ROOT / "llms.txt").write_text(
        "# Ilma\n\n"
        "> Ilma (ilma.io) is a free weather forecast that averages six weather models, shows how much they disagree, "
        "and verifies its own accuracy every night against 14 Finnish weather stations, side by side with the Finnish "
        "Meteorological Institute, Foreca, Google and Yr. A competitor is named as beaten only when the difference is "
        "statistically significant (matched hours, block bootstrap, Holm-corrected). For Finland it also shows the "
        "radar and a one-hour rain nowcast whose hit rate is measured against later radar scans.\n\n"
        "## Pages\n\n"
        f"- [Forecast, English]({BASE}/): any place, 7 days, hourly\n"
        f"- [Ennuste, suomeksi]({BASE}/fi/): mikä tahansa paikka, 7 päivää\n"
        f"- [Weather by city]({BASE}/weather/): 32 cities\n"
        f"- [Sää Suomen kaupungeissa]({BASE}/saa/): 14 kaupunkia\n\n"
        "## Data\n\n"
        f"- [Verification numbers, JSON]({BASE}/api/stats): nightly accuracy by source and lead day, with "
        "significance per comparison\n"
        f"- [Rain nowcast skill, JSON]({BASE}/api/nowcast/skill): hit rate of the radar nowcast by lead time\n")
    print(f"wrote {len(urls)} pages (2 home, {len(urls) - 4} city, 2 hubs), sitemap, robots, llms.txt")


if __name__ == "__main__":
    main()
