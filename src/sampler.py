import os
import re
import sqlite3
import zipfile
from pathlib import Path
import duckdb
import pandas as pd

DB_PATH = Path("data/pipeline.db")
SAMPLE_CSV_PATH = Path("data/sample_companies.csv")
RANDOM_SEED = 42


def init_database(db_path: Path) -> sqlite3.Connection:
    """Creates the SQLite database tables to track every stage of the pipeline."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            company_number TEXT PRIMARY KEY,
            company_name TEXT NOT NULL,
            company_status TEXT,
            company_category TEXT,
            account_category TEXT,
            incorporation_date TEXT,
            address_line1 TEXT,
            post_town TEXT,
            postcode TEXT,
            sic_1 TEXT,
            sample_arm TEXT,
            ixbrl_zip_file TEXT,
            ixbrl_inner_file TEXT
        );

        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_number TEXT NOT NULL,
            vat_number TEXT NOT NULL,
            prefix TEXT,
            source_method TEXT NOT NULL,
            source_detail TEXT,
            checksum_algo TEXT,
            raw_snippet TEXT,
            UNIQUE(company_number, vat_number, source_method)
        );

        CREATE TABLE IF NOT EXISTS hmrc_checks (
            vat_number TEXT PRIMARY KEY,
            is_valid INTEGER NOT NULL,
            hmrc_name TEXT,
            hmrc_address TEXT,
            checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            raw_response TEXT
        );

        CREATE TABLE IF NOT EXISTS verified_matches (
            company_number TEXT PRIMARY KEY,
            vat_number TEXT NOT NULL,
            confidence_tier TEXT NOT NULL,
            name_similarity REAL,
            postcode_match INTEGER,
            match_reason TEXT,
            manual_audit_label TEXT
        );
        """
    )
    conn.commit()
    return conn


