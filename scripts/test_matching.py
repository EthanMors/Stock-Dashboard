import sys
sys.path.insert(0, "stock-dashboard")

from data.cache import get_all_hedge_fund_filings
from data.thirteend_fetcher import get_sc13d_rationales, match_rationales_to_holdings

# Find Exor in the DB
funds = get_all_hedge_fund_filings()
exor = next((f for f in funds if "exor" in f["name"].lower()), None)
if not exor:
    print("Exor not found in DB")
    sys.exit(1)

print(f"Fund: {exor['name']}  CIK: {exor['cik']}  positions: {exor['total_holdings']}")
print("Holdings:")
for h in exor["holdings"]:
    print(f"  issuer={h['issuer']!r}  ticker={h['ticker']!r}")

print()
rationales = get_sc13d_rationales(exor["cik"])
print(f"SC 13D rationales ({len(rationales)}):")
for r in rationales:
    print(f"  subject={r['subject_company']!r}  has_text={bool(r['item4_text'])}")

print()
matched = match_rationales_to_holdings(rationales, exor["holdings"])
print(f"Matched ({len(matched)} holdings with rationale):")
for issuer, rat_list in matched.items():
    print(f"  '{issuer}' → {[r['subject_company'] for r in rat_list]}")
