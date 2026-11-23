import gzip
import json
import random
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

try:
    from checksum import extract_vat_candidates
except ImportError:
    from src.checksum import extract_vat_candidates

DB_PATH = Path("data/pipeline.db")
BATCH_LIMIT = 60

BLOCKED_DOMAINS = {
    "gov.uk",
    "companieshouse.gov.uk",
    "endole.co.uk",
    "companypulse.co.uk",
    "bizstats.co.uk",
    "globaldatabase.com",
    "kompass.com",
    "cylex-uk.co.uk",
    "scoot.co.uk",
    "freeindex.co.uk",
    "thomsonlocal.com",
    "checkatrade.com",
    "yell.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "opencorporates.com",
    "companieslist.co.uk",
    "companycheck.co.uk",
    "duedil.com",
    "creditsafe.com",
    "experian.co.uk",
    "bloomberg.com",
    "wikipedia.org",
    "trustpilot.com",
    "glassdoor.co.uk",
    "glassdoor.com",
    "indeed.com",
    "amazon.co.uk",
    "ebay.co.uk",
    "192.com",
    "bizdb.co.uk",
    "ukdata.com",
    "pomanda.com",
    "dnb.com",
    "zoominfo.com",
    "apollo.io",
    "rocketreach.co",
    "owler.com",
    "crunchbase.com",
    "pitchbook.com",
}

LEGAL_KEYWORDS = re.compile(
    r"contact|about|term|privac|legal|condition|polic|imprint|deliver|ship|custom|vat|tax",
    re.IGNORECASE,
)


