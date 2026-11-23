import os
import re
import sqlite3
import time
from pathlib import Path
import httpx
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

DB_PATH = Path("data/pipeline.db")
RESULTS_CSV_PATH = Path("data/sample_results.csv")


def test_hmrc_sandbox_oauth() -> bool:
    """
    Demonstrates live OAuth2 client_credentials authentication against HMRC's
    VAT (Registered Companies) API v2.0 Sandbox using the .env credentials.
    """
    client_id = os.getenv("HMRC_CLIENT_ID", "").strip()
    client_secret = os.getenv("HMRC_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret or "your_sandbox" in client_id:
        print("[HMRC Sandbox API] Skipped (no credentials in .env).")
        return False

    token_url = "https://test-api.service.hmrc.gov.uk/oauth/token"
    try:
        resp = httpx.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "read:vat",
            },
            timeout=10.0,
        )
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            vat_resp = httpx.get(
                "https://test-api.service.hmrc.gov.uk/organisations/vat/check-vat-number/lookup/553557881",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.hmrc.2.0+json",
                },
                timeout=10.0,
            )
            print(f"[HMRC Sandbox API] OAuth2 Connected! Test lookup HTTP {vat_resp.status_code}: {vat_resp.text[:140]}...")
            return True
        else:
            print(f"[HMRC Sandbox API] Token request returned HTTP {resp.status_code}: {resp.text[:120]}")
            return False
    except Exception as e:
        print(f"[HMRC Sandbox API] Connection error: {e}")
        return False


def check_live_uk_vat(client: httpx.Client, vat_number: str) -> dict:
    """
    Queries HMRC's official public GOV.UK VAT checker (which hits the live HMRC registry)
    and extracts the registered business name and address.
    """
    entry_url = "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details"
    try:
        get_resp = client.get(entry_url, follow_redirects=True, timeout=12.0)
        soup = BeautifulSoup(get_resp.text, "lxml")
        csrf_input = soup.find("input", {"name": "csrfToken"})
        csrf_token = csrf_input["value"] if csrf_input else ""

        # GOV.UK's form field for the VAT number being checked is named 'target'
        post_resp = client.post(
            entry_url,
            data={"csrfToken": csrf_token, "target": vat_number},
            follow_redirects=True,
            timeout=12.0,
        )
        res_soup = BeautifulSoup(post_resp.text, "lxml")
        page_text = res_soup.get_text("\n", strip=True)

        if "Invalid UK VAT number" in page_text or "does not match" in page_text.lower():
            return {
                "is_valid": 0,
                "hmrc_name": None,
                "hmrc_address": None,
                "raw_response": "INVALID_OR_DEREGISTERED",
            }

        hmrc_name = None
        hmrc_address = None
        for dt in res_soup.find_all(["dt", "th", "h2", "h3"]):
            label = dt.get_text(" ", strip=True).lower()
            dd = dt.find_next_sibling(["dd", "td", "p", "div"])
            if not dd:
                continue
            val = ", ".join(line.strip() for line in dd.get_text("\n", strip=True).splitlines() if line.strip())
            if "registered business name" in label or "business name" in label:
                hmrc_name = val
            elif "registered business address" in label or "business address" in label:
                hmrc_address = val

        if not hmrc_name:
            m_name = re.search(r"Registered business name\s*\n+([^\n]+)", page_text, re.IGNORECASE)
            if m_name:
                hmrc_name = m_name.group(1).strip()
        if not hmrc_address:
            m_addr = re.search(
                r"Registered business address\s*\n+(.+?)(?:\n(?:Date|Time|Check another|Print))",
                page_text,
                re.IGNORECASE | re.DOTALL,
            )
            if m_addr:
                hmrc_address = ", ".join(line.strip() for line in m_addr.group(1).splitlines() if line.strip())

        is_valid = 1 if hmrc_name else 0
        return {
            "is_valid": is_valid,
            "hmrc_name": hmrc_name,
            "hmrc_address": hmrc_address,
            "raw_response": page_text[:600],
        }
    except Exception as e:
        return {
            "is_valid": -1,
            "hmrc_name": None,
            "hmrc_address": None,
            "raw_response": f"ERROR: {e}",
        }


def normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.upper().replace("&", " AND ")
    s = re.sub(r"\b(LIMITED|LTD|LLP|PLC|UK|THE)\b", " ", s)
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    return " ".join(s.split())


