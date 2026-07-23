"""Shared plumbing for the Finnish weather forecast benchmark.

All timestamps are stored as UTC ISO strings "YYYY-MM-DDTHH:MMZ".
Daily aggregation (tmin/tmax) is done over Europe/Helsinki local days in score.py.
"""
import json
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "bench.sqlite"

# key = city slug; fmi_place = FMI WFS place param; foreca_id = 100000000 + geonames id;
# lat/lon = the FMI observation station used for verification (all sources are
# interpolated/requested at this exact point so everyone is judged on the same spot).
CITIES = [
    {"key": "helsinki",  "fmi_place": "helsinki",  "foreca_id": 100658225, "foreca_path": "Finland/Helsinki",  "lat": 60.17523, "lon": 24.94459},
    {"key": "tampere",   "fmi_place": "tampere",   "foreca_id": 100634963, "foreca_path": "Finland/Tampere",   "lat": 61.51757, "lon": 23.75388},
    {"key": "oulu",      "fmi_place": "oulu",      "foreca_id": 100643492, "foreca_path": "Finland/Oulu",      "lat": 64.99685, "lon": 25.52233},
    {"key": "rovaniemi", "fmi_place": "rovaniemi", "foreca_id": 100638936, "foreca_path": "Finland/Rovaniemi", "lat": 66.49832, "lon": 25.70880},
    {"key": "turku",     "fmi_place": "turku",     "foreca_id": 100633679, "foreca_path": "Finland/Turku",     "lat": 60.45439, "lon": 22.17870},
    {"key": "jyvaskyla", "fmi_place": "jyvaskyla", "foreca_id": 100655194, "foreca_path": "Finland/Jyvaskyla", "lat": 62.39332, "lon": 25.68862},
]

# Open-Meteo model ids benchmarked prospectively and retrospectively.
# gfs_graphcast025 was tried and dropped 2026-07-23: Open-Meteo returns all-null
# values for it (live and archive) - NOAA's experimental GraphCast feed is dead.
OM_MODELS = [
    "ecmwf_ifs025",         # ECMWF IFS physics model (what Foreca/FMI build on)
    "ecmwf_aifs025_single", # ECMWF AIFS deterministic - the AI model
    "metno_nordic",         # MET Norway post-processed 1km Nordic dataset
]

UA = "weather-bench/0.1 (personal forecast verification research)"


def http_get(url: str, tries: int = 3, sleep: float = 2.0) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 - retry any transport error
            last = e
            time.sleep(sleep * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}") from last


def http_json(url: str) -> dict:
    return json.loads(http_get(url))


def fmi_simple(storedquery: str, **params) -> list[tuple[str, str, float]]:
    """Query an FMI WFS ::simple stored query. Returns [(utc_iso, param_name, value)]."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = (
        "https://opendata.fmi.fi/wfs?service=WFS&version=2.0.0&request=getFeature"
        f"&storedquery_id={storedquery}&{qs}"
    )
    xml = http_get(url)
    ns = {"BsWfs": "http://xml.fmi.fi/schema/wfs/2.0"}
    out = []
    for el in ET.fromstring(xml).iter("{http://xml.fmi.fi/schema/wfs/2.0}BsWfsElement"):
        t = el.find("BsWfs:Time", ns).text
        p = el.find("BsWfs:ParameterName", ns).text
        v = el.find("BsWfs:ParameterValue", ns).text
        if v is None or v == "NaN":
            continue
        # normalize 2026-07-23T12:00:00Z -> 2026-07-23T12:00Z
        out.append((t[:16] + "Z", p, float(v)))
    return out


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
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
    con.executemany(
        "INSERT OR IGNORE INTO observations(city, time, var, value) VALUES(?,?,?,?)",
        [(city, t, var, v) for (t, var, v) in rows],
    )


# FMI observation parameter -> our canonical var name
OBS_PARAMS = {"t2m": "t2m", "ws_10min": "ws", "r_1h": "rain1h"}


def fetch_obs(city: dict, start_utc: str, end_utc: str) -> list[tuple[str, str, float]]:
    """Hourly (top-of-hour) temperature/wind/precip observations from the city's FMI station."""
    rows = fmi_simple(
        "fmi::observations::weather::simple",
        place=city["fmi_place"], parameters=",".join(OBS_PARAMS), timestep=60,
        starttime=start_utc, endtime=end_utc,
    )
    return [(t, OBS_PARAMS[p], v) for (t, p, v) in rows if p in OBS_PARAMS]
