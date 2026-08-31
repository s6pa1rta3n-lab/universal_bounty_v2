import os
from dotenv import load_dotenv
from src.core.firestore_client import get_firestore_client

load_dotenv()
db = get_firestore_client()

docs = db.collection("bounty_leads").limit(1).stream()
for doc in docs:
    d = doc.to_dict()
    for k, v in d.items():
        print(f"{k}: {v}")
