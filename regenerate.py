#!/usr/bin/env python3
"""Regenerate designmonks.co crawl-status index.html.

Inventory: Webflow API (all collections) + sitemap + core pages.
Status: GSC URL Inspection per URL.
Output: /root/crawlstatus/index.html (ready to commit+push).
"""
import json, os, re, sys, time, urllib.request, ssl
from pathlib import Path
from datetime import datetime

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
WF_TOKEN = (HERMES / "webflow_token.txt").read_text().strip()
GSC_TOKEN = (HERMES / "google_search_console_token.json")
SITE = "672a72b52eb5f37692d645a9"
PROP = "sc-domain:designmonks.co"
BASE = "https://api.webflow.com/v2"

ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE

def wf_get(url):
    req = urllib.request.Request(url, headers={"accept": "application/json", "authorization": f"Bearer {WF_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode())

def fetch_sitemap():
    def get(u):
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 Chrome/120"})
        return urllib.request.urlopen(req, timeout=30, context=ctx).read().decode("utf-8", "ignore")
    xml = get("https://www.designmonks.co/sitemap.xml")
    locs = re.findall(r"<loc>(.*?)</loc>", xml)
    urls = []
    for l in locs:
        if l.endswith(".xml"):
            try: urls += re.findall(r"<loc>(.*?)</loc>", get(l))
            except Exception: pass
        else:
            urls.append(l)
    return urls

def main():
    # 1) sitemap URLs
    smap = fetch_sitemap()
    smap_set = set(u.rstrip("/") for u in smap)
    print(f"sitemap urls: {len(smap_set)}")

    # 2) Webflow collections -> inventory
    colls = wf_get(f"{BASE}/sites/{SITE}/collections")["collections"]
    type_by_slug = {"blog": "Blog", "case-study": "Case Study", "service": "Service",
                    "projects": "Project", "industry": "Industry", "products": "Product",
                    "blog-category": "Blog Category", "projects-categories": "Project Category",
                    "study-categories": "Case Study Category", "product-categories": "Product Category"}
    # only page-producing collections; skip non-page (testimonials, logos, mockups, stacks, authors, teams, careers)
    include = set(type_by_slug) | {"location"}
    inv = {}  # url -> {type, lastUpdated, in_sitemap}
    # location collection not in API list; handle via sitemap core pages instead
    for c in colls:
        cid, cslug = c["id"], c["slug"]
        if cslug not in type_by_slug:
            continue
        ctype = type_by_slug[cslug]
        offset = 0
        while True:
            res = wf_get(f"{BASE}/collections/{cid}/items?offset={offset}&limit=100")
            items = res.get("items", [])
            for it in items:
                fd = it.get("fieldData", {})
                slug = fd.get("slug")
                if not slug: continue
                url = f"https://www.designmonks.co/{'blog/' if cslug=='blogs' else cslug+'/'}{slug}"
                url = url.rstrip("/")
                # skip pagination params
                lu = fd.get("update-date") or fd.get("last-updated") or ""
                inv[url] = {"url": url, "type": ctype, "status": "Not Crawled",
                            "lastCrawlTime": "", "in_sitemap": url in smap_set or url + "/" in smap_set,
                            "lastUpdated": lu}
            if len(items) < 100: break
            offset += 100
        print(f"collection {cslug}: +{offset} items -> total {len(inv)}")

    # 3) core/simple pages from sitemap not already in inventory
    for u in smap:
        nu = u.rstrip("/")
        if nu not in inv and "/blog/" not in nu and "/case-study/" not in nu:
            seg = nu.replace("https://www.designmonks.co", "").strip("/")
            t = "Home" if seg == "" else "Page"
            if seg.startswith("location/"): t = "Location"
            elif seg.startswith("services/"): t = "Service"
            elif seg.startswith("industry/"): t = "Industry"
            inv[nu] = {"url": nu, "type": t, "status": "Not Crawled",
                       "lastCrawlTime": "", "in_sitemap": True, "lastUpdated": ""}
    print(f"final inventory: {len(inv)}")

    # 4) GSC credentials
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(GSC_TOKEN), ["https://www.googleapis.com/auth/webmasters.readonly"])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request()); GSC_TOKEN.write_text(json.dumps(json.loads(creds.to_json()), indent=2))
    svc = build("searchconsole", "v1", credentials=creds)

    # 5) inspect each url
    urls = list(inv.keys())
    for i, u in enumerate(urls):
        try:
            r = svc.urlInspection().index().inspect(body={"inspectionUrl": u, "siteUrl": PROP, "languageCode": "en-US"}).execute()
            ires = (r.get("inspectionResult") or {}).get("indexStatusResult") or {}
            st = ires.get("coverageState") or "Unknown"
            inv[u]["status"] = st
            inv[u]["lastCrawlTime"] = ires.get("lastCrawlTime") or ""
            # normalize statuses to existing filters
        except Exception as e:
            inv[u]["status"] = "Unknown"
        if (i + 1) % 50 == 0: print(f"...inspected {i+1}/{len(urls)}")
        time.sleep(0.25)

    # normalize status strings to the filter labels used in UI
    norm = {
        "Submitted and indexed": "Indexed",
        "Duplicate, Google chose different canonical than user": "Duplicate without user-selected canonical",
        "Crawled - currently not indexed": "Crawled, Not Indexed",
        "Discovered - currently not indexed": "Crawled, Not Indexed",
        "Page with redirect": "Page with redirect",
        "URL is unknown to Google": "Not Crawled",
        "Excluded by 'noindex' tag": "Excluded by noindex",
    }
    for u in inv:
        s = inv[u]["status"]
        inv[u]["status"] = norm.get(s, s)

    rows = sorted(inv.values(), key=lambda r: r["url"])
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"rows: {len(rows)} | statuses: {sorted(set(r['status'] for r in rows))}")

    # 6) build index.html (reuse existing head/JS, only swap ROWS + count + generated)
    html_path = Path("/root/crawlstatus/index.html")
    html = html_path.read_text()
    html = re.sub(r"const ROWS = \[.*?\];\n", "", html, flags=re.S)
    # update generated + count note
    html = re.sub(r"generated [0-9-]+ [0-9:]+", f"generated {gen}", html)
    html = re.sub(r"Live tracker &middot; [0-9]+ pages", f"Live tracker &middot; {len(rows)} pages", html)
    # find script tag: insert ROWS after 'const ROWS' marker removed -> add before 'let sortK'
    rows_js = "const ROWS = " + json.dumps(rows, ensure_ascii=False) + ";\n"
    html = html.replace("let sortK = 'url'", rows_js + "let sortK = 'url'")
    html = re.sub(r"Showing \$\{rows\.length\} of [0-9]+", "{rows.length} of " + str(len(rows)), html)
    html_path.write_text(html, encoding="utf-8")
    print("WROTE index.html", html_path, os.path.getsize(html_path))

if __name__ == "__main__":
    main()