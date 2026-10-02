"""
Rebuild index.html from the latest SEC 13F-HR filings of the managers in managers.json.

For each manager: take the latest original 13F-HR and the one before it, list long stock
positions (options, bonds, preferreds and warrants are left out of the weights), and mark
each position as new / added / trimmed / unchanged by comparing share counts.

Run:  SEC_USER_AGENT="Your Name you@example.com" python scripts/build.py
The SEC asks every automated client to identify itself with a name and contact email.
Use --offline data.json to render from saved data without touching the network.
"""
import json, os, re, sys, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda f: os.path.join(ROOT, f)

DEBT = re.compile(r"\b(NOTE|NOTES|DBCV|SDCV|BOND|BONDS|DEB|DEBT|CONV|SR NT)\b", re.I)
NOT_COMMON = re.compile(r"\b(PFD|PREF|PREFERRED|WARRANT|WARRANTS|WTS?|RIGHTS?|UNIT)\b|DEP SHS|\*W\b|\bW EXP", re.I)
FUND = re.compile(r"ISHARES|SPDR|INVESCO|VANGUARD|SELECT SECTOR|GLOBAL X|\bETF\b|\bTRUST\b|\bFD\b|\bFDS\b|\bFUND\b", re.I)

UA = os.environ.get("SEC_USER_AGENT", "").strip()


