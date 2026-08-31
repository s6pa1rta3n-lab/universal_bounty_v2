from src.core.firestore_client import get_firestore_client
from dotenv import load_dotenv

load_dotenv()
db = get_firestore_client()
docs = list(db.collection("bounty_leads").stream())

for doc in docs:
    if "base" in doc.id.lower():
        print(doc.id)
