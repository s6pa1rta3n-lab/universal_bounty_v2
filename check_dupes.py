from src.core.firestore_client import get_firestore_client
from dotenv import load_dotenv

load_dotenv()
db = get_firestore_client()
docs = list(db.collection("bounty_leads").stream())

base_docs = [d for d in docs if "base-org" in d.id]
print(f"Found {len(base_docs)} documents containing 'base-org'")
for doc in base_docs:
    d = doc.to_dict()
    print(f"ID: {doc.id} | Status: {d.get('status')}")