def get(url, as_json=True, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            time.sleep(0.15)                       # SEC limit is 10 requests/second
            return json.loads(body) if as_json else body
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def filings(cik):
    """All original 13F-HR filings for a CIK: (period, filed, cik, accession, filer name)."""
    j = get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
    out, blocks = [], [j["filings"]["recent"]]
    for extra in j["filings"].get("files", []):           # older filings live in extra pages
        blocks.append(get("https://data.sec.gov/submissions/" + extra["name"]))
    for r in blocks:
        for form, per, fd, acc in zip(r["form"], r["reportDate"], r["filingDate"], r["accessionNumber"]):
            if form == "13F-HR":
                out.append((per, fd, int(cik), acc, j["name"]))
    return out


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_infotable(xml_bytes):
    root = ET.fromstring(xml_bytes)
    rows = []
    for it in root.iter():
        if local(it.tag) != "infoTable":
            continue
        f = {}
        for el in it.iter():
            f[local(el.tag)] = (el.text or "").strip()
        rows.append({
            "cusip": f.get("cusip", "").upper(), "name": f.get("nameOfIssuer", ""), "cls": f.get("titleOfClass", ""),
            "value": float(f.get("value") or 0), "shares": float(f.get("sshPrnamt") or 0),
            "type": f.get("sshPrnamtType", ""), "pc": f.get("putCall", "").lower(),
        })
    return rows


def holdings(cik, acc):
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
    idx = get(base + "index.json")
    for item in idx["directory"]["item"]:
        n = item["name"]
        if not n.lower().endswith(".xml") or "primary_doc" in n.lower():
            continue
        rows = parse_infotable(get(base + n, as_json=False))
        if rows:
            return rows
    return []


def summarise(rows):
    """Group long stock positions by issuer; return positions, option notional, total."""
    pos, opt = {}, {"put": 0.0, "call": 0.0}
    prices = []
    for r in rows:
        if r["pc"] in opt:
            opt[r["pc"]] += r["value"]; continue
        if r["type"] == "PRN" or DEBT.search(r["cls"]) or NOT_COMMON.search(r["cls"]):
            continue
        key = r["cusip"] if FUND.search(r["name"]) else r["cusip"][:6]
        p = pos.setdefault(key, {"cusip": r["cusip"], "name": r["name"], "value": 0.0, "shares": 0.0})
        p["value"] += r["value"]; p["shares"] += r["shares"]
        if r["shares"] > 0:
            prices.append(r["value"] / r["shares"])
    # Most filers report dollars; some still report thousands. A median "price" under $1 means thousands.
    prices.sort()
    mult = 1000 if prices and prices[len(prices) // 2] < 1 else 1
    for p in pos.values():
        p["value"] *= mult
    return pos, {k: v * mult for k, v in opt.items()}, sum(p["value"] for p in pos.values())


def latest_two(ciks):
    """The two most recent reporting periods, keeping the largest filing when several CIKs report the same period."""
    allf = [f for c in ciks for f in filings(c)]
    periods = sorted({f[0] for f in allf}, reverse=True)[:2]
    picked = []
    for per in periods:
        best = None
        for f in [f for f in allf if f[0] == per]:
            pos, opt, tot = summarise(holdings(f[2], f[3]))
            if not best or tot > best["total"]:
                best = {"period": per, "filed": f[1], "filer": f[4], "pos": pos, "opt": opt, "total": tot}
        picked.append(best)
    return picked


def openfigi(cusips):
    """Look up tickers for CUSIPs we have not seen before (free OpenFIGI API, no key needed)."""
    found = {}
    cusips = list(cusips)
    for i in range(0, len(cusips), 10):
        chunk = cusips[i:i + 10]
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in chunk]).encode()
        req = urllib.request.Request("https://api.openfigi.com/v3/mapping", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
        except Exception as e:
            print("OpenFIGI lookup failed:", e); break
        for c, item in zip(chunk, res):
            data = item.get("data") or []
            us = [d for d in data if d.get("exchCode") == "US"] or data
            if us and us[0].get("ticker"):
                found[c] = us[0]["ticker"].replace("/", "-")
        time.sleep(2.6)                              # 25 requests a minute without a key
    return found


def build_data(managers):
    data = {}
    for m in managers:
        print("Fetching", m["name"])
        two = latest_two(m["ciks"])
        if not two:
            print("  no 13F-HR found"); continue
        cur = two[0]; prev = two[1] if len(two) > 1 else None
        rows = []
        for key, p in sorted(cur["pos"].items(), key=lambda kv: -kv[1]["value"]):
            q = prev["pos"].get(key) if prev else None
            ch = "N" if q is None else (round((p["shares"] / q["shares"] - 1) * 100) if q["shares"] else 0)
            rows.append([p["cusip"], "", re.sub(r"\s+", " ", p["name"])[:30], round(p["value"] / 1e6, 1), int(p["shares"]), ch])
        sold = []
        if prev:
            for key, q in sorted(prev["pos"].items(), key=lambda kv: -kv[1]["value"]):
                if key not in cur["pos"]:
                    sold.append([q["cusip"], "", re.sub(r"\s+", " ", q["name"])[:30], round(q["value"] / 1e6, 1)])
        data[m["key"]] = {
            "f": cur["filer"], "d": cur["filed"], "p": cur["period"], "pp": prev["period"] if prev else None,
            "t": round(cur["total"] / 1e6), "pt": round(prev["total"] / 1e6) if prev else None,
            "o": [round(cur["opt"]["put"] / 1e6), round(cur["opt"]["call"] / 1e6)], "r": rows, "s": sold,
        }
    return data


def add_tickers(data):
    cmap = json.load(open(P("cusip_map.json")))
    unknown = {r[0] for d in data.values() for r in d["r"] + d["s"] if r[0] not in cmap}
    if unknown:
        print(f"Looking up {len(unknown)} new CUSIPs")
        new = openfigi(unknown)
        cmap.update(new)
        json.dump(dict(sorted(cmap.items())), open(P("cusip_map.json"), "w"), indent=0)
    for d in data.values():
        for r in d["r"] + d["s"]:
            r[1] = cmap.get(r[0], "")


def render(data, managers):
    tpl = open(P("template.html")).read()
    meta = {m["key"]: [m["name"], m["firm"]] for m in managers if m["key"] in data}
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    html = (tpl.replace("__DATA__", json.dumps(data, separators=(",", ":")))
               .replace("__META__", json.dumps(meta, separators=(",", ":")))
               .replace("__BUILT__", built))
    open(P("index.html"), "w").write(html)


def main():
    managers = json.load(open(P("managers.json")))["managers"]
    if len(sys.argv) > 2 and sys.argv[1] == "--offline":
        data = json.load(open(sys.argv[2]))
    else:
        if not UA or "@" not in UA:
            sys.exit("Set SEC_USER_AGENT to 'Your Name your@email' (the SEC requires it).")
        data = build_data(managers)
        add_tickers(data)
        json.dump(data, open(P("data.json"), "w"), separators=(",", ":"))
    render(data, managers)
    print("Wrote index.html for", len(data), "managers")


if __name__ == "__main__":
    main()
