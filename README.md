# Superinvestor Portfolios

A one-page site showing the latest SEC 13F holdings of nine managers (Druckenmiller, Tepper, Ackman, Buffett, Klarman, Einhorn, Li Lu, Loeb, Pabrai), what changed since their previous filing, and which stocks they hold in common.

The page rebuilds itself: a GitHub Action checks the SEC every day while 13F filings are arriving (10th–25th of February, May, August and November) and commits a new `index.html` when anything changed. GitHub Pages serves it.

## One-time setup

1. **Upload these files** to a new repository (keep the folder structure, including `.github/workflows`).
2. **Add your SEC contact.** Settings → Secrets and variables → Actions → New repository secret.
   Name: `SEC_USER_AGENT`. Value: your name and email, e.g. `Jane Doe jane@example.com`.
   The SEC requires every automated request to identify a contact; it is only sent to sec.gov.
3. **Turn on Pages.** Settings → Pages → Source: *Deploy from a branch* → Branch: `main`, folder `/ (root)` → Save.
4. **Run it once.** Actions tab → *Update 13F portfolios* → *Run workflow*. When it finishes (about 3–5 minutes), the site is at `https://<your-username>.github.io/<repo-name>/`.

## Changing the managers

Edit `managers.json`. Each entry needs the manager's SEC CIK number(s); find them at https://www.sec.gov/edgar/search (search the firm name, the CIK is in the result). List more than one CIK when a firm has filed under different entities; the larger filing for each quarter is used.

## Files

- `index.html` – the page (generated; don't edit by hand)
- `template.html` – page design; edit this to change the look
- `scripts/build.py` – fetches filings and writes `index.html` and `data.json`
- `cusip_map.json` – CUSIP → ticker lookup; new CUSIPs are filled in automatically via the free OpenFIGI API
- `managers.json` – who is tracked

Run locally: `SEC_USER_AGENT="Your Name you@example.com" python scripts/build.py`

Data comes from public SEC filings. A 13F shows long US-listed positions only, 45 days after quarter end. Not investment advice.
