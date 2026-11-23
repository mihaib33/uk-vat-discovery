import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class VatCandidate:
    raw_match: str
    vat_number: str          # Normalized 9-digit string, e.g. "232457280"
    prefix: str              # "GB" or "XI"
    source_type: str         # "DIRECT_VAT" or "EORI_DERIVED"
    checksum_algorithm: str  # "MOD97" or "MOD9755"


def validate_uk_vat_checksum(nine_digits: str) -> Optional[str]:
    """
    Validates a 9-digit UK VAT number against HMRC Modulus-97 and Modulus-9755 rules.
    Returns 'MOD97', 'MOD9755', or None if invalid.
    """
    if not re.fullmatch(r"\d{9}", nine_digits):
        return None

    # Reject dummy/impossible blocks (000 prefix or non-standard special blocks)
    if nine_digits.startswith("000"):
        return None

    weights = [8, 7, 6, 5, 4, 3, 2]
    weighted_sum = sum(int(nine_digits[i]) * weights[i] for i in range(7))
    check_digits = int(nine_digits[7:9])

    # Standard Modulus 97 (issued prior to ~2010)
    rem_97 = (weighted_sum + check_digits) % 97
    if rem_97 == 0:
        return "MOD97"

    # Modulus 9755 (introduced when 9-digit Mod97 space ran low)
    rem_9755 = (weighted_sum + check_digits + 55) % 97
    if rem_9755 == 0:
        return "MOD9755"

    return None


def extract_vat_candidates(text: str) -> list[VatCandidate]:
    """
    Extracts and validates UK VAT numbers (9-digit) and GB EORI numbers (12-digit)
    from raw text, discarding ~98% of random digit noise via checksum validation.
    """
    candidates: list[VatCandidate] = []
    seen: set[str] = set()

    # 1. Check 12-digit GB/XI EORI numbers first: GB + 9 digits + 000
    eori_pattern = re.compile(r"\b(GB|XI)[\s\-]?(\d{3}[\s\-]?\d{3}[\s\-]?\d{3})[\s\-]?000\b", re.IGNORECASE)
    for match in eori_pattern.finditer(text):
        prefix = match.group(1).upper()
        digits = re.sub(r"[\s\-]", "", match.group(2))
        algo = validate_uk_vat_checksum(digits)
        if algo and digits not in seen:
            seen.add(digits)
            candidates.append(
                VatCandidate(
                    raw_match=match.group(0).strip(),
                    vat_number=digits,
                    prefix=prefix,
                    source_type="EORI_DERIVED",
                    checksum_algorithm=algo,
                )
            )

    # 2. Check standard 9-digit VAT numbers (with or without GB/XI prefix when near VAT context)
    vat_pattern = re.compile(
        r"(?:(?:GB|XI)[\s\-]?|(?:\bVAT\b|\bV\.A\.T\.?\b)[^\d]{0,25})(\d{3}[\s\-]?\d{4}[\s\-]?\d{2}|\d{3}[\s\-]?\d{3}[\s\-]?\d{3}|\d{9})\b",
        re.IGNORECASE,
    )
    for match in vat_pattern.finditer(text):
        raw = match.group(0).strip()
        prefix_match = re.search(r"\b(GB|XI)\b", raw, re.IGNORECASE)
        prefix = prefix_match.group(1).upper() if prefix_match else "GB"
        digits = re.sub(r"[\s\-]", "", match.group(1))
        algo = validate_uk_vat_checksum(digits)
        if algo and digits not in seen:
            seen.add(digits)
            candidates.append(
                VatCandidate(
                    raw_match=raw,
                    vat_number=digits,
                    prefix=prefix,
                    source_type="DIRECT_VAT",
                    checksum_algorithm=algo,
                )
            )

    return candidates


if __name__ == "__main__":
    sample_text = """
    Footer: Registered in England No. 01234567. VAT Reg No: GB 232 4572 80.
    Customs declarations use EORI GB232457280000.
    Fake noise number: GB 123 4567 89.
    """
    results = extract_vat_candidates(sample_text)
    for r in results:
        print(r)