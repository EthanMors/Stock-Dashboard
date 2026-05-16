import sys
sys.path.insert(0, "stock-dashboard")

from data.thirteend_fetcher import get_sc13d_rationales

# Exor N.V. — known to have SC 13D filings
cik = "1589122"
print(f"Fetching SC 13D rationales for CIK {cik} (Exor N.V.)...")
rationales = get_sc13d_rationales(cik)
print(f"Found {len(rationales)} rationale(s)")
for r in rationales[:5]:
    print(f"  subject_company={r['subject_company']!r}  filing_date={r['filing_date']}  has_text={bool(r['item4_text'])}")
