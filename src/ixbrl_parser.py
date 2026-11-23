import re
import sqlite3
import zipfile
from pathlib import Path
from bs4 import BeautifulSoup

try:
    from checksum import extract_vat_candidates
except ImportError:
    from src.checksum import extract_vat_candidates

DB_PATH = Path("data/pipeline.db")


def read_filing_text(zip_path: str, inner_file: str) -> str:
    """Reads a single company's financial report directly from the outer ZIP file."""
    with zipfile.ZipFile(zip_path, "r") as outer_zf:
        raw_bytes = outer_zf.read(inner_file)

        # Handle rare cases where the inner file is itself a nested ZIP
        if inner_file.lower().endswith(".zip"):
            import io
            with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as inner_zf:
                html_files = [
                    f for f in inner_zf.namelist() if f.lower().endswith((".html", ".xml"))
                ]
                if not html_files:
                    return ""
                raw_bytes = inner_zf.read(html_files[0])

    html_text = raw_bytes.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html_text, "lxml")

    # 1. Check if any hidden XBRL tags explicitly hold a VAT or EORI attribute/value
    tagged_snippets = []
    for tag in soup.find_all(attrs={"name": re.compile(r"vat|eori|taxreg", re.I)}):
        val = tag.get_text(" ", strip=True)
        if val:
            tagged_snippets.append(f"VAT {val}")

    # 2. Extract all visible text from the financial report
    visible_text = soup.get_text(" ", strip=True)
    return "\n".join(tagged_snippets) + "\n" + visible_text


def get_context_snippet(text: str, needle: str, window: int = 55) -> str:
    """Grabs a short readable sentence around the matched VAT number."""
    idx = text.lower().find(needle.lower())
    if idx == -1:
        return needle
    start = max(0, idx - window)
    end = min(len(text), idx + len(needle) + window)
    return " ".join(text[start:end].split())


def scan_ixbrl_sample() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT company_number, company_name, account_category, ixbrl_zip_file, ixbrl_inner_file
        FROM companies
        WHERE ixbrl_zip_file IS NOT NULL AND ixbrl_inner_file IS NOT NULL
        """
    ).fetchall()

    print(f"Scanning {len(rows)} company financial reports inside your ZIP archives...\n")

    scanned = 0
    companies_with_hits = 0
    total_candidates = 0

    for crn, name, acc_cat, zip_path, inner_file in rows:
        text = read_filing_text(zip_path, inner_file)
        scanned += 1
        candidates = extract_vat_candidates(text)

        if candidates:
            companies_with_hits += 1
            for cand in candidates:
                total_candidates += 1
                snippet = get_context_snippet(text, cand.raw_match)
                cur.execute(
                    """
                    INSERT OR IGNORE INTO candidates
                    (company_number, vat_number, prefix, source_method, source_detail, checksum_algo, raw_snippet)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        crn,
                        cand.vat_number,
                        cand.prefix,
                        "COMPANIES_HOUSE_IXBRL",
                        inner_file,
                        cand.checksum_algorithm,
                        snippet,
                    ),
                )
                print(
                    f"[HIT] {crn} | {name} ({acc_cat})\n"
                    f"      -> VAT: {cand.prefix}{cand.vat_number} ({cand.source_type}, {cand.checksum_algorithm})\n"
                    f"      -> Context: \"...{snippet}...\"\n"
                )

    conn.commit()
    conn.close()

    pct = (companies_with_hits / scanned * 100) if scanned else 0
    print("-" * 60)
    print(f"Finished scanning {scanned} financial reports.")
    print(f"Companies with a valid VAT/EORI candidate: {companies_with_hits} / {scanned} ({pct:.1f}%)")
    print(f"Total candidate numbers saved to data/pipeline.db: {total_candidates}")


if __name__ == "__main__":
    scan_ixbrl_sample()