def reset_previous_web_run(conn: sqlite3.Connection) -> None:
    """Clears v1 web scan logs and web candidates so we get clean v2 metrics."""
    conn.execute("DROP TABLE IF EXISTS web_scans")
    conn.execute("DELETE FROM candidates WHERE source_method IN ('LIVE_WEB_CRAWL', 'COMMON_CRAWL_WEB', 'SEARCH_SNIPPET')")
    conn.execute(
        """
        CREATE TABLE web_scans (
            company_number TEXT PRIMARY KEY,
            discovered_domain TEXT,
            discovery_status TEXT,
            pages_checked INTEGER DEFAULT 0,
            candidates_found INTEGER DEFAULT 0,
            scanned_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def clean_company_tokens(company_name: str) -> list[str]:
    cleaned = re.sub(
        r"\b(LIMITED|LTD|LLP|PLC|UK|THE|AND|GROUP|HOLDINGS|SERVICES|INTERNATIONAL)\b",
        " ",
        company_name.upper(),
    )
    return [t for t in re.findall(r"[A-Z0-9]{3,}", cleaned)]


def is_blocked_domain(netloc: str) -> bool:
    host = netloc.lower().removeprefix("www.")
    return any(host == b or host.endswith("." + b) for b in BLOCKED_DOMAINS)


def page_belongs_to_other_company(text: str, target_crn: str) -> bool:
    """
    Guardrail: If a page explicitly prints 'Company No. 05431902' and that number
    is NOT our target company's CRN, reject it (prevents directory/agency footer leaks).
    """
    crn_mentions = re.findall(
        r"(?:company\s*(?:no|number|reg(?:istration)?)|registered\s*(?:no|number))[^\w]{0,10}([A-Z0-9]{8})\b",
        text,
        re.IGNORECASE,
    )
    if not crn_mentions:
        return False
    normalized = {c.upper() for c in crn_mentions}
    return target_crn.upper() not in normalized


def search_company_and_vat_urls(company_name: str, postcode: str) -> tuple[str | None, list[str], list[str]]:
    """
    Runs two targeted queries:
      1) Find the company's official domain
      2) Search directly for '"Company Name" VAT' to catch deep subpages & snippets
    Returns: (official_domain, extra_urls_to_crawl, search_snippets)
    """
    tokens = clean_company_tokens(company_name)
    if not tokens:
        return None, [], []

    ddgs = DDGS()
    official_domain: str | None = None
    extra_urls: list[str] = []
    snippets: list[str] = []

    # Query 1: Find official website
    q_site = f'"{company_name}" {postcode or "UK"}'.strip()
    try:
        res_site = list(ddgs.text(q_site, region="uk-en", max_results=6))
    except Exception:
        res_site = []

    for res in res_site:
        href = res.get("href", "")
        title = res.get("title", "").upper()
        body = res.get("body", "").upper()
        parsed = urlparse(href)
        host = parsed.netloc.lower().removeprefix("www.")
        if not host or is_blocked_domain(host):
            continue

        domain_alpha = re.sub(r"[^A-Z0-9]", "", host.upper())
        if any(t in domain_alpha for t in tokens) or any(t in title or t in body for t in tokens[:2]):
            official_domain = f"{parsed.scheme or 'https'}://{parsed.netloc}"
            break

    # Query 2: Direct VAT search (either on the discovered site or across the open web)
    if official_domain:
        host = urlparse(official_domain).netloc.removeprefix("www.")
        q_vat = f'site:{host} "VAT"'
    else:
        q_vat = f'"{company_name}" "VAT"'

    try:
        res_vat = list(ddgs.text(q_vat, region="uk-en", max_results=5))
    except Exception:
        res_vat = []

    for res in res_vat:
        href = res.get("href", "")
        body = res.get("body", "")
        title = res.get("title", "")
        parsed = urlparse(href)
        host = parsed.netloc.lower().removeprefix("www.")
        if not host or is_blocked_domain(host):
            continue

        # Only accept off-domain results if the company name actually appears in the snippet/title
        combined_upper = (title + " " + body).upper()
        if official_domain or all(t in combined_upper for t in tokens[:2]):
            if body:
                snippets.append(f"{title} — {body}")
            if href and not href.lower().endswith(".pdf") and href not in extra_urls:
                extra_urls.append(href)
            if not official_domain:
                official_domain = f"{parsed.scheme or 'https'}://{parsed.netloc}"

    return official_domain, extra_urls[:3], snippets


def get_latest_cc_cdx_endpoint(client: httpx.Client) -> str | None:
    try:
        resp = client.get("https://index.commoncrawl.org/collinfo.json", timeout=10.0)
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]["cdx-api"]
    except Exception:
        pass
    return None


def fetch_from_common_crawl(client: httpx.Client, cdx_api: str, domain: str) -> list[tuple[str, str]]:
    host = urlparse(domain).netloc.removeprefix("www.")
    pages: list[tuple[str, str]] = []
    try:
        resp = client.get(
            cdx_api,
            params={"url": f"{host}/*", "output": "json", "limit": 5, "filter": "status:200"},
            timeout=12.0,
        )
        if resp.status_code != 200 or not resp.text.strip():
            return pages

        for line in resp.text.strip().splitlines()[:3]:
            rec = json.loads(line)
            warc_url = f"https://data.commoncrawl.org/{rec['filename']}"
            offset, length = int(rec["offset"]), int(rec["length"])
            warc_resp = client.get(
                warc_url,
                headers={"Range": f"bytes={offset}-{offset + length - 1}"},
                timeout=12.0,
            )
            if warc_resp.status_code in (200, 206):
                raw_warc = gzip.decompress(warc_resp.content).decode("utf-8", errors="ignore")
                parts = raw_warc.split("\r\n\r\n", 2)
                pages.append((f"CC:{rec['url']}", parts[2] if len(parts) == 3 else raw_warc))
    except Exception:
        pass
    return pages


def crawl_target_pages(
    client: httpx.Client, base_url: str, extra_urls: list[str], cdx_api: str | None
) -> list[tuple[str, str]]:
    fetched: list[tuple[str, str]] = []
    visited: set[str] = set()

    def try_get(url: str) -> str | None:
        if url in visited:
            return None
        visited.add(url)
        try:
            r = client.get(url, follow_redirects=True, timeout=9.0)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                fetched.append((str(r.url), r.text))
                return r.text
        except Exception:
            pass
        return None

    home_html = try_get(base_url)
    for u in extra_urls:
        try_get(u)

    if home_html:
        soup = BeautifulSoup(home_html, "lxml")
        base_host = urlparse(base_url).netloc.removeprefix("www.")
        sub_urls: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            link_text = a.get_text(" ", strip=True)
            if LEGAL_KEYWORDS.search(href) or LEGAL_KEYWORDS.search(link_text):
                full_url = urljoin(base_url, href).split("#")[0]
                parsed = urlparse(full_url)
                if (
                    parsed.scheme in ("http", "https")
                    and parsed.netloc.removeprefix("www.") == base_host
                    and full_url not in visited
                    and not full_url.lower().endswith((".jpg", ".png", ".pdf", ".zip"))
                    and full_url not in sub_urls
                ):
                    sub_urls.append(full_url)
        for sub_url in sub_urls[:4]:
            try_get(sub_url)
    elif cdx_api:
        fetched.extend(fetch_from_common_crawl(client, cdx_api, base_url))

    return fetched


def get_context_snippet(text: str, needle: str, window: int = 55) -> str:
    idx = text.lower().find(needle.lower())
    if idx == -1:
        return needle
    start = max(0, idx - window)
    end = min(len(text), idx + len(needle) + window)
    return " ".join(text[start:end].split())


def run_web_discovery(limit: int = BATCH_LIMIT) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Keep all previous results and pick 25 SMALL, 20 MICRO, and 15 DORMANT companies not yet scanned
    pending: list[tuple] = []
    for condition, quota in [
        ("UPPER(c.account_category) LIKE '%SMALL%'", 25),
        ("UPPER(c.account_category) LIKE '%MICRO%'", 20),
        ("UPPER(c.account_category) LIKE '%DORMANT%'", 15),
    ]:
        rows = cur.execute(
            f"""
            SELECT c.company_number, c.company_name, c.postcode, c.account_category
            FROM companies c
            LEFT JOIN web_scans w ON c.company_number = w.company_number
            WHERE w.company_number IS NULL AND {condition}
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (quota,),
        ).fetchall()
        pending.extend(rows)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    with httpx.Client(headers=headers, verify=False) as client:
        cdx_api = get_latest_cc_cdx_endpoint(client)
        print(f"Starting v2 open-web discovery on {len(pending)} companies...")
        print(f"Common Crawl fallback: {cdx_api or 'Unavailable'}\n")

        domains_found = 0
        companies_with_vat = 0

        for idx, (crn, name, postcode, acc_cat) in enumerate(pending, start=1):
            domain, extra_urls, snippets = search_company_and_vat_urls(name, postcode or "")
            found_for_company = 0
            pages_count = 0

            # 1. Check search snippets directly first (catches PDFs & deep pages without crawling)
            for snip in snippets:
                if page_belongs_to_other_company(snip, crn):
                    continue
                for cand in extract_vat_candidates(snip):
                    found_for_company += 1
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO candidates
                        (company_number, vat_number, prefix, source_method, source_detail, checksum_algo, raw_snippet)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (crn, cand.vat_number, cand.prefix, "SEARCH_SNIPPET", domain or "DDG_SNIPPET", cand.checksum_algorithm, snip[:180]),
                    )
                    print(
                        f"[{idx}/{len(pending)}] HIT (Snippet): {crn} | {name} ({acc_cat})\n"
                        f"   VAT:  {cand.prefix}{cand.vat_number} ({cand.source_type}, {cand.checksum_algorithm})\n"
                        f"   Text: \"{snip[:150]}...\"\n"
                    )

            # 2. Crawl homepage + VAT search hit URLs + legal/contact subpages
            if domain:
                domains_found += 1
                pages = crawl_target_pages(client, domain, extra_urls, cdx_api)
                pages_count = len(pages)

                for page_url, html in pages:
                    visible_text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
                    if page_belongs_to_other_company(visible_text, crn):
                        continue

                    for cand in extract_vat_candidates(visible_text):
                        found_for_company += 1
                        snippet = get_context_snippet(visible_text, cand.raw_match)
                        method = "COMMON_CRAWL_WEB" if page_url.startswith("CC:") else "LIVE_WEB_CRAWL"
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO candidates
                            (company_number, vat_number, prefix, source_method, source_detail, checksum_algo, raw_snippet)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (crn, cand.vat_number, cand.prefix, method, page_url, cand.checksum_algorithm, snippet),
                        )
                        print(
                            f"[{idx}/{len(pending)}] HIT (Page): {crn} | {name} ({acc_cat})\n"
                            f"   Site: {page_url}\n"
                            f"   VAT:  {cand.prefix}{cand.vat_number} ({cand.source_type}, {cand.checksum_algorithm})\n"
                            f"   Text: \"...{snippet}...\"\n"
                        )

            if found_for_company > 0:
                companies_with_vat += 1

            status = (
                "VAT_FOUND"
                if found_for_company > 0
                else ("CRAWLED_NO_VAT" if pages_count > 0 else ("NO_WEBSITE_FOUND" if not domain else "SITE_UNREACHABLE"))
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO web_scans
                (company_number, discovered_domain, discovery_status, pages_checked, candidates_found)
                VALUES (?, ?, ?, ?, ?)
                """,
                (crn, domain, status, pages_count, found_for_company),
            )
            conn.commit()

            if idx % 20 == 0:
                print(f"...Progress: {idx}/{len(pending)} checked | Websites found: {domains_found} | Companies with VAT: {companies_with_vat}")

            time.sleep(random.uniform(0.9, 1.6))

    conn.close()
    print("-" * 60)
    print(f"v2 Batch complete! Checked {len(pending)} companies.")
    print(f"Websites discovered:        {domains_found} / {len(pending)} ({domains_found/len(pending)*100:.1f}%)")
    print(f"Companies with VAT found:   {companies_with_vat} / {len(pending)} ({companies_with_vat/len(pending)*100:.1f}%)")


if __name__ == "__main__":
    run_web_discovery(BATCH_LIMIT)