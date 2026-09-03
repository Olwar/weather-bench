"""Shared plumbing for the Finnish weather forecast benchmark.

All timestamps are stored as UTC ISO strings "YYYY-MM-DDTHH:MMZ".
Daily aggregation (tmin/tmax) is done over Europe/Helsinki local days in score.py.
"""
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# macOS blocks launchd-started jobs from reading ~/Documents, so once install.sh
# has set up the background agent the data lives under ~/Library instead. Prefer
# that location when it exists so scheduled and manual runs share one database.
_LIBRARY_DATA = Path.home() / "Library/Application Support/weather-bench/data"
if os.environ.get("WEATHERBENCH_DATA"):
    DATA_DIR = Path(os.environ["WEATHERBENCH_DATA"])
elif _LIBRARY_DATA.exists():
    DATA_DIR = _LIBRARY_DATA
else:
    DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "bench.sqlite"

# key = city slug; fmi_place = FMI WFS place param; foreca_id = 100000000 + geonames id;
# lat/lon = the FMI observation station used for verification (all sources are
# interpolated/requested at this exact point so everyone is judged on the same spot).
CITIES = [
    {"country": "fi", "key": "helsinki",     "fmi_place": "helsinki",      "foreca_id": 100658225, "foreca_path": "Finland/Helsinki",      "lat": 60.17523, "lon": 24.94459},
    {"country": "fi", "key": "tampere",      "fmi_place": "tampere",       "foreca_id": 100634963, "foreca_path": "Finland/Tampere",       "lat": 61.51757, "lon": 23.75388},
    {"country": "fi", "key": "oulu",         "fmi_place": "oulu",          "foreca_id": 100643492, "foreca_path": "Finland/Oulu",          "lat": 64.99685, "lon": 25.52233},
    {"country": "fi", "key": "rovaniemi",    "fmi_place": "rovaniemi",     "foreca_id": 100638936, "foreca_path": "Finland/Rovaniemi",     "lat": 66.49832, "lon": 25.70880},
    {"country": "fi", "key": "turku",        "fmi_place": "turku",         "foreca_id": 100633679, "foreca_path": "Finland/Turku",         "lat": 60.45439, "lon": 22.17870},
    {"country": "fi", "key": "jyvaskyla",    "fmi_place": "jyvaskyla",     "foreca_id": 100655194, "foreca_path": "Finland/Jyvaskyla",     "lat": 62.39332, "lon": 25.68862},
    {"country": "fi", "key": "vaasa",        "fmi_place": "vaasa",         "foreca_id": 100632978, "foreca_path": "Finland/Vaasa",         "lat": 63.09871, "lon": 21.63938},
    {"country": "fi", "key": "kuopio",       "fmi_place": "kuopio",        "foreca_id": 100650224, "foreca_path": "Finland/Kuopio",        "lat": 62.89256, "lon": 27.63331},
    {"country": "fi", "key": "joensuu",      "fmi_place": "joensuu",       "foreca_id": 100655808, "foreca_path": "Finland/Joensuu",       "lat": 62.60179, "lon": 29.72713},
    {"country": "fi", "key": "lappeenranta", "fmi_place": "lappeenranta",  "foreca_id": 100648900, "foreca_path": "Finland/Lappeenranta",  "lat": 61.04030, "lon": 28.12916},
    {"country": "fi", "key": "pori",         "fmi_place": "pori",          "foreca_id": 100640999, "foreca_path": "Finland/Pori",          "lat": 61.46011, "lon": 21.80839},
    {"country": "fi", "key": "kajaani",      "fmi_place": "kajaani",       "foreca_id": 100654899, "foreca_path": "Finland/Kajaani",       "lat": 64.28290, "lon": 27.67114},
    {"country": "fi", "key": "sodankyla",    "fmi_place": "sodankylä",     "foreca_id": 100636464, "foreca_path": "Finland/Sodankyla",     "lat": 67.36663, "lon": 26.62901},
    {"country": "fi", "key": "mariehamn",    "fmi_place": "maarianhamina", "foreca_id": 103041732, "foreca_path": "Finland/Maarianhamina", "lat": 60.12735, "lon": 19.90038},
    # ---- International cities (added 2026-08-26, stages 1+2) ----
    # Verification truth is the airport METAR station ("metar" = ICAO id);
    # lat/lon are the AIRPORT coordinates, so every forecast source is asked
    # about the exact point the observations describe - same station-point
    # discipline as the Finnish FMI stations. METAR temps are often whole
    # degrees, a coarser truth than FMI; disclosed in the README.
    {"country": "se", "key": "stockholm",  "metar": "ESSB", "lat": 59.3544, "lon": 17.9416},
    {"country": "se", "key": "goteborg",   "metar": "ESGG", "lat": 57.6628, "lon": 12.2798},
    {"country": "se", "key": "malmo",      "metar": "ESMS", "lat": 55.5300, "lon": 13.3762},
    {"country": "se", "key": "lulea",      "metar": "ESPA", "lat": 65.5436, "lon": 22.1220},
    {"country": "dk", "key": "kobenhavn",  "metar": "EKCH", "lat": 55.6180, "lon": 12.6560},
    {"country": "dk", "key": "aarhus",     "metar": "EKAH", "lat": 56.3000, "lon": 10.6190},
    {"country": "dk", "key": "aalborg",    "metar": "EKYT", "lat": 57.0928, "lon": 9.8492},
    {"country": "de", "key": "berlin",     "metar": "EDDB", "lat": 52.3667, "lon": 13.5033},
    {"country": "de", "key": "hamburg",    "metar": "EDDH", "lat": 53.6304, "lon": 9.9882},
    {"country": "de", "key": "munchen",    "metar": "EDDM", "lat": 48.3538, "lon": 11.7861},
    {"country": "de", "key": "frankfurt",  "metar": "EDDF", "lat": 50.0379, "lon": 8.5622},
    {"country": "de", "key": "koln",       "metar": "EDDK", "lat": 50.8659, "lon": 7.1427},
    {"country": "us", "key": "newyork",    "metar": "KLGA", "lat": 40.7772, "lon": -73.8726},
    {"country": "us", "key": "chicago",    "metar": "KMDW", "lat": 41.7868, "lon": -87.7522},
    {"country": "us", "key": "houston",    "metar": "KHOU", "lat": 29.6454, "lon": -95.2789},
    {"country": "us", "key": "denver",     "metar": "KDEN", "lat": 39.8617, "lon": -104.6731},
    {"country": "us", "key": "seattle",    "metar": "KSEA", "lat": 47.4502, "lon": -122.3088},
    {"country": "us", "key": "miami",      "metar": "KMIA", "lat": 25.7959, "lon": -80.2870},
]

