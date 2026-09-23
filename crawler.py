#!/usr/bin/env python3
"""
adstxt-watch: a weekly crawl of public ads.txt and sellers.json files.

In plain words:
1. Read the publisher websites listed in publishers.txt.
2. Download each site's ads.txt, the public file where a site lists every
   ad-tech company allowed to sell its ad space.
3. Count, for every ad-tech company named, how many sites list it.
4. For the most-listed ad-tech companies, download their sellers.json, the
   public file where an exchange lists every company selling through it.
   Keep the intermediaries (ad networks, resellers): that is where small
   acquisition targets show up by name.
5. Compare with the previous run and write what changed to data/latest-diff.md.

Only public files are read. Results are written into this repository only.
A site or exchange that fails in either run is left out of the comparison,
so a download error can never show up as a company "losing" a publisher.
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "adstxt-watch/1.0 (weekly monitor of public ads.txt and sellers.json files)"
TIMEOUT = 25
ADS_TXT_MAX = 5_000_000          # bytes
SELLERS_MAX = 150_000_000        # bytes; bigger files are skipped and logged
MAX_EXCHANGES = 40               # how many ad systems get a sellers.json read
MIN_PUBLISHERS_FOR_EXCHANGE = 2  # an ad system must be on at least this many sites
KEEP_SNAPSHOTS = 12
RELATIONSHIPS = {"DIRECT", "RESELLER"}
LEGAL_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|gmbh|sas|sa|bv|b\.v|plc|corp|corporation|co|srl|pte|pty|ag|oy|ab)\b\.?", re.I)


# ---------- fetching ----------

class HttpFetcher:
    def __init__(self):
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        self.requests = requests
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        self.s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))

    def get(self, url, max_bytes):
        """Returns (status, text, note). status is ok / error / too_large."""
        try:
            with self.s.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
                if r.status_code != 200:
                    return "error", None, f"HTTP {r.status_code}"
                cl = r.headers.get("Content-Length", "")
                if cl.isdigit() and int(cl) > max_bytes:
                    return "too_large", None, f"{int(cl)} bytes"
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return "too_large", None, f"over {max_bytes} bytes"
                return "ok", buf.decode("utf-8", errors="replace"), r.headers.get("Content-Type", "")
        except self.requests.RequestException as e:
            return "error", None, type(e).__name__


class FixtureFetcher:
    """Test double: serves files from a folder instead of the internet."""
    def __init__(self, folder):
        self.folder = Path(folder)

    def get(self, url, max_bytes):
        m = re.match(r"https://([^/]+)/(ads\.txt|sellers\.json)$", url)
        if not m:
            return "error", None, "bad url"
        host, kind = m.groups()
        sub = "ads" if kind == "ads.txt" else "sellers"
        ext = ".txt" if kind == "ads.txt" else ".json"
        p = self.folder / sub / (host + ext)
        if not p.exists():
            return "error", None, "HTTP 404"
        if p.stat().st_size > max_bytes:
            return "too_large", None, f"{p.stat().st_size} bytes"
        ctype = "text/html" if p.read_text(errors="replace").lstrip().lower().startswith("<") else "text/plain"
        return "ok", p.read_text(encoding="utf-8", errors="replace"), ctype


# ---------- parsing ----------

def normalise_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split(":")[0].strip(". ")
    if d.startswith("www."):
        d = d[4:]
    return d if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d) else ""


def parse_ads_txt(text):
    """Returns (records, variables). A record is [ad_system_domain, account_id, DIRECT|RESELLER]."""
    records, variables = set(), {}
    for raw in text.replace("﻿", "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line and "," not in line:
            k, v = line.split("=", 1)
            variables.setdefault(k.strip().lower(), []).append(v.strip())
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        adsys, rel = normalise_domain(parts[0]), parts[2].upper()
        if adsys and parts[1] and rel in RELATIONSHIPS:
            records.add((adsys, parts[1], rel))
    return [list(r) for r in sorted(records)], variables


def looks_like_html(text, ctype):
    return "html" in (ctype or "").lower() or text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))


def company_key(s):
    if s.get("domain"):
        return s["domain"]
    name = LEGAL_SUFFIX.sub("", (s.get("name") or "").lower())
    name = re.sub(r"[^a-z0-9]+", " ", name).strip()
    return f"name:{name}" if name else ""


# ---------- crawling ----------

def get_ads_txt(fetcher, domain):
    tried = []
    hosts = [domain] if domain.startswith("www.") else [domain, "www." + domain]
    for host in hosts:
        url = f"https://{host}/ads.txt"
        status, text, note = fetcher.get(url, ADS_TXT_MAX)
        if status == "ok" and looks_like_html(text, note):
            status, note = "error", "served a web page, not ads.txt"
        if status == "ok":
            records, variables = parse_ads_txt(text)
            if records:
                return {"status": "ok", "url": url, "records": records, "variables": variables}
            status, note = "error", "no valid records"
        tried.append(f"{url}: {status} ({note})")
    return {"status": "error", "tried": tried}


def get_sellers(fetcher, adsys):
    tried = []
    for host in (adsys, "www." + adsys):
        url = f"https://{host}/sellers.json"
        status, text, note = fetcher.get(url, SELLERS_MAX)
        if status == "too_large":
            return {"status": "too_large", "url": url, "note": note}
        if status == "ok":
            try:
                doc = json.loads(text.lstrip("﻿"))
            except ValueError:
                status, note = "error", "not valid JSON"
            else:
                sellers = doc.get("sellers") if isinstance(doc, dict) else None
                if isinstance(sellers, list):
                    by_type, inter = {}, {}
                    for s in sellers:
                        if not isinstance(s, dict):
                            continue
                        t = str(s.get("seller_type", "")).upper()
                        by_type[t] = by_type.get(t, 0) + 1
                        sid = str(s.get("seller_id", "")).strip()
                        if t in ("INTERMEDIARY", "BOTH") and sid:
                            inter[sid] = {
                                "name": str(s.get("name") or "").strip()[:200],
                                "domain": normalise_domain(str(s.get("domain") or "")),
                                "type": t,
                            }
                    return {"status": "ok", "url": url, "total_sellers": len(sellers),
                            "by_type": by_type, "intermediaries": inter}
                status, note = "error", "no sellers list"
        tried.append(f"{url}: {status} ({note})")
    return {"status": "error", "tried": tried}


def load_publishers(path):
    out = []
    for line in Path(path).read_text().splitlines():
        d = normalise_domain(line.split("#", 1)[0])
        if d and d not in out:
            out.append(d)
    return out


def crawl(fetcher, publishers, keep_exchanges=(), workers=8):
    with ThreadPoolExecutor(workers) as ex:
        pubs = dict(zip(publishers, ex.map(lambda d: get_ads_txt(fetcher, d), publishers)))
    counts = {}
    for p, v in pubs.items():
        if v["status"] == "ok":
            for adsys in {r[0] for r in v["records"]}:
                counts[adsys] = counts.get(adsys, 0) + 1
    exchanges = [a for a, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
                 if n >= MIN_PUBLISHERS_FOR_EXCHANGE][:MAX_EXCHANGES]
    # always re-read every exchange read last time, so one that loses publishers stays comparable
    exchanges += [a for a in keep_exchanges if a not in exchanges]
    with ThreadPoolExecutor(max(1, workers // 2)) as ex:
        sellers = dict(zip(exchanges, ex.map(lambda a: get_sellers(fetcher, a), exchanges)))
    return {"publishers": pubs, "sellers": sellers}


# ---------- comparing ----------

def ok_keys(d):
    return {k for k, v in d.items() if v.get("status") == "ok"}


def adsys_map(snap, pubs):
    m = {}
    for p in pubs:
        for adsys in {r[0] for r in snap["publishers"][p]["records"]}:
            m.setdefault(adsys, set()).add(p)
    return m


def company_map(snap, exchanges):
    m = {}
    for ex in exchanges:
        for s in snap["sellers"][ex]["intermediaries"].values():
            k = company_key(s)
            if not k:
                continue
            c = m.setdefault(k, {"name": s["name"], "domain": s["domain"], "exchanges": set(), "listings": 0})
            c["exchanges"].add(ex)
            c["listings"] += 1
            c["name"] = c["name"] or s["name"]
    return m


def compare(prev, cur):
    pubs = sorted(ok_keys(prev["publishers"]) & ok_keys(cur["publishers"]))
    exs = sorted(ok_keys(prev["sellers"]) & ok_keys(cur["sellers"]))
    a0, a1 = adsys_map(prev, pubs), adsys_map(cur, pubs)
    adsys_changes = []
    for a in sorted(set(a0) | set(a1)):
        s0, s1 = a0.get(a, set()), a1.get(a, set())
        if s0 != s1:
            adsys_changes.append({"adsys": a, "before": len(s0), "after": len(s1),
                                  "lost": sorted(s0 - s1), "gained": sorted(s1 - s0)})
    c0, c1 = company_map(prev, exs), company_map(cur, exs)
    company_changes = []
    for k in sorted(set(c0) | set(c1)):
        e0 = c0.get(k, {}).get("exchanges", set())
        e1 = c1.get(k, {}).get("exchanges", set())
        l0 = c0.get(k, {}).get("listings", 0)
        l1 = c1.get(k, {}).get("listings", 0)
        if e0 != e1 or l0 != l1:
            ref = c1.get(k) or c0.get(k)
            company_changes.append({"key": k, "name": ref["name"], "domain": ref["domain"],
                                    "exchanges_before": len(e0), "exchanges_after": len(e1),
                                    "listings_before": l0, "listings_after": l1,
                                    "dropped_from": sorted(e0 - e1), "added_to": sorted(e1 - e0)})
    exchange_changes = []
    for ex in exs:
        t0, t1 = prev["sellers"][ex]["total_sellers"], cur["sellers"][ex]["total_sellers"]
        if t0 != t1:
            exchange_changes.append({"exchange": ex, "before": t0, "after": t1})
    return {"compared_publishers": len(pubs), "compared_exchanges": len(exs),
            "adsys": adsys_changes, "companies": company_changes, "exchanges": exchange_changes}


# ---------- writing ----------

def coverage_line(snap):
    p, s = snap["publishers"], snap["sellers"]
    big = sorted(k for k, v in s.items() if v["status"] == "too_large")
    line = (f"Publishers read OK: {len(ok_keys(p))} of {len(p)}. "
            f"Exchange sellers.json read OK: {len(ok_keys(s))} of {len(s)}.")
    if big:
        line += f" Skipped as too large: {', '.join(big)}."
    return line


def table(rows, headers):
    if not rows:
        return "_None this run._\n"
    out = "| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n"
    for r in rows:
        out += "| " + " | ".join(str(x).replace("|", "/") for x in r) + " |\n"
    return out


def render_diff(date, prev_date, cur, d):
    md = [f"# ads.txt watch: {date} compared with {prev_date}\n",
          f"{coverage_line(cur)} Compared on the {d['compared_publishers']} publishers and "
          f"{d['compared_exchanges']} exchanges that read OK in both runs, so download errors "
          f"never count as losses.\n"]
    drops = sorted([c for c in d["adsys"] if c["after"] < c["before"]], key=lambda c: c["after"] - c["before"])
    gains = sorted([c for c in d["adsys"] if c["after"] > c["before"]], key=lambda c: c["before"] - c["after"])
    gone = [c for c in d["companies"] if c["exchanges_after"] == 0]
    new = [c for c in d["companies"] if c["exchanges_before"] == 0]
    shrink = sorted([c for c in d["companies"] if 0 < c["exchanges_after"] < c["exchanges_before"]],
                    key=lambda c: c["exchanges_after"] - c["exchanges_before"])
    md.append("## Ad-tech companies that lost publishers\n")
    md.append(table([[c["adsys"], c["before"], c["after"], ", ".join(c["lost"])] for c in drops[:40]],
                    ["ad system", "publishers before", "after", "lost"]))
    md.append("\n## Intermediaries gone from every exchange we read\n")
    md.append(table([[c["name"] or "?", c["domain"] or "-", c["exchanges_before"], ", ".join(c["dropped_from"])] for c in gone[:60]],
                    ["name", "domain", "exchanges before", "dropped from"]))
    md.append("\n## Intermediaries dropped by some exchanges\n")
    md.append(table([[c["name"] or "?", c["domain"] or "-", c["exchanges_before"], c["exchanges_after"], ", ".join(c["dropped_from"])] for c in shrink[:60]],
                    ["name", "domain", "exchanges before", "after", "dropped from"]))
    md.append("\n## New intermediaries\n")
    md.append(table([[c["name"] or "?", c["domain"] or "-", c["exchanges_after"], ", ".join(c["added_to"])] for c in new[:60]],
                    ["name", "domain", "exchanges", "added to"]))
    md.append("\n## Exchanges whose seller count changed\n")
    md.append(table([[e["exchange"], e["before"], e["after"], e["after"] - e["before"]] for e in sorted(d["exchanges"], key=lambda e: e["after"] - e["before"])],
                    ["exchange", "sellers before", "after", "change"]))
    md.append("\n## Ad-tech companies that gained publishers\n")
    md.append(table([[c["adsys"], c["before"], c["after"], ", ".join(c["gained"])] for c in gains[:40]],
                    ["ad system", "publishers before", "after", "gained"]))
    return "\n".join(md)


def render_baseline(date, cur):
    return (f"# ads.txt watch: baseline {date}\n\n{coverage_line(cur)}\n\n"
            "First run, so there is nothing to compare yet. The first week-on-week "
            "changes appear after the next run. Current footprint is in "
            "data/adsystems.csv and data/intermediaries.csv.\n")


def write_state_csvs(data_dir, cur):
    pubs = sorted(ok_keys(cur["publishers"]))
    am = adsys_map(cur, pubs)
    with open(data_dir / "adsystems.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ad_system", "publishers", "publisher_list"])
        for a, s in sorted(am.items(), key=lambda x: (-len(x[1]), x[0])):
            w.writerow([a, len(s), " ".join(sorted(s))])
    cm = company_map(cur, sorted(ok_keys(cur["sellers"])))
    with open(data_dir / "intermediaries.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "domain", "exchanges", "listings", "exchange_list"])
        for c in sorted(cm.values(), key=lambda c: (-len(c["exchanges"]), c["name"])):
            w.writerow([c["name"], c["domain"], len(c["exchanges"]), c["listings"], " ".join(sorted(c["exchanges"]))])


def run(fetcher, publishers_file, data_dir, date):
    data_dir = Path(data_dir)
    snaps, hist = data_dir / "snapshots", data_dir / "history"
    snaps.mkdir(parents=True, exist_ok=True)
    hist.mkdir(parents=True, exist_ok=True)
    earlier = sorted(p for p in snaps.glob("*.json.gz") if p.name[:10] < date)
    prev = None
    if earlier:
        with gzip.open(earlier[-1], "rt", encoding="utf-8") as f:
            prev = json.load(f)
    cur = crawl(fetcher, load_publishers(publishers_file), keep_exchanges=sorted(prev["sellers"]) if prev else ())
    cur["date"] = date
    with gzip.open(snaps / f"{date}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(cur, f)
    if prev:
        d = compare(prev, cur)
        md = render_diff(date, prev["date"], cur, d)
        (data_dir / "latest-diff.json").write_text(json.dumps({"date": date, "previous": prev["date"], **d}, indent=1))
    else:
        md = render_baseline(date, cur)
        (data_dir / "latest-diff.json").write_text(json.dumps({"date": date, "previous": None}, indent=1))
    (data_dir / "latest-diff.md").write_text(md)
    (hist / f"{date}.md").write_text(md)
    write_state_csvs(data_dir, cur)
    errors = {k: v.get("tried") or v.get("note") for part in ("publishers", "sellers")
              for k, v in cur[part].items() if v["status"] != "ok"}
    (data_dir / "latest-run.json").write_text(json.dumps({"date": date, "coverage": coverage_line(cur), "errors": errors}, indent=1))
    for old in sorted(snaps.glob("*.json.gz"))[:-KEEP_SNAPSHOTS]:
        old.unlink()
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publishers", default=str(ROOT / "publishers.txt"))
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--date", default=dt.datetime.utcnow().strftime("%Y-%m-%d"))
    ap.add_argument("--fixtures", help="read files from this folder instead of the internet (tests only)")
    a = ap.parse_args()
    fetcher = FixtureFetcher(a.fixtures) if a.fixtures else HttpFetcher()
    md = run(fetcher, a.publishers, a.data, a.date)
    print(md.splitlines()[0])
    print(coverage_line(json.load(gzip.open(Path(a.data) / "snapshots" / f"{a.date}.json.gz", "rt"))))


if __name__ == "__main__":
    sys.exit(main())
