from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
HTML = BASE / "index.html"
ENDPOINT = os.environ.get(
    "CVR_ENDPOINT",
    "https://distribution.virk.dk/cvr-permanent/virksomhed/_search",
)
MONTHS_DA = ["Jan", "Feb", "Mar", "Apr", "Maj", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dec"]


def extract_data(text: str):
    marker = "var DATA = "
    start = text.find(marker)
    if start < 0:
        raise RuntimeError("Kunne ikke finde 'var DATA = ' i index.html")
    json_start = start + len(marker)
    decoder = json.JSONDecoder()
    data, consumed = decoder.raw_decode(text[json_start:])
    return data, json_start, json_start + consumed


def as_cvr(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 8 else None


def month_key(label: str):
    m = re.fullmatch(r"([A-Za-zÆØÅæøå]+)\s+(\d{4})", label.strip())
    if not m:
        raise ValueError(f"Ukendt månedslabel: {label}")
    month = MONTHS_DA.index(m.group(1).capitalize()) + 1
    return int(m.group(2)), month


def month_label(year: int, month: int):
    return f"{MONTHS_DA[month - 1]} {year}"


def auth_header(user: str, password: str):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def fetch_batch(cvrs, user, password):
    payload = {
        "size": len(cvrs),
        "query": {"terms": {"Vrvirksomhed.cvrNummer": [int(x) for x in cvrs]}},
        "_source": [
            "Vrvirksomhed.cvrNummer",
            "Vrvirksomhed.maanedsbeskaeftigelse",
            "Vrvirksomhed.erstMaanedsbeskaeftigelse",
        ],
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": auth_header(user, password),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "DAK-ansatte-dashboard/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"CVR API svarede HTTP {exc.code}: {body}") from exc


def employment_map(vr):
    out = {}
    for key in ("maanedsbeskaeftigelse", "erstMaanedsbeskaeftigelse"):
        for row in vr.get(key) or []:
            try:
                year, month = int(row["aar"]), int(row["maaned"])
            except (KeyError, TypeError, ValueError):
                continue
            value = row.get("antalAnsatte")
            if value is None:
                continue
            try:
                out[(year, month)] = int(value)
            except (TypeError, ValueError):
                continue
    return out


def fetch_all(cvrs, user, password):
    result = {}
    batch_size = 200
    for offset in range(0, len(cvrs), batch_size):
        batch = cvrs[offset : offset + batch_size]
        response = fetch_batch(batch, user, password)
        for hit in response.get("hits", {}).get("hits", []):
            vr = (hit.get("_source") or {}).get("Vrvirksomhed") or {}
            cvr = as_cvr(vr.get("cvrNummer"))
            if cvr:
                result[cvr] = employment_map(vr)
    return result


def member_cvrs(item):
    candidates = []
    for key in ("cvrs", "cvrNumre", "cvrnumre", "members", "medlemmer", "ids"):
        value = item.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    single = as_cvr(item.get("id"))
    if single:
        candidates.append(single)
    out = []
    for value in candidates:
        cvr = as_cvr(value.get("id") if isinstance(value, dict) else value)
        if cvr and cvr not in out:
            out.append(cvr)
    return out


def update_series(data, fetched):
    labels = data.get("labels") or []
    if not labels:
        raise RuntimeError("DATA.labels mangler")
    current_keys = [month_key(label) for label in labels]
    current_last = max(current_keys)

    available = sorted({key for series in fetched.values() for key in series if key > current_last})
    if not available:
        return False, labels[-1], 0

    new_labels = [month_label(*key) for key in current_keys + available]
    added = len(available)

    for item in data.get("cvr") or []:
        cvr = as_cvr(item.get("id"))
        values = list(item.get("v") or [])
        if len(values) != len(current_keys):
            raise RuntimeError(f"CVR {cvr or item.get('id')} har {len(values)} værdier mod {len(current_keys)} labels")
        series = fetched.get(cvr, {})
        values.extend(series.get(key) for key in available)
        item["v"] = values

    for item in data.get("koncern") or []:
        values = list(item.get("v") or [])
        if len(values) != len(current_keys):
            raise RuntimeError(f"Koncern {item.get('navn') or item.get('id')} har forkert serielængde")
        members = member_cvrs(item)
        for key in available:
            vals = [fetched.get(cvr, {}).get(key) for cvr in members]
            vals = [v for v in vals if v is not None]
            values.append(sum(vals) if vals else None)
        item["v"] = values

    data["labels"] = new_labels
    return True, new_labels[-1], added


def replace_date_text(text: str, latest_label: str):
    text = re.sub(
        r"(Månedlig beskæftigelse, januar 2020 til )[^<]+",
        rf"\g<1>{latest_label.lower()}",
        text,
        count=1,
    )
    text = re.sub(
        r"([A-ZÆØÅ][a-zæøå]{2} \d{4} er foreløbig og kan være ufuldstændigt indberettet\.)",
        f"{latest_label} er foreløbig og kan være ufuldstændigt indberettet.",
        text,
        count=1,
    )
    return text


def validate(data):
    labels = data.get("labels") or []
    if len(labels) < 70:
        raise RuntimeError("Uventet kort historik")
    if len(data.get("cvr") or []) < 900:
        raise RuntimeError("For få CVR-serier")
    n = len(labels)
    bad = [str(x.get("id")) for x in data.get("cvr") or [] if len(x.get("v") or []) != n]
    if bad:
        raise RuntimeError(f"Uens labels/værdier for CVR: {', '.join(bad[:10])}")
    for item in data.get("koncern") or []:
        if len(item.get("v") or []) != n:
            raise RuntimeError(f"Uens labels/værdier for koncern {item.get('navn') or item.get('id')}")


def main():
    user = os.environ.get("CVR_USER", "").strip()
    password = os.environ.get("CVR_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("CVR_USER og CVR_PASSWORD skal være sat som GitHub Actions secrets")

    text = HTML.read_text(encoding="utf-8")
    data, start, end = extract_data(text)
    cvrs = [as_cvr(x.get("id")) for x in data.get("cvr") or []]
    cvrs = [x for x in cvrs if x]
    if len(cvrs) < 900:
        raise RuntimeError(f"Kun {len(cvrs)} gyldige CVR-numre fundet i dashboardet")

    all_cvrs = list(dict.fromkeys(cvrs + [c for x in data.get("koncern") or [] for c in member_cvrs(x)]))
    fetched = fetch_all(all_cvrs, user, password)
    if len([c for c in cvrs if c in fetched]) < int(len(cvrs) * 0.90):
        raise RuntimeError(f"CVR API returnerede kun {len(fetched)} af {len(cvrs)} centrale CVR-numre")

    changed, latest, added = update_series(data, fetched)
    validate(data)
    if not changed:
        print(f"Ingen nye måneder. Dashboardet står fortsat på {latest}.")
        return 0

    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out = text[:start] + compact + text[end:]
    out = replace_date_text(out, latest)
    HTML.write_text(out, encoding="utf-8")
    print(f"Tilføjede {added} måned(er). Ny seneste periode: {latest}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FEJL: {exc}", file=sys.stderr)
        raise SystemExit(1)