def find_or_extract_companies_csv() -> str:
    """Finds BasicCompanyDataAsOneFile-*.csv (even in subfolders) or extracts it from the ZIP."""
    data_dir = Path("data")
    csv_matches = sorted(data_dir.rglob("BasicCompanyDataAsOneFile-*.csv"))
    if csv_matches:
        return csv_matches[0].as_posix()

    zip_matches = sorted(data_dir.rglob("BasicCompanyDataAsOneFile-*.zip"))
    if zip_matches:
        zip_path = zip_matches[0]
        print(f"Found {zip_path.name} — extracting CSV to data/ (takes ~15 seconds)...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            csv_members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
            if not csv_members:
                raise FileNotFoundError(f"No CSV found inside {zip_path}")
            extracted_path = zf.extract(csv_members[0], path=data_dir)
            return Path(extracted_path).as_posix()

    found_files = [p.name for p in data_dir.iterdir()] if data_dir.exists() else []
    raise FileNotFoundError(
        f"Could not find BasicCompanyDataAsOneFile-*.csv or .zip inside data/.\n"
        f"Files currently inside data/: {found_files}\n"
        f"Move BasicCompanyDataAsOneFile-2026-11-01.zip from Downloads into data/ and re-run."
    )


def index_ixbrl_zips() -> dict[str, tuple[str, str]]:
    """
    Reads the filenames inside the downloaded Accounts_Bulk_Data ZIPs without extracting them.
    Returns a dict mapping 8-char CompanyNumber -> (zip_path, inner_filename).
    """
    crn_map: dict[str, tuple[str, str]] = {}
    zip_files = sorted(Path("data").rglob("Accounts_Bulk_Data-*.zip"))

    if not zip_files:
        print("Warning: No Accounts_Bulk_Data-*.zip files found in data/.")
        return crn_map

    crn_regex = re.compile(r"_([A-Z0-9]{8})_\d{8}", re.IGNORECASE)

    for zp in zip_files:
        with zipfile.ZipFile(zp, "r") as zf:
            for member in zf.namelist():
                if not member.lower().endswith((".html", ".xml", ".zip")):
                    continue
                match = crn_regex.search(os.path.basename(member))
                if match:
                    crn = match.group(1).upper()
                    if crn not in crn_map or member.lower().endswith(".html"):
                        crn_map[crn] = (zp.as_posix(), member)

    print(f"Indexed {len(crn_map):,} unique companies across {len(zip_files)} iXBRL ZIP archive(s).")
    return crn_map


def build_stratified_sample() -> pd.DataFrame:
    csv_path = find_or_extract_companies_csv()
    print(f"Querying {csv_path} with DuckDB...")

    ixbrl_index = index_ixbrl_zips()

    con = duckdb.connect()
    raw_cols = [
        row[0]
        for row in con.execute(
            f"DESCRIBE SELECT * FROM read_csv_auto('{csv_path}', all_varchar=true)"
        ).fetchall()
    ]
    col_lookup = {c.strip(): c for c in raw_cols}

    def qcol(clean_name: str) -> str:
        return f'TRIM("{col_lookup[clean_name]}")'

    query = f"""
        SELECT
            {qcol('CompanyNumber')} AS company_number,
            {qcol('CompanyName')} AS company_name,
            {qcol('CompanyStatus')} AS company_status,
            {qcol('CompanyCategory')} AS company_category,
            COALESCE(NULLIF({qcol('Accounts.AccountCategory')}, ''), 'NONE') AS account_category,
            {qcol('IncorporationDate')} AS incorporation_date,
            {qcol('RegAddress.AddressLine1')} AS address_line1,
            {qcol('RegAddress.PostTown')} AS post_town,
            {qcol('RegAddress.PostCode')} AS postcode,
            {qcol('SICCode.SicText_1')} AS sic_1
        FROM read_csv_auto('{csv_path}', all_varchar=true)
        WHERE {qcol('CompanyStatus')} = 'Active'
          AND {qcol('CompanyCategory')} IN (
              'Private Limited Company',
              'Public Limited Company',
              'Limited Liability Partnership'
          )
    """
    df = con.execute(query).df()
    print(f"Total active corporate entities in UK register: {len(df):,}")

    def bucket_tier(cat: str) -> str:
        c = cat.upper()
        if c in {"FULL", "MEDIUM", "GROUP"}:
            return "LARGE_OR_MEDIUM"
        if "SMALL" in c:
            return "SMALL"
        if "MICRO" in c:
            return "MICRO"
        if "DORMANT" in c:
            return "DORMANT"
        return "OTHER_NEW"

    df["tier"] = df["account_category"].apply(bucket_tier)
    df["has_ixbrl"] = df["company_number"].isin(ixbrl_index)

    print("\n--- UK Active Register Breakdown by Account Tier ---")
    print(df["tier"].value_counts().to_string())

    quotas = {
        "LARGE_OR_MEDIUM": 50,
        "SMALL": 75,
        "MICRO": 50,
        "DORMANT": 25,
    }

    samples = []
    for arm_name, subset in [
        ("IXBRL_ARM", df[df["has_ixbrl"]]),
        ("GENERAL_ARM", df[~df["has_ixbrl"]]),
    ]:
        for tier, n_target in quotas.items():
            pool = subset[subset["tier"] == tier]
            take = min(n_target, len(pool))
            drawn = pool.sample(n=take, random_state=RANDOM_SEED).copy()
            drawn["sample_arm"] = arm_name
            samples.append(drawn)

    sample_df = pd.concat(samples, ignore_index=True)

    sample_df["ixbrl_zip_file"] = sample_df["company_number"].map(
        lambda crn: ixbrl_index[crn][0] if crn in ixbrl_index else None
    )
    sample_df["ixbrl_inner_file"] = sample_df["company_number"].map(
        lambda crn: ixbrl_index[crn][1] if crn in ixbrl_index else None
    )

    cols_to_save = [
        "company_number",
        "company_name",
        "company_status",
        "company_category",
        "account_category",
        "incorporation_date",
        "address_line1",
        "post_town",
        "postcode",
        "sic_1",
        "sample_arm",
        "ixbrl_zip_file",
        "ixbrl_inner_file",
    ]
    sample_df = sample_df[cols_to_save]

    conn = init_database(DB_PATH)
    sample_df.to_sql("companies", conn, if_exists="replace", index=False)
    conn.close()

    sample_df.to_csv(SAMPLE_CSV_PATH, index=False)
    print(f"\nSaved {len(sample_df)} stratified sample companies to {DB_PATH} and {SAMPLE_CSV_PATH}.")
    return sample_df


if __name__ == "__main__":
    build_stratified_sample()