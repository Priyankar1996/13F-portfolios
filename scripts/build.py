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


def summary(old, new, managers):
    """Markdown note listing managers whose filing changed since the last build, for the email."""
    lines = []
    for m in managers:
        k = m["key"]; d = new.get(k)
        if not d or (old.get(k, {}).get("d") == d["d"] and old.get(k, {}).get("p") == d["p"]):
            continue
        tot = sum(r[3] for r in d["r"]) or 1
        tag = lambda r: f"{r[1] or r[2]} ({r[3] / tot:.1%})"
        new_pos = [tag(r) for r in d["r"] if r[5] == "N"][:6]
        adds = sorted([r for r in d["r"] if isinstance(r[5], int) and r[5] >= 20], key=lambda r: -r[3])[:6]
        exits = [x[1] or x[2] for x in d["s"]][:6]
        lines.append(f"### {m['name']} ({m['firm']})")
        lines.append(f"Holdings as of {d['p']}, filed {d['d']}: {len(d['r'])} positions, ${d['t'] / 1000:,.1f}B reported.")
        lines.append(f"- Top holdings: {', '.join(tag(r) for r in d['r'][:5])}")
        if new_pos: lines.append(f"- New: {', '.join(new_pos)}")
        if adds: lines.append(f"- Added 20%+: {', '.join(f'{r[1] or r[2]} (+{r[5]}%)' for r in adds)}")
        if exits: lines.append(f"- Sold out: {', '.join(exits)}")
        lines.append("")
    return "\n".join(lines)


BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PRICE_TIMEOUT = 10          # seconds per request
PRICE_BUDGET = 8 * 60       # stop fetching prices after this many seconds and keep what we have


def _get_text(url, extra=None):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*", **(extra or {})})
    with urllib.request.urlopen(req, timeout=PRICE_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_closes(ticker, start, host="query1"):
    """Daily closes adjusted for splits and dividends: {date: price}."""
    import datetime as dt
    p1 = int(dt.datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    res = json.loads(_get_text(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
                               f"?period1={p1}&period2={p2}&interval=1d&events=div,split"))["chart"]["result"][0]
    ts = res.get("timestamp") or []
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or res["indicators"]["quote"][0]["close"]
    return {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"): v for t, v in zip(ts, adj) if v}


def yahoo2_closes(ticker, start):
    return yahoo_closes(ticker, start, "query2")


def nasdaq_closes(ticker, start):
    """Nasdaq.com historical prices (not dividend-adjusted)."""
    out = {}
    for cls in ("stocks", "etf"):
        j = json.loads(_get_text(f"https://api.nasdaq.com/api/quote/{ticker.replace('-', '.')}/historical"
                                 f"?assetclass={cls}&fromdate={start}&limit=9999",
                                 {"Accept": "application/json", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}))
        rows = (((j or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
        for r in rows:
            m, d, y = r["date"].split("/")
            v = float(r["close"].replace("$", "").replace(",", ""))
            out[f"{y}-{m}-{d}"] = v
        if out:
            break
    return out


def stooq_closes(ticker, start):
    lines = _get_text(f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d&d1={start.replace('-', '')}").strip().splitlines()
    out = {}
    for ln in lines[1:]:
        f = ln.split(",")
        if len(f) >= 5 and f[4] not in ("", "null"):
            try: out[f[0]] = float(f[4])
            except ValueError: pass
    return out


def fetch_prices(data, old=None):
    """Daily prices for every holding since the earliest quarter-end on the page, plus SPY as a benchmark.
    Each source is tried once on SPY first; sources that don't answer are skipped for every stock, so a
    blocked site costs seconds, not hours. If nothing works, the previous prices are kept."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import date, timedelta
    # start at the beginning of the quarter each filing covers (the previous quarter-end), so the page
    # can show the price range during the quarter when the shares were bought
    starts = [d.get("pp") or (date.fromisoformat(d["p"]) - timedelta(days=92)).isoformat() for d in data.values() if d.get("p")]
    if not starts:
        return old or {}
    start = (date.fromisoformat(min(starts)) - timedelta(days=7)).isoformat()
    sources = []
    for name, fn in (("Yahoo", yahoo_closes), ("Yahoo (query2)", yahoo2_closes), ("Nasdaq", nasdaq_closes), ("Stooq", stooq_closes)):
        t0 = time.time()
        try:
            ok = len(fn("SPY", start)) > 20
        except Exception as e:
            ok = False; print(f"  {name}: unavailable ({type(e).__name__}: {str(e)[:80]})")
        if ok:
            sources.append(fn); print(f"  {name}: working ({time.time() - t0:.1f}s)")
    if not sources:
        print("No price source reachable; keeping the previous prices.")
        return old or {}
    tickers = sorted({r[1] for d in data.values() for r in d["r"] if r[1] and " " not in r[1]} | {"SPY"})
    deadline = time.time() + PRICE_BUDGET

    def one(t):
        for fn in sources:
            if time.time() > deadline:
                return t, None
            try:
                got = fn(t, start)
                if got: return t, got
            except Exception:
                pass
        return t, None

    series, failed = {}, []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for fut in as_completed([ex.submit(one, t) for t in tickers]):
            t, got = fut.result()
            if got: series[t] = got
            else: failed.append(t)
    if failed:
        print(f"No prices for {len(failed)} tickers: {', '.join(sorted(failed))}")
    if "SPY" not in series and old:
        return old
    days = sorted(series.get("SPY") or {d for s in series.values() for d in s})
    sig = lambda v: float(f"{v:.5g}")
    return {"dates": days, "px": {t: [sig(s[d]) if d in s else None for d in days] for t, s in series.items()}}


def render(data, managers, prices=None):
    tpl = open(P("template.html")).read()
    meta = {m["key"]: [m["name"], m["firm"]] for m in managers if m["key"] in data}
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    html = (tpl.replace("__DATA__", json.dumps(data, separators=(",", ":")))
               .replace("__META__", json.dumps(meta, separators=(",", ":")))
               .replace("__BUILT__", built)
               .replace("__PRICES__", json.dumps(prices or {}, separators=(",", ":"))))
    open(P("index.html"), "w").write(html)


def main():
    managers = json.load(open(P("managers.json")))["managers"]
    if len(sys.argv) > 2 and sys.argv[1] == "--offline":
        data = json.load(open(sys.argv[2]))
        prices = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else {}
    else:
        if not UA or "@" not in UA:
            sys.exit("Set SEC_USER_AGENT to 'Your Name your@email' (the SEC requires it).")
        old = json.load(open(P("data.json"))) if os.path.exists(P("data.json")) else {}
        data = build_data(managers)
        add_tickers(data)
        json.dump(data, open(P("data.json"), "w"), separators=(",", ":"))
        note = summary(old, data, managers)
        with open(P("update_summary.md"), "w") as f:   # read by the workflow to send the email; not committed
            f.write(note)
        print(note or "No new filings since the last build.")
        old_px = json.load(open(P("prices.json"))) if os.path.exists(P("prices.json")) else None
        print("Fetching prices")
        prices = fetch_prices(data, old_px)
        json.dump(prices, open(P("prices.json"), "w"), separators=(",", ":"))
        print("Prices for", len(prices.get("px", {})), "tickers through", (prices.get("dates") or ["?"])[-1])
    render(data, managers, prices)
    print("Wrote index.html for", len(data), "managers")


if __name__ == "__main__":
    main()
