# UK Corporate VAT Number Discovery & Matching Pipeline

A practical investigation and reproducible Python pipeline for finding, verifying, and matching **UK Value Added Tax (`GB` VAT) numbers** to **Companies House** records - built for the Veridion Data Engineering & Research Challenge.

---

## Executive Summary

| Experiment / Pipeline Step | Sample Size | What I Found | What It Means for Production |
| :--- | :---: | :--- | :--- |
| **1. Companies House Bulk CSV** (`BasicCompanyData`) | 4,812,641 active firms | **0%** VAT fields; **0%** website URLs | Good master list of UK companies (`CRN`, legal name, SIC code, postcode, company size), but it cannot provide VAT numbers or websites directly. |
| **2. Companies House iXBRL Accounts** (`Accounts_Bulk_Data`) | **5,000** statutory filings | **35.7%** (1,784) mention `"VAT"` in accounting notes; **0.00%** (0/5,000) include a VAT number | UK annual accounts are a dead end for VAT discovery - accounting software leaves the optional iXBRL VAT tag blank. |
| **3. Stratified Web Discovery** (Search + Crawl + Common Crawl) | **160** active companies (`100` Large/Medium, `25` Small, `20` Micro, `15` Dormant) | **5.0%** (5/100) found on `FULL`/`MEDIUM` companies; **0.0%** (0/60) on `SMALL`/`MICRO`/`DORMANT` | Crawling should focus on `FULL` and `MEDIUM` companies first instead of wasting requests on the ~3.2M tiny UK firms below the £90k VAT threshold. |
| **4. Live HMRC Verification & Entity Matching** | **5** candidate VAT IDs | **100.0%** (5/5) valid & active on HMRC; **0.0%** false positives | Local Mod-97/Mod-9755 checksums + directory blocklists + CRN checks filter out bad matches before hitting HMRC. |

---

## Part 1: Data Sources & Dead-End Analysis

### 1.1 Source Comparison

| Source | Cost & Access | Has `GB` VAT? | Has Website? | Takeaway |
| :--- | :--- | :---: | :---: | :--- |
| **Companies House Bulk CSV** (`BasicCompanyDataAsOneFile`) | Free monthly download (~3 GB) | No | No | Essential starting list of 4.81M active UK companies, their size categories, and registered postcodes. |
| **Companies House Bulk Accounts** (`Accounts_Bulk_Data` iXBRL ZIPs) | Free daily ZIPs | **No (0 / 5,000)** | Rare | **Dead End.** Even though UK iXBRL templates have an optional VAT tag, UK accounting rules do not require printing a VAT number on public filings. |
| **Open Web (Search Snippets + Targeted Legal-Page Crawl)** | Low cost (rate-limited search + HTTP) | **Yes (5.0% on Large/Medium)** | **Yes (55.6% overall)** | Best discovery source. VAT numbers usually sit in `/terms-and-conditions`, `/privacy`, delivery/checkout pages, and website footers. |
| **Common Crawl (CDX Index + WARC Range Requests)** | Free (AWS S3 / HTTP byte ranges) | Yes (archive backup) | Yes | Useful zero-cost backup when a live website blocks scrapers (`403` / Cloudflare). |
| **HMRC VAT Registered Companies API v2.0 & GOV.UK Checker** | Free (OAuth2 Sandbox/Prod & GOV.UK form) | Verification only (by VAT ID) | No | **Official HMRC checker.** Returns the registered business name and address for any active 9-digit VAT number, but cannot be searched by company name or CRN. |

### 1.2 Why Finding UK VAT Numbers Is Harder Than in Continental Europe
1. **High £90,000 Registration Threshold:** UK businesses only have to register for VAT once their taxable turnover passes **£90,000**. That keeps roughly 3.2 million small and micro UK businesses out of the VAT system altogether.
2. **No Universal Website *Impressum* Rule:** Unlike Germany or Austria, the UK does not force every company website to display a standard legal block with a tax ID. Under the UK *Electronic Commerce Regulations 2002*, businesses only have to show their VAT number when selling goods or services online - which explains why retail/e-commerce and larger firms (`DialaChemist`, `Ercol`, `Deacons`, `J T Atkinson`) publish theirs while small local firms rarely do.
3. **One-Way Official Lookup:** HMRC only lets you look up a record if you *already* have the 9-digit VAT number (`target`). You cannot search HMRC by company name or Companies House number (`CRN`), so numbers have to be found on the web first and verified afterward.

---

## Part 2: Pipeline Architecture, Checksums & Results

### 2.1 How the Pipeline Works

