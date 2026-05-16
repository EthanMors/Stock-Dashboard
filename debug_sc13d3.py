import sys, os
sys.path.insert(0, "stock-dashboard")
os.environ["PYTHONIOENCODING"] = "utf-8"

from edgar import get_entity, set_identity

set_identity("ethanjosemorris@gmail.com")

entity = get_entity("1589122")
filings = entity.get_filings(form="SC 13D")

f = filings[0]
print(f"accession: {f.accession_number}, date: {f.filing_date}")
print()

# Try all_entities - might have subject company
try:
    ents = f.all_entities
    print("all_entities:", ents)
except Exception as e:
    print(f"all_entities failed: {e}")

# Try index_headers
try:
    ih = f.index_headers
    print("index_headers type:", type(ih))
    if isinstance(ih, dict):
        for k, v in list(ih.items())[:20]:
            print(f"  {k}: {v!r}")
    else:
        print("index_headers:", str(ih)[:500])
except Exception as e:
    print(f"index_headers failed: {e}")

# Try items / parsed_items
try:
    items = f.items
    print("items:", items)
except Exception as e:
    print(f"items failed: {e}")

try:
    pi = f.parsed_items
    print("parsed_items type:", type(pi))
    print("parsed_items:", str(pi)[:500])
except Exception as e:
    print(f"parsed_items failed: {e}")

# Try sgml to get raw header with subject company
try:
    sgml_text = f.full_text_submission
    if sgml_text:
        # Print first part which has the header
        first_part = str(sgml_text)[:3000]
        print("full_text_submission (first 3000 chars):")
        print(first_part.encode("ascii", errors="replace").decode())
except Exception as e:
    print(f"full_text_submission failed: {e}")
