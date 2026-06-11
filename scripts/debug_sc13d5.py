import sys
sys.path.insert(0, "stock-dashboard")

from edgar import get_entity, set_identity

set_identity("ethanjosemorris@gmail.com")

entity = get_entity("1589122")
filings = entity.get_filings(form="SC 13D")

print("First 8 filings subject companies:")
for i, f in enumerate(filings[:8]):
    try:
        sc = f.index_headers.subject_company
        name = sc.company_data.conformed_name
        print(f"  {i}: {f.filing_date}  subject={name!r}")
    except Exception as e:
        print(f"  {i}: {f.filing_date}  ERROR: {e}")