CITY_COUNTRY = {c["key"]: c["country"] for c in CITIES}

# Open-Meteo model ids benchmarked prospectively and retrospectively.
# gfs_graphcast025 was tried and dropped 2026-07-23: Open-Meteo returns all-null
# values for it (live and archive) - NOAA's experimental GraphCast feed is dead.
OM_MODELS = [
    "ecmwf_ifs025",         # ECMWF IFS physics model (what Foreca/FMI build on)
    "ecmwf_aifs025_single", # ECMWF AIFS deterministic - the AI model
    "metno_nordic",         # MET Norway post-processed 1km Nordic dataset
    "best_match",           # Open-Meteo's auto blend - what a solo dev's app would ship
]

# api.met.no terms require an identifying User-Agent with a contact point;
# the benchmark site doubles as that contact.
UA = "weather-bench/0.1 (+http://89.167.5.149:8080; forecast verification research)"


def http_get(url: str, tries: int = 3, sleep: float = 2.0, timeout: float = 60) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 - retry any transport error
            last = e
            time.sleep(sleep * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}") from last


def http_json(url: str, tries: int = 3, sleep: float = 2.0, timeout: float = 60) -> dict:
    return json.loads(http_get(url, tries=tries, sleep=sleep, timeout=timeout))


def fmi_simple(storedquery: str, expect_pos=None, _tries: int = 3, _timeout: float = 60, **params) -> list[tuple[str, str, float]]:
    """Query an FMI WFS ::simple stored query. Returns [(utc_iso, param_name, value)].

    expect_pos=(lat, lon): assert the responding station is the pinned one -
    place-name resolution drifting to a different station mid-benchmark would
    silently corrupt the verification truth.
    """
    qs = urllib.parse.urlencode(params)  # place names like "sodankylä" need encoding
    url = (
        "https://opendata.fmi.fi/wfs?service=WFS&version=2.0.0&request=getFeature"
        f"&storedquery_id={storedquery}&{qs}"
    )
    xml = http_get(url, tries=_tries, timeout=_timeout)
    ns = {
        "BsWfs": "http://xml.fmi.fi/schema/wfs/2.0",
        "gml": "http://www.opengis.net/gml/3.2",
    }
    root = ET.fromstring(xml)
    out = []
    checked = False
    for el in root.iter("{http://xml.fmi.fi/schema/wfs/2.0}BsWfsElement"):
        if expect_pos is not None and not checked:
            pos = el.find("BsWfs:Location/gml:Point/gml:pos", ns)
            if pos is not None:
                lat, lon = (float(x) for x in pos.text.split())
                if abs(lat - expect_pos[0]) > 0.02 or abs(lon - expect_pos[1]) > 0.03:
                    raise RuntimeError(
                        f"station drift: got {lat},{lon}, expected {expect_pos}"
                    )
                checked = True
        t = el.find("BsWfs:Time", ns).text
        p = el.find("BsWfs:ParameterName", ns).text
        v = el.find("BsWfs:ParameterValue", ns).text
        if v is None or v == "NaN":
            continue
        # normalize 2026-07-23T12:00:00Z -> 2026-07-23T12:00Z
        out.append((t[:16] + "Z", p, float(v)))
    return out


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    # WAL + a busy timeout longer than any writer's transaction. The nightly
    # blend rebuild commits once per blend (up to ~10 min each) and the wipe is
    # one ~15 min DELETE; the 5-hourly collect timer drifts and periodically
    # lands inside that window. With a short timeout collect died with
    # "database is locked" and the whole snapshot was lost (2026-09-03 05:04Z).
    # Waiting up to an hour is harmless: WAL readers are never blocked, and a
    # collect run that waits still stores its snapshot.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=3600000")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS forecasts(
          source TEXT NOT NULL, city TEXT NOT NULL, run_time TEXT NOT NULL,
          target_time TEXT NOT NULL, var TEXT NOT NULL, value REAL NOT NULL,
          PRIMARY KEY(source, city, run_time, target_time, var));
        CREATE TABLE IF NOT EXISTS observations(
          city TEXT NOT NULL, time TEXT NOT NULL, var TEXT NOT NULL, value REAL NOT NULL,
          PRIMARY KEY(city, time, var));
        CREATE TABLE IF NOT EXISTS retro_forecasts(
          model TEXT NOT NULL, city TEXT NOT NULL, lead_days INTEGER NOT NULL,
          target_time TEXT NOT NULL, value REAL NOT NULL,
          PRIMARY KEY(model, city, lead_days, target_time));
        CREATE TABLE IF NOT EXISTS collect_log(
          run_time TEXT NOT NULL, source TEXT NOT NULL, city TEXT NOT NULL,
          rows INTEGER NOT NULL, error TEXT);
        """
    )
    return con


def store_observations(con: sqlite3.Connection, city: str, rows: list[tuple[str, str, float]]):
    # OR REPLACE: the 166h re-fetch window lets FMI's later QC corrections
    # overwrite provisional values - claims verify against final QC'd data.
    con.executemany(
        "INSERT OR REPLACE INTO observations(city, time, var, value) VALUES(?,?,?,?)",
        [(city, t, var, v) for (t, var, v) in rows],
    )


# FMI observation parameter -> our canonical var name.
# Extended 2026-08-22 (exploratory endpoints; the pre-registered t2m primary is
# untouched): humidity, dew point, gusts, direction, sea-level pressure, cloud
# amount and snow depth. Availability varies by station - a missing parameter
# simply yields no rows there, and scoring only counts matched pairs.
OBS_PARAMS = {
    "t2m": "t2m", "ws_10min": "ws", "r_1h": "rain1h",
    "rh": "rh", "td": "td", "wg_10min": "gust", "wd_10min": "wdir",
    "p_sea": "pmsl",
    "n_man": "cc",      # cloud amount in octas 0-8; converted to % below
    "snow_aws": "snow", # cm; FMI encodes "no snow" as -1
}


def fetch_obs(city: dict, start_utc: str, end_utc: str) -> list[tuple[str, str, float]]:
    """Hourly (top-of-hour) temperature/wind/precip observations from the city's FMI station."""
    rows = fmi_simple(
        "fmi::observations::weather::simple",
        expect_pos=(city["lat"], city["lon"]),
        place=city["fmi_place"], parameters=",".join(OBS_PARAMS), timestep=60,
        starttime=start_utc, endtime=end_utc,
    )
    out = []
    for (t, p, v) in rows:
        var = OBS_PARAMS.get(p)
        if var is None:
            continue
        # Unit normalization at the door, so the DB speaks one language per var:
        # cloud in percent (models publish %), snow depth with -1 meaning bare
        # ground mapped to 0 so summer scores aren't poisoned by a sentinel.
        if var == "cc":
            v = v * 12.5
        elif var == "snow" and v < 0:
            v = 0.0
        out.append((t, var, v))
    return out
