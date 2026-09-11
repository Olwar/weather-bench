"""Generate the SEO surface of ilma.io from web/static/index.html.

The app is one HTML file that renders everything from the API, so crawlers
saw a title and nothing else. This script writes:

  web/static/saa/<city>/index.html      Finnish landing page, Finnish cities
  web/static/weather/<city>/index.html  English landing page, every city
  web/static/sitemap.xml, web/static/robots.txt
  and refreshes the city link list inside index.html (between <!--cities--> markers)

Each landing page IS the app (same markup and script) with a different head,
`data-*` attributes on <html> that make the bootstrap load that city in that
language, and a static, visible text block so the page has content before any
script runs. Run after every index.html change:  python3 web/seo.py
Generated pages are gitignored; deploy/site.sh runs this before deploying.
"""
import json
import re
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


def coords():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from common import CITIES as C
    return {c["key"]: (c["lat"], c["lon"]) for c in C}


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
            f'<meta property="og:image" content="{BASE}/og.png">\n'
            f'<meta property="og:image:width" content="1200">\n'
            f'<meta property="og:image:height" content="630">\n'
            f'<meta name="twitter:card" content="summary_large_image">\n'
            f'<meta name="twitter:title" content="{esc(title)}">\n'
            f'<meta name="twitter:description" content="{esc(desc)}">\n'
            f'<meta name="twitter:image" content="{BASE}/og.png">\n'
            f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>\n')


FAQ_FI = [
    ("Mihin Ilman ennuste perustuu?",
     "Ilma laskee keskiarvon kuudesta ennustemallista: ECMWF AIFS ja sen parvi, ECMWF IFS, MET Nordic, Ilmatieteen laitoksen toimitettu ennuste ja Open-Meteo. Varjostettu alue kaaviossa näyttää, kuinka paljon mallit ovat eri mieltä."),
    ("Kuinka tarkka ennuste on?",
     "Tarkkuus todennetaan joka yö 14 suomalaisella sääasemalla, ja tulokset näkyvät sivulla. Sivu väittää olevansa tarkempi kuin kilpailija vain, kun ero on tilastollisesti merkitsevä."),
    ("Mitä Ilma-avustaja osaa?",
     "Voit kysyä säästä omin sanoin, esimerkiksi sopiiko lauantai-ilta ulkoilmatapahtumaan. Avustaja käyttää samoja työkaluja kuin selainten tekoälyagentit: kalibroituja todennäköisyyksiä, ennusteen vakautta ja päätösajankohtaa."),
    ("Mitä WebMCP tarkoittaa?",
     "WebMCP on avoin standardi, jolla verkkosivu tarjoaa työkaluja selaimessa toimiville tekoälyagenteille. Ilma rekisteröi 19 työkalua, joten agentti voi lukea todennettuja todennäköisyyksiä ja merkitä kaavioon sen, mitä ihminen katsoo."),
]
FAQ_EN = [
    ("What is the forecast based on?",
     "Ilma averages six forecast models: ECMWF AIFS and its ensemble, ECMWF IFS, MET Nordic, the Finnish Meteorological Institute's edited forecast and Open-Meteo. The shaded band on the chart shows how much the models disagree."),
    ("How accurate is it?",
     "Accuracy is verified every night against 14 Finnish weather stations, and the results are shown on the page. The site claims to beat a competitor only when the difference is statistically significant."),
    ("What can the Ilma assistant do?",
     "Ask about the weather in your own words, for example whether Saturday evening works for an outdoor event. The assistant uses the same tools that browser AI agents get: calibrated probabilities, forecast stability and when to decide."),
    ("What is WebMCP?",
     "WebMCP is an open standard that lets a web page offer tools to AI agents running in the browser. Ilma registers 19 tools, so an agent can read verified probabilities and mark the chart the human is looking at."),
]


def about_html(lang, h1, intro, faq):
    q = "".join(f"<details><summary>{esc(a)}</summary><p>{esc(b)}</p></details>" for a, b in faq)
    return (f'<section class="about" id="about"><h1>{esc(h1)}</h1>'
            + "".join(f"<p>{esc(p)}</p>" for p in intro)
            + f'<div class="faq">{q}</div></section>')


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


def main():
    src = SRC.read_text()
    # 1. refresh the city link list inside index.html itself
    links = city_links()
    src = re.sub(r"<!--cities-->.*?<!--/cities-->", f"<!--cities-->{links}<!--/cities-->", src, flags=re.S)
    SRC.write_text(src)

    head_re = re.compile(r"<!--seo-->.*?<!--/seo-->", re.S)
    about_re = re.compile(r"<!--about-->.*?<!--/about-->", re.S)
    urls = [(f"{BASE}/", "1.0", "daily")]
    ll = coords()

    for key, (name, gen, reg_fi, reg_en, cc) in CITIES.items():
        lat, lon = ll[key]
        pages = [("en", f"/weather/{key}/")] + ([("fi", f"/saa/{key}/")] if cc == "fi" else [])
        alts = [(l, BASE + p) for l, p in pages]
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
                         f"Ennusteen tarkkuus {reg_fi}n alueella todennetaan joka yö oikeilla sääasemilla, ja luvut ovat "
                         f"sivulla nähtävissä. Kysy Ilma-avustajalta omin sanoin, esimerkiksi sopiiko viikonloppu retkelle."]
                faq = FAQ_FI
            else:
                title = f"{name} weather – 7-day forecast with verified accuracy | Ilma"
                desc = (f"Hourly weather for {name}, {reg_en}, 7 days ahead. Ilma blends six forecast models, shows "
                        f"how much they disagree, and verifies its accuracy against real weather stations every night.")
                h1 = f"{name} weather"
                intro = [f"The weather in {name}, {reg_en}, for the next seven days, hour by hour. Ilma averages six "
                         f"forecast models and shows the band the models fall in: a narrow band means a confident "
                         f"forecast, a wide one means uncertainty.",
                         f"Accuracy is verified every night against real weather stations, and the numbers are on the "
                         f"page. Ask the Ilma assistant in plain words, for example whether the weekend works for a hike."]
                faq = FAQ_EN
            ld = [{"@context": "https://schema.org", "@type": "WebPage", "name": title, "url": url,
                   "inLanguage": lang, "isPartOf": {"@type": "WebSite", "name": "Ilma", "url": BASE + "/"},
                   "about": {"@type": "Place", "name": name,
                             "geo": {"@type": "GeoCoordinates", "latitude": lat, "longitude": lon}}},
                  faq_ld(faq)]
            html = src.replace('<html lang="en">',
                               f'<html lang="{lang}" data-lang="{lang}" data-city="{key}" data-lat="{lat}" data-lon="{lon}" '
                               f'data-name="{esc(name)}" data-sub="{esc(reg_fi if lang == "fi" else reg_en)}">')
            html = head_re.sub(lambda m: "<!--seo-->\n" + head_block(lang, title, desc, url, alts, ld) + "<!--/seo-->", html)
            html = about_re.sub(lambda m: "<!--about-->" + about_html(lang, h1, intro, faq) + "<!--/about-->", html)
            out = ROOT / path.strip("/") / "index.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html)
            urls.append((url, "0.8", "daily"))

    (ROOT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc><changefreq>{c}</changefreq><priority>{p}</priority></url>\n" for u, p, c in urls)
        + "</urlset>\n")
    (ROOT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    print(f"wrote {len(urls) - 1} city pages, sitemap with {len(urls)} urls")


if __name__ == "__main__":
    main()
