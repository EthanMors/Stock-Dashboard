import sys
sys.path.insert(0, "stock-dashboard")

from edgar import get_entity, set_identity

set_identity("ethanjosemorris@gmail.com")

entity = get_entity("1589122")
filings = entity.get_filings(form="SC 13D")

f = filings[0]
print("Filing attrs:", [a for a in dir(f) if not a.startswith("_")])
print()

# Try obj()
try:
    obj = f.obj()
    print("obj type:", type(obj))
    if obj:
        print("obj attrs:", [a for a in dir(obj) if not a.startswith("_")])
except Exception as e:
    print(f"obj() failed: {e}")

# Try header
try:
    print("header:", f.header)
except Exception as e:
    print(f"header failed: {e}")

# Try document
try:
    docs = f.documents
    print(f"documents: {docs}")
except Exception as e:
    print(f"documents failed: {e}")

# Try text with encoding fix
try:
    import httpx
    resp = httpx.get(f"https://www.sec.gov/Archives/edgar/full-index/", timeout=10)
    print("httpx works")
except Exception as e:
    print(f"httpx: {e}")

# Try to get filing text with error handling
try:
    text = f.text
    if callable(text):
        text = text()
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    print("Text first 2000 chars:")
    print(text[:2000])
except Exception as e:
    print(f"text failed: {e}")
    # Try getting raw document
    try:
        primary = f.primary_document
        print(f"primary_document: {primary}")
    except Exception as e2:
        print(f"primary_document failed: {e2}")