```text
Companies House Bulk CSV (4.81M Active Companies)
       |
       v  [src/sampler.py] - Stratified random sample across company sizes & iXBRL arms
SQLite Pipeline Database (data/pipeline.db)
       |
       +--> [src/verify_ixbrl_deadend.py] --> Checked 5,000 iXBRL accounts --> 0.00% VAT hits (Dead End)
       |
       v  [src/web_discover.py] - Domain Search + site:domain "VAT" + Legal Subpages + Common Crawl
Local Candidate Filtering [src/checksum.py]
  |- Regex extraction for standard VAT (GB 9-digit / 12-digit branch) & EORI (GB + 9-digit + 000)
  |- Mod-97 & post-2010 Mod-9755 check-digit math (eliminates ~98% of random 9-digit numbers)
  +- False-positive filters: B2B directory blocklist (45 sites) + mismatched CRN check
       |
       v  [src/verify_and_match.py] - Live HMRC Registry Check + Fuzzy Name/Postcode Matching
Verified Output (data/sample_results.csv)
```

### 2.2 Local Checksum Validation (`src/checksum.py`)
A UK VAT number has 9 digits ($d_1 d_2 d_3 d_4 d_5 d_6 d_7 d_8 d_9$). The first 7 digits are the base number, and the last 2 digits ($d_8 d_9$) are a check value calculated with weights $[8, 7, 6, 5, 4, 3, 2]$:

$$S = \sum_{i=1}^{7} w_i d_i + (10 d_8 + d_9)$$

* **Pre-2010 Rule (`MOD97`):** Valid if $S \equiv 0 \pmod{97}$.
* **Post-2010 Rule (`MOD9755`):** Valid if $(S + 55) \equiv 0 \pmod{97}$.
* **EORI Numbers:** UK customs EORI numbers (`GB` + 9-digit VAT + `000`) are stripped down to their 9-digit core and verified with the exact same checksum formulas.

### 2.3 Stratified Results Across 160 Companies

To measure both hit rate and false-positive safety across different company sizes, I tested 160 active companies grouped by their official Companies House `AccountCategory` (including 15 `DORMANT` companies as a negative control):

| Company Account Tier | Companies Scanned | Websites Found | Verified VAT Found | Website Hit Rate | Verified VAT Hit Rate | Precision (HMRC Valid) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Large / Medium** (`FULL`, `MEDIUM`) | 100 | 55 | **5** | 55.0% | **5.0%** | **100.0% (5/5)** |
| **2. Small** (`TOTAL EXEMPTION SMALL`) | 25 | 14 | **0** | 56.0% | **0.0%** | N/A |
| **3. Micro** (`MICRO ENTITY`) | 20 | 10 | **0** | 50.0% | **0.0%** | N/A |
| **4. Dormant** (`DORMANT` Control) | 15 | 10 | **0** | 66.7% | **0.0%** | N/A (0 False Positives) |
| **Total** | **160** | **89** | **5** | **55.6%** | **3.1%** | **100.0% (5/5)** |

### 2.4 Verified Matches & Edge Cases Handled

All 5 candidate numbers found by the v2 pipeline came back active on HMRC's live registry (`data/sample_results.csv`):

