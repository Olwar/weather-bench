"""Radar nowcast: FMI's Finland rain-rate composite, moved forward by its own motion.

Every 5 minutes (systemd timer):
  1. Ask FMI's WFS for the newest composite times and fetch the raw 16-bit
     GeoTIFFs (EPSG:3067, 1 km, hundredths of mm/h; 65535 = outside coverage)
     for t, t-5 and t-10 min. Raw frames are cached under data/nowcast/raw so
     a run fetches one new file.
  2. Estimate the motion field with dense optical flow (Farneback) between
     consecutive frames on a log-rain image, average the two fields, fill the
     rain-free areas with the intensity-weighted mean motion and smooth.
  3. Advect the newest frame backwards along that field (semi-Lagrangian,
     constant velocity) for +10 .. +60 min. This is Lagrangian persistence,
     the same first-order method behind the national services' 0-2 h views:
     good for fronts and bands, blind to showers that form or die in place.
  4. Reproject the newest scan and the future frames to Web Mercator, colour
     them with a fixed rain-rate palette and write PNGs + index.json.

Output: data/nowcast/index.json, data/nowcast/<stamp>.png. Read by web/app.py
(/api/nowcast). Display-only: nothing here feeds a score. Runs in ~15 s.
"""
import io
import json
import logging
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import pyproj
import tifffile
from PIL import Image

from common import DATA_DIR, UA

# FMI writes GDAL_NODATA as "0.0"; tifffile expects an int and logs a
# warning per file, which is noise in the journal.
logging.getLogger("tifffile").setLevel(logging.ERROR)

WFS = "https://opendata.fmi.fi/wfs?service=WFS&version=2.0.0&request=getFeature&storedquery_id=fmi::radar::composite::rr"
WMS = "https://openwms.fmi.fi/geoserver/Radar/wms"
LAYER = "Radar:suomi_rr_eureffin"
BBOX = (-118331.366, 6335621.167, 875567.732, 7907751.537)   # composite extent, EPSG:3067
W, H = 994, 1572                                             # 1 km cells
STEP = 5                                                     # composite cadence, minutes
PAST = [0]                                                   # the newest scan anchors the loop
FUTURE = list(range(5, 65, 5))                               # next hour, 5-minute steps
OUT = DATA_DIR / "nowcast"
RAW = OUT / "raw"
KEEP_RAW = 16
OUT_RES_M = 2400                                             # output pixel size in Web Mercator
NODATA = 65535
# rain-rate classes (mm/h) and RGBA, from light to violent
PALETTE = [(0.1, (110, 205, 235)), (0.3, (60, 175, 195)), (0.9, (40, 150, 90)), (2.2, (205, 200, 40)),
           (4.0, (240, 160, 30)), (10.0, (230, 70, 40)), (25.0, (185, 30, 120))]
ALPHA = 205


