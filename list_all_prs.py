import os
from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter
from src.core.firestore_client import get_firestore_client

load_dotenv()
db = get_firestore_client()
docs = db.collection("bounty_leads").where(filter=FieldFilter("status", "==", "pr_open")).stream()
c = 0
for doc in docs:
    d = doc.to_dict()
    if "grantfox" in str(d).lower():
        print(f"GRANTFOX: {doc.id}")
        c += 1
print(f"Total Grantfox pr_open: {c}")