def extract_postcode(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", text.upper())
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def crn_in_snippet(crn: str, snippet: str) -> bool:
    if not snippet:
        return False
    stripped = crn.lstrip("0")
    return bool(re.search(rf"\b0*{re.escape(stripped)}\b", snippet, re.IGNORECASE))


def score_entity_match(
    ch_crn: str,
    ch_name: str,
    ch_postcode: str,
    hmrc_name: str,
    hmrc_address: str,
    snippet: str,
) -> tuple[str, float, int, str]:
    norm_ch = normalize_name(ch_name)
    norm_hmrc = normalize_name(hmrc_name)
    sim = round(fuzz.token_sort_ratio(norm_ch, norm_hmrc), 1)

    pc_ch = extract_postcode(ch_postcode or "")
    pc_hmrc = extract_postcode(hmrc_address or "")
    pc_match = 1 if (pc_ch and pc_hmrc and pc_ch == pc_hmrc) else 0
    has_crn_proof = crn_in_snippet(ch_crn, snippet or "")

    if sim >= 85.0 and pc_match == 1:
        return "HIGH_CONFIDENCE", sim, pc_match, "Exact/near-exact name AND registered postcode match"
    if sim >= 85.0 and has_crn_proof:
        return "HIGH_CONFIDENCE", sim, pc_match, "Exact/near-exact name AND CRN co-occurred on source page"
    if sim >= 90.0:
        return "HIGH_CONFIDENCE", sim, pc_match, "Strong normalized legal name match"
    if pc_match == 1 and sim >= 35.0:
        return "MEDIUM_VAT_GROUP", sim, pc_match, "Same registered postcode + shared group name (likely corporate VAT group)"
    if has_crn_proof and pc_match == 1:
        return "MEDIUM_VAT_GROUP", sim, pc_match, "Same postcode + CRN verified on page (trading name or VAT group)"
    return "REJECTED_MISMATCH", sim, pc_match, f"Name/postcode mismatch (CH='{ch_name}' vs HMRC='{hmrc_name}')"


def run_verification_and_matching() -> None:
    print("=" * 65)
    test_hmrc_sandbox_oauth()
    print("=" * 65 + "\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Clear any prior failed lookups so they get checked properly
    cur.execute("DELETE FROM hmrc_checks WHERE is_valid <= 0")
    conn.commit()

    cand_rows = cur.execute(
        """
        SELECT
            c.company_number,
            co.company_name,
            co.account_category,
            co.postcode,
            c.vat_number,
            c.prefix,
            GROUP_CONCAT(DISTINCT c.source_method) AS methods,
            MAX(c.source_detail) AS sample_url,
            MAX(c.raw_snippet) AS sample_snippet
        FROM candidates c
        JOIN companies co ON c.company_number = co.company_number
        GROUP BY c.company_number, c.vat_number
        """
    ).fetchall()

    if not cand_rows:
        print("No candidate VAT numbers found in data/pipeline.db yet.")
        conn.close()
        return

    print(f"Verifying {len(cand_rows)} candidate VAT record(s) against live HMRC registry...\n")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    with httpx.Client(headers=headers) as client:
        for crn, ch_name, acc_cat, ch_pc, vat_num, prefix, methods, url, snippet in cand_rows:
            cached = cur.execute(
                "SELECT is_valid, hmrc_name, hmrc_address FROM hmrc_checks WHERE vat_number = ?",
                (vat_num,),
            ).fetchone()

            if cached:
                is_valid, hmrc_name, hmrc_address = cached
            else:
                res = check_live_uk_vat(client, vat_num)
                is_valid = res["is_valid"]
                hmrc_name = res["hmrc_name"]
                hmrc_address = res["hmrc_address"]
                cur.execute(
                    """
                    INSERT OR REPLACE INTO hmrc_checks
                    (vat_number, is_valid, hmrc_name, hmrc_address, raw_response)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (vat_num, is_valid, hmrc_name, hmrc_address, res["raw_response"]),
                )
                conn.commit()
                time.sleep(1.0)

            print(f"Company:   {crn} | {ch_name} ({acc_cat}) [CH Postcode: {ch_pc}]")
            print(f"Candidate: {prefix}{vat_num} (via {methods})")

            if is_valid == 1 and hmrc_name:
                tier, sim, pc_match, reason = score_entity_match(
                    crn, ch_name, ch_pc or "", hmrc_name, hmrc_address or "", snippet or ""
                )
                cur.execute(
                    """
                    INSERT OR REPLACE INTO verified_matches
                    (company_number, vat_number, confidence_tier, name_similarity, postcode_match, match_reason, manual_audit_label)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (crn, vat_num, tier, sim, pc_match, reason, "PENDING_AUDIT"),
                )
                conn.commit()
                print(f"HMRC says: {hmrc_name} | {hmrc_address}")
                print(f"Decision:  [{tier}] (Name Sim: {sim}%, Postcode Match: {bool(pc_match)}) -> {reason}\n")
            else:
                print(f"HMRC says: NOT ACTIVE / INVALID (status={is_valid})\n")

    export_df = pd.read_sql_query(
        """
        SELECT
            co.company_number,
            co.company_name,
            co.account_category,
            co.postcode AS ch_postcode,
            co.sic_1,
            w.discovered_domain,
            w.discovery_status,
            vm.vat_number,
            vm.confidence_tier,
            vm.name_similarity,
            vm.postcode_match,
            vm.match_reason,
            hc.hmrc_name,
            hc.hmrc_address
        FROM web_scans w
        JOIN companies co ON w.company_number = co.company_number
        LEFT JOIN verified_matches vm ON w.company_number = vm.company_number
        LEFT JOIN hmrc_checks hc ON vm.vat_number = hc.vat_number
        """,
        conn,
    )
    export_df.to_csv(RESULTS_CSV_PATH, index=False)
    conn.close()

    print("-" * 65)
    print(f"Saved full verified dataset to {RESULTS_CSV_PATH} ({len(export_df)} scanned companies).")


if __name__ == "__main__":
    run_verification_and_matching()