def _get(url, timeout=120, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url[:120]}") from last


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_time() -> datetime:
    start = _iso(datetime.now(timezone.utc) - timedelta(minutes=40))
    xml = _get(f"{WFS}&starttime={start}", timeout=60).decode("utf-8", "replace")
    times = sorted(set(re.findall(r"timePosition>\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", xml)))
    if not times:
        raise RuntimeError("WFS listed no composite in the last 40 minutes")
    return datetime.strptime(times[-1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def frame(t: datetime) -> np.ndarray:
    """Rain rate in mm/h as float32; NaN outside radar coverage. Cached on disk."""
    stamp = t.strftime("%Y%m%dT%H%M")
    path = RAW / f"{stamp}.npy"
    if path.exists():
        return np.load(path)
    url = (f"{WMS}?service=WMS&version=1.3.0&request=GetMap&layers={LAYER}&styles=raster"
           f"&bbox={BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]}&crs=EPSG:3067&format=image/geotiff"
           f"&width={W}&height={H}&time={_iso(t)}")
    a = tifffile.imread(io.BytesIO(_get(url)))
    if a.shape != (H, W):
        raise RuntimeError(f"unexpected composite shape {a.shape}")
    rr = a.astype(np.float32) / 100.0
    rr[a == NODATA] = np.nan
    RAW.mkdir(parents=True, exist_ok=True)
    np.save(path, rr)
    return rr


def _flow_img(rr: np.ndarray) -> np.ndarray:
    x = np.log1p(np.nan_to_num(rr, nan=0.0) * 10.0)
    return np.clip(x / np.log1p(500.0) * 255.0, 0, 255).astype(np.uint8)


def motion(frames: list) -> np.ndarray:
    """Pixels per STEP minutes, shape (H, W, 2), from the two newest steps."""
    imgs = [_flow_img(f) for f in frames]
    flows = [cv2.calcOpticalFlowFarneback(imgs[i], imgs[i + 1], None, 0.5, 4, 25, 3, 7, 1.5, 0)
             for i in range(len(imgs) - 1)]
    v = np.mean(flows, axis=0).astype(np.float32)
    # Farneback returns ~0 where nothing moves visibly, i.e. wherever it is
    # dry; rain must still be carried INTO those areas. Fill them with the
    # intensity-weighted mean motion of the rain that is there, then smooth.
    wgt = imgs[-1].astype(np.float32)
    if wgt.sum() > 0:
        mean_v = (v * wgt[..., None]).sum(axis=(0, 1)) / wgt.sum()
        wet = cv2.dilate((wgt > 8).astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
        v[~wet] = mean_v
    v = cv2.GaussianBlur(v, (0, 0), 12)
    return v


def advect(rr: np.ndarray, v: np.ndarray, steps: int) -> np.ndarray:
    """Semi-Lagrangian backward sampling: value at x now was at x - steps*v."""
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    src = np.nan_to_num(rr, nan=0.0)
    out = cv2.remap(src, xs - steps * v[..., 0], ys - steps * v[..., 1], cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
    out[np.isnan(rr)] = np.nan
    return out


def _mercator_maps():
    """Sampling maps from a Web-Mercator output grid into the 3067 composite."""
    cache = OUT / "maps.npz"
    if cache.exists():
        z = np.load(cache)
        return z["mx"], z["my"], tuple(z["bbox"].tolist())
    to_merc = pyproj.Transformer.from_crs("EPSG:3067", "EPSG:3857", always_xy=True)
    to_utm = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:3067", always_xy=True)
    ex = np.linspace(BBOX[0], BBOX[2], 200)
    ey = np.linspace(BBOX[1], BBOX[3], 200)
    edge_x = np.concatenate([ex, ex, np.full(200, BBOX[0]), np.full(200, BBOX[2])])
    edge_y = np.concatenate([np.full(200, BBOX[1]), np.full(200, BBOX[3]), ey, ey])
    mx, my = to_merc.transform(edge_x, edge_y)
    bbox = (float(mx.min()), float(my.min()), float(mx.max()), float(my.max()))
    ow = int(np.ceil((bbox[2] - bbox[0]) / OUT_RES_M))
    oh = int(np.ceil((bbox[3] - bbox[1]) / OUT_RES_M))
    gx = bbox[0] + (np.arange(ow) + 0.5) * (bbox[2] - bbox[0]) / ow
    gy = bbox[3] - (np.arange(oh) + 0.5) * (bbox[3] - bbox[1]) / oh
    GX, GY = np.meshgrid(gx, gy)
    ux, uy = to_utm.transform(GX.ravel(), GY.ravel())
    sx = ((np.asarray(ux) - BBOX[0]) / (BBOX[2] - BBOX[0]) * W - 0.5).reshape(oh, ow).astype(np.float32)
    sy = ((BBOX[3] - np.asarray(uy)) / (BBOX[3] - BBOX[1]) * H - 0.5).reshape(oh, ow).astype(np.float32)
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez(cache, mx=sx, my=sy, bbox=np.array(bbox))
    return sx, sy, bbox


def render(rr: np.ndarray, maps, path: Path):
    sx, sy, _ = maps
    m = cv2.remap(np.nan_to_num(rr, nan=0.0), sx, sy, cv2.INTER_LINEAR,
                  borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
    rgba = np.zeros(m.shape + (4,), np.uint8)
    for thr, col in PALETTE:
        sel = m >= thr
        rgba[sel, :3] = col
        rgba[sel, 3] = ALPHA
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def main():
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    now = latest_time()
    need = sorted({now + timedelta(minutes=m) for m in PAST + [-STEP, -2 * STEP]})
    frames = {t: frame(t) for t in need}
    v = motion([frames[now - timedelta(minutes=2 * STEP)], frames[now - timedelta(minutes=STEP)], frames[now]])
    speed = float(np.hypot(v[..., 0], v[..., 1]).mean()) * 60 / STEP   # km/h at 1 km cells
    maps = _mercator_maps()
    bbox = maps[2]
    to_ll = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    w, s = to_ll.transform(bbox[0], bbox[1])
    e, n = to_ll.transform(bbox[2], bbox[3])
    out_frames, keep = [], set()
    for lead in PAST:
        t = now + timedelta(minutes=lead)
        name = f"{t.strftime('%Y%m%dT%H%M')}_obs.png"
        if not (OUT / name).exists():
            render(frames[t], maps, OUT / name)
        out_frames.append({"t": _iso(t), "lead_min": lead, "file": name, "kind": "obs"})
        keep.add(name)
    for lead in FUTURE:
        t = now + timedelta(minutes=lead)
        name = f"{now.strftime('%Y%m%dT%H%M')}_p{lead:02d}.png"
        render(advect(frames[now], v, lead / STEP), maps, OUT / name)
        out_frames.append({"t": _iso(t), "lead_min": lead, "file": name, "kind": "nowcast"})
        keep.add(name)
    index = {"latest": _iso(now), "generated": _iso(datetime.now(timezone.utc)), "step_min": STEP,
             "bbox_lnglat": {"w": w, "s": s, "e": e, "n": n},
             "mean_speed_kmh": round(speed, 1), "frames": out_frames,
             "attribution": "Ilmatieteen laitos (CC BY 4.0); nowcast by Ilma"}
    tmp = OUT / "index.json.tmp"
    tmp.write_text(json.dumps(index))
    tmp.replace(OUT / "index.json")
    for p in OUT.glob("*.png"):
        if p.name not in keep:
            p.unlink()
    for p in sorted(RAW.glob("*.npy"))[:-KEEP_RAW]:
        p.unlink()
    print(f"nowcast {_iso(now)}: {len(out_frames)} frames, mean motion {speed:.0f} km/h, {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
