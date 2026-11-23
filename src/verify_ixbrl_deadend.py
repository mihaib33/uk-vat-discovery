import re
import zipfile
from pathlib import Path

try:
    from checksum import extract_vat_candidates
except ImportError:
    from src.checksum import extract_vat_candidates

MAX_FILES_TO_CHECK = 5000


def audit_bulk_ixbrl() -> None:
    zip_files = sorted(Path("data").rglob("Accounts_Bulk_Data-*.zip"))
    checked = 0
    mentions_word_vat = 0
    valid_vat_numbers = 0

    for zp in zip_files:
        with zipfile.ZipFile(zp, "r") as zf:
            for name in zf.namelist():
                if not name.lower().endswith((".html", ".xml")):
                    continue
                raw_text = zf.read(name).decode("utf-8", errors="ignore")
                # Strip HTML tags quickly with regex so we scan 5,000 files in ~3 seconds
                clean_text = re.sub(r"<[^>]+>", " ", raw_text)
                checked += 1

                if re.search(r"\bVAT\b|\bValue Added Tax\b", clean_text, re.IGNORECASE):
                    mentions_word_vat += 1

                cands = extract_vat_candidates(clean_text)
                if cands:
                    valid_vat_numbers += 1
                    print(f"[FOUND] {name} -> {cands[0].prefix}{cands[0].vat_number}")

                if checked >= MAX_FILES_TO_CHECK:
                    break
        if checked >= MAX_FILES_TO_CHECK:
            break

    print("-" * 60)
    print(f"Total financial reports scanned: {checked:,}")
    print(f"Reports mentioning 'VAT' in accounting text: {mentions_word_vat:,} ({mentions_word_vat/checked*100:.1f}%)")
    print(f"Reports containing an actual VAT number:     {valid_vat_numbers:,} ({valid_vat_numbers/checked*100:.2f}%)")


if __name__ == "__main__":
    audit_bulk_ixbrl()