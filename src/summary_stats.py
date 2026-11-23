import sqlite3
import pandas as pd

conn = sqlite3.connect("data/pipeline.db")

df = pd.read_sql_query(
    """
    SELECT
        co.company_number,
        co.company_name,
        co.account_category,
        co.sample_arm,
        w.discovered_domain,
        w.discovery_status,
        vm.vat_number,
        vm.confidence_tier,
        vm.name_similarity,
        vm.postcode_match,
        hc.hmrc_name
    FROM web_scans w
    JOIN companies co ON w.company_number = co.company_number
    LEFT JOIN verified_matches vm ON w.company_number = vm.company_number
    LEFT JOIN hmrc_checks hc ON vm.vat_number = hc.vat_number
    """,
    conn,
)
conn.close()


def bucket(cat: str) -> str:
    c = (cat or "").upper()
    if c in {"FULL", "MEDIUM", "GROUP"}:
        return "1. LARGE_OR_MEDIUM"
    if "SMALL" in c:
        return "2. SMALL"
    if "MICRO" in c:
        return "3. MICRO"
    if "DORMANT" in c:
        return "4. DORMANT (Control)"
    return "5. OTHER"


df["tier"] = df["account_category"].apply(bucket)
df["has_site"] = df["discovered_domain"].notna()
df["has_verified_vat"] = df["vat_number"].notna()

summary = (
    df.groupby("tier")
    .agg(
        companies_scanned=("company_number", "count"),
        websites_found=("has_site", "sum"),
        verified_vat_found=("has_verified_vat", "sum"),
    )
    .reset_index()
)
summary["site_rate_%"] = (summary["websites_found"] / summary["companies_scanned"] * 100).round(1)
summary["vat_discovery_%"] = (summary["verified_vat_found"] / summary["companies_scanned"] * 100).round(1)

print("\n=== FINAL 160-COMPANY STRATIFIED RESULTS ===")
print(summary.to_string(index=False))

print("\n=== VERIFIED MATCHES DETAIL ===")
matches = df[df["has_verified_vat"]][
    ["company_number", "company_name", "tier", "vat_number", "confidence_tier", "name_similarity", "postcode_match", "hmrc_name"]
]
print(matches.to_string(index=False))