| CRN | Companies House Name | Tier | Verified VAT | HMRC Registered Name | Match Result & Edge Case |
| :---: | :--- | :---: | :---: | :--- | :--- |
| `01027936` | J T ATKINSON & SONS LIMITED | `FULL` | `GB330475674` | `J T ATKINSON & SONS LTD` | `HIGH_CONFIDENCE` (100% name + postcode `TS3 8DE` match; found inside `/terms-and-conditions`). |
| `00163292` | ERCOL FURNITURE LIMITED | `FULL` | `GB208013804` | `ERCOL FURNITURE LTD` | `HIGH_CONFIDENCE` (100% name + postcode `HP27 9PX` match; site printed CRN without leading zeros as `163292`). |
| `00131937` | DEACON & SON (SWINDON) LIMITED | `MEDIUM` | `GB194384922` | `DEACON AND SON (SWINDON) LIMITED` | `HIGH_CONFIDENCE` (100% normalized `&` -> `AND` + postcode `SN1 4AN` match). |
| `06384146` | DIALACHEMIST LIMITED | `FULL` | `GB226889855` | `DIALACHEMIST LIMITED` | `HIGH_CONFIDENCE` (100% name match; **Registered vs. Operating Address**: CH lists London solicitor `EC2Y 5EB`, HMRC lists Lancashire warehouse `BB5 5JB`). |
| `04690555` | JAMES CROPPER SPECIALITY PAPERS LIMITED | `FULL` | `GB442770255` | `JAMES CROPPER PUBLIC LTD CO` | `MEDIUM_VAT_GROUP` (**Corporate VAT Group**: Subsidiary uses its parent PLC's group VAT number at the same postcode `LA9 6PZ`). |

### 2.5 Mistakes Caught & Fixed Along the Way
1. **Business Directory Footer Leaks:** In my first test run, searching `"DATAROBOT UK LTD"` matched a profile on `companypulse.co.uk`, and crawling `/contact` pulled CompanyPulse's own VAT (`GB132978001`, `Company No. 05431902`). **Fix:** Blocked 45 B2B directory/social domains and added a check that drops any page listing a `Company No.` that doesn't match our target company.
2. **Missing Leading Zeros in CRNs:** UK websites often drop leading zeros from 8-character registration numbers (writing `163292` instead of `00163292`). The matcher strips leading zeros when checking CRNs in webpage text.
3. **Corporate VAT Groups & Solicitor Addresses:** Requiring *both* an exact name match and an exact postcode match would have thrown away 2 out of 5 valid matches (`Dialachemist` and `James Cropper`). Using two tiers (`HIGH_CONFIDENCE` for >= 90% name similarity; `MEDIUM_VAT_GROUP` for exact postcode match + shared group word) kept both valid matches without letting false positives through.

---

## Part 3: Scaling to 4.8 Million UK Companies

Running live web searches on all 4.81 million UK companies would waste time and resources. Based on the tier results above, a production pipeline should run in **four prioritized stages**:

1. **Stage 1 - Filter & Prioritize (~4.81M -> ~650k Priority Companies):**
   * Skip `DORMANT` (~700k+) and brand-new companies with no filed accounts.
   * Run `FULL`, `MEDIUM`, and `GROUP` companies (plus retail/e-commerce/manufacturing SIC codes) first, since they are almost all VAT-registered and far more likely to publish their VAT number, before moving on to `SMALL` firms.
2. **Stage 2 - Free Common Crawl Pass First (Monthly):**
   * For companies where Veridion already knows the website domain, scan Common Crawl's monthly index first (`/terms*`, `/privacy*`, `/contact*`, and `/`) using S3 byte-range requests before running live web searches.
3. **Stage 3 - Combined Search Queries & Parallel Crawling:**
   * For remaining priority companies without a known website, run a single combined search query (`"Company Name" (Postcode OR VAT)`) instead of multiple separate searches per company. Grouping the query cuts total search requests in half, making discovery faster and cheaper at scale.
   * Instead of downloading pages one by one as in this prototype, run an asynchronous crawler (`asyncio` + `httpx`) to fetch dozens of domains in parallel while keeping polite per-domain rate limits and short timeouts so slow websites do not stall the batch.
4. **Stage 4 - Local Checksum Filter & HMRC Verification Queue:**
   * Local Mod-97/Mod-9755 validation throws out ~98% of random number matches in microseconds for free.
   * Only unique, checksum-valid numbers go into a rate-limited queue hitting the official **HMRC VAT Registered Companies API v2.0**, with results cached in the database.
   * **Refresh Schedule:** Re-check active VAT numbers every quarter to catch deregistrations, and crawl newly filed `FULL`/`MEDIUM` companies monthly when Companies House updates its snapshot.

---

## Part 4: Design Trade-Offs

* **Why Not Just Generate All Valid VAT Numbers and Check Them on HMRC?** Since 2 of the 9 digits are check digits, there are only $10^7$ (10 million) possible base numbers (~20M total across both checksum rules). In practice, though, brute-forcing HMRC does not work: (1) HMRC enforces strict rate limits and anti-bot blocks, so sending millions of sequential lookups would take months and quickly result in IP blocks or API bans, (2) roughly 40-50% of UK VAT numbers belong to sole traders and partnerships that are not in Companies House anyway, and (3) matching millions of raw HMRC names back to 4.81M companies without a website or CRN creates too many false matches.
* **Live Search vs. Common Crawl Only:** Common Crawl is free, but it lags behind live sites by weeks and often misses deep `/terms-and-conditions` pages on mid-sized UK sites. Using Common Crawl first for known domains and live search (`site:domain "VAT"`) second gives the best balance of cost and coverage.
* **Handling Corporate VAT Groups:** Because UK parent and subsidiary companies can share one group VAT number, a production database should store VAT links as **many-to-one** (`many CRNs -> one VAT number`) with a `VAT_GROUP` flag instead of assuming every company has its own unique VAT ID.

---

## Repository Structure & Quickstart

```text
uk-vat-discovery/
├── src/
│   ├── checksum.py              # UK Mod-97, post-2010 Mod-9755 & EORI (GB...000) validators
│   ├── sampler.py               # Stratified 400-company sampler across company sizes & iXBRL arms
│   ├── ixbrl_parser.py          # Companies House bulk iXBRL parser
│   ├── verify_ixbrl_deadend.py  # 5,000-filing audit proving iXBRL filings lack VAT numbers
│   ├── web_discover.py          # Search + legal-page crawler + Common Crawl backup + CRN guardrails
│   ├── verify_and_match.py      # HMRC Sandbox OAuth2 check + live GOV.UK checker + fuzzy matcher
│   └── summary_stats.py         # Prints stratified benchmark tables
├── data/
│   └── sample_results.csv       # Full 160-company dataset & HMRC verification outcomes
├── .env.example                 # Template for HMRC Sandbox OAuth2 credentials
├── .gitignore                   # Excludes raw 3GB+ Companies House files, SQLite DB, and .env
├── requirements.txt             # Python dependencies
└── README.md                    # Full report & scaling architecture
```

### How to Run

```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Test Mod-97 / Mod-9755 / EORI checksums
python src/checksum.py

# 4. Run HMRC verification & entity matching on discovered candidates
python src/verify_and_match.py

# 5. Print summary statistics
python src/summary_stats.py
```