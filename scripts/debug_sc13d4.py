import sys
sys.path.insert(0, "stock-dashboard")

from edgar import get_entity, set_identity

set_identity("ethanjosemorris@gmail.com")

entity = get_entity("1589122")
filings = entity.get_filings(form="SC 13D")

f = filings[0]
ih = f.index_headers
sc = ih.subject_company
print("subject_company type:", type(sc))
print("subject_company attrs:", [a for a in dir(sc) if not a.startswith("_")])
print("str(subject_company):", str(sc)[:300])

# Try common name attrs
for attr in ["name", "company", "company_name", "cik", "entity_name"]:
    try:
        val = getattr(sc, attr)
        print(f"  sc.{attr} = {val!r}")
    except AttributeError:
        pass

# Check a few more filings
print("\n--- Checking first 5 filings ---")
for i, filing in enumerate(filings[:5]):
    try:
        sc2 = filing.index_headers.subject_company
        name = getattr(sc2, "name", None) or getattr(sc2, "company", None) or getattr(sc2, "company_name", None)
        print(f"Filing {i}: date={filing.filing_date}, subject={name!r}")
    except Exception as e:
        print(f"Filing {i}: error - {e}")
