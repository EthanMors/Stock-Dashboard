import sys
sys.path.insert(0, "stock-dashboard")

from edgar import get_entity, set_identity

set_identity("ethanjosemorris@gmail.com")

entity = get_entity("1589122")
filings = entity.get_filings(form="SC 13D")
print(f"Total SC 13D filings: {len(filings)}")

# Look at the most recent filing
f = filings[0]
print(f"filing.company = {f.company!r}")
print(f"filing.filing_date = {f.filing_date!r}")
print(f"filing.accession_number = {f.accession_number!r}")
print()

# Print the first 3000 chars of the filing text
try:
    text = f.text
    if callable(text):
        text = text()
    print("=== FIRST 3000 CHARS OF FILING TEXT ===")
    print(text[:3000])
except Exception as e:
    print(f"Error getting text: {e}")
