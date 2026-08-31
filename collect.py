#!/usr/bin/env python3
"""
collect.py — one poll of the ActiveSG live crowd feed, appended to data.csv.

Designed to be run by GitHub Actions every 20 minutes. Self-contained:
the only dependency is `requests`.

Endpoint resolution order:
  1. ACTIVESG_ENDPOINT environment variable, if set
  2. auto-discovery: scrape the page's JS bundles for a crowd API path,
     then probe a list of likely candidates

Writes one row per venue per run to data.csv.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

SGT = timezone(timedelta(hours=8))
PAGE = "https://activesg.gov.sg/gym-pool-crowd?tab=Pool"
CSV_PATH = os.environ.get("CSV_PATH", "data.csv")

# Which venues to keep. Substring match, case-insensitive.
# Set TRACK_ALL=1 to record every venue in the feed instead.
VENUES = [v.strip() for v in os.environ.get(
    "VENUES", "Delta Swimming,Queenstown Swimming").split(",") if v.strip()]

# The real endpoint, found via DevTools: a tRPC procedure that takes no
# arguments. The input param is superjson's encoding of `undefined`.
TRPC_INPUT = '{"json":null,"meta":{"values":["undefined"]}}'
PROC = "pass.getFacilityCapacities"

CANDIDATES = [
    f"https://activesg.gov.sg/api/trpc/{PROC}?input={quote(TRPC_INPUT)}",
    # Fallbacks in case the router prefix differs from the standard one.
    f"https://activesg.gov.sg/trpc/{PROC}?input={quote(TRPC_INPUT)}",
    f"https://activesg.gov.sg/api/{PROC}?input={quote(TRPC_INPUT)}",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": PAGE,
}

NAME_KEYS = ("name", "venuename", "facilityname", "title", "label", "centre", "center")
CROWD_KEYS = ("crowdlevel", "crowd", "occupancy", "capacity", "load",
              "percentage", "percent", "utilisation", "utilization", "count")

FIELDS = ["ts_sgt", "venue", "level", "pct", "headcount", "capacity"]


def _norm(k):
    return re.sub(r"[^a-z]", "", str(k).lower())


def _to_pct(val):
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        v = float(val)
        if 0 <= v <= 1:
            return round(v * 100, 1)
        if 0 <= v <= 100:
            return round(v, 1)
        return None
    if isinstance(val, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", val)
        if m:
            return round(float(m.group(1)), 1)
        return {"low": 25.0, "moderate": 55.0, "medium": 55.0,
                "high": 80.0, "veryhigh": 95.0, "full": 100.0}.get(_norm(val))
    return None


def extract(obj, out=None):
    """Walk arbitrary JSON and pull out anything shaped like a venue record."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        keys = {_norm(k): k for k in obj}
        name_k = next((keys[k] for k in keys if k in NAME_KEYS), None)
        crowd_k = next((keys[k] for k in keys
                        if any(c in k for c in CROWD_KEYS)), None)
        if name_k and crowd_k and isinstance(obj[name_k], str):
            # Find the descriptive level on its own key — the generic crowd-key
            # scan may well have landed on `capacity` instead.
            level = next((obj[keys[k]] for k in keys
                          if ("level" in k or "status" in k or "crowd" in k)
                          and isinstance(obj[keys[k]], str)), None)
            if level is None and isinstance(obj[crowd_k], str):
                level = obj[crowd_k]
            head = next((obj[keys[k]] for k in keys
                         if k in ("count", "headcount", "current", "occupancy")
                         and isinstance(obj[keys[k]], int)), None)
            cap = next((obj[keys[k]] for k in keys
                        if k in ("capacity", "maxcapacity", "max", "total")
                        and isinstance(obj[keys[k]], int)), None)
            # Exact counts beat the coarse Low/Moderate/High word buckets.
            if head is not None and cap:
                pct = round(100.0 * head / cap, 1)
            else:
                pct = _to_pct(obj[crowd_k])
                if pct is None and level:
                    pct = _to_pct(level)
            if pct is not None or level is not None:
                out.append({"venue": obj[name_k].strip(), "pct": pct,
                            "level": level, "headcount": head, "capacity": cap})
        for v in obj.values():
            extract(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract(v, out)
    return out


def discover(session):
    env = os.environ.get("ACTIVESG_ENDPOINT", "").strip()
    if env:
        print(f"Using ACTIVESG_ENDPOINT: {env}")
        return env

    tried = list(CANDIDATES)
    try:
        html = session.get(PAGE, headers=HEADERS, timeout=25).text
        blobs = [html]
        for c in re.findall(r'src="(/_next/static/[^"]+\.js)"', html)[:40]:
            try:
                blobs.append(session.get("https://activesg.gov.sg" + c,
                                         headers=HEADERS, timeout=25).text)
            except requests.RequestException:
                pass
        found = set()
        for b in blobs:
            for m in re.findall(r'["\'](/(?:api|trpc)/[A-Za-z0-9_\-./]*)["\']', b):
                if any(k in m.lower() for k in ("crowd", "capacity", "occupan", "facilit")):
                    found.add("https://activesg.gov.sg" + m)
        for f in sorted(found):
            if f not in tried:
                tried.insert(0, f)
        print(f"Page scan surfaced {len(found)} candidate path(s).")
    except requests.RequestException as e:
        print(f"Page scan failed: {e}", file=sys.stderr)

    for url in tried:
        try:
            r = session.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                print(f"  {r.status_code}  {url}")
                continue
            if extract(r.json()):
                print(f"  OK   {url}  <-- using this")
                return url
            print(f"  200 but no venue records: {url}")
        except (requests.RequestException, ValueError) as e:
            print(f"  ERR  {url}  ({type(e).__name__})")
    return None


def main():
    session = requests.Session()
    endpoint = discover(session)
    if not endpoint:
        print("\nFAILED: no working endpoint found.\n"
              "Find it manually (browser F12 -> Network -> Fetch/XHR -> reload),\n"
              "then add it as repo variable ACTIVESG_ENDPOINT.", file=sys.stderr)
        sys.exit(1)

    r = session.get(endpoint, headers=HEADERS, timeout=25)
    r.raise_for_status()
    records = extract(r.json())

    if os.environ.get("TRACK_ALL") == "1":
        keep = records
    else:
        keep = [x for x in records
                if any(v.lower() in x["venue"].lower() for v in VENUES)]

    if not keep:
        names = sorted({x["venue"] for x in records})
        print(f"No venue matched {VENUES}. Feed contains {len(names)}:", file=sys.stderr)
        for n in names[:60]:
            print(f"  - {n}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).astimezone(SGT).isoformat(timespec="seconds")
    seen, rows = set(), []
    for x in keep:
        if x["venue"] in seen:
            continue
        seen.add(x["venue"])
        rows.append({"ts_sgt": now, "venue": x["venue"], "level": x["level"],
                     "pct": x["pct"], "headcount": x["headcount"],
                     "capacity": x["capacity"]})

    new_file = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)

    for row in rows:
        val = f"{row['pct']}%" if row["pct"] is not None else row["level"]
        print(f"{row['ts_sgt']}  {row['venue']:<45} {val}")
    print(f"Appended {len(rows)} row(s) to {CSV_PATH}")


if __name__ == "__main__":
    main()
