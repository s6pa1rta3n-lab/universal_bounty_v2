from src.core.firestore_client import get_firestore_client
from dotenv import load_dotenv

load_dotenv()
db = get_firestore_client()
docs = list(db.collection("bounty_leads").stream())

bridge_docs = [d for d in docs if "bridge" in d.id and "501" in d.id]
print(f"Found {len(bridge_docs)} documents containing 'bridge' and '501'")
for doc in bridge_docs:
    d = doc.to_dict()
    print(f"ID: {doc.id} | Status: {d.get('status')}")
