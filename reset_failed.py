import os
from google.cloud.firestore_v1.base_query import FieldFilter
from src.core.firestore_client import get_firestore_client
from dotenv import load_dotenv

load_dotenv()
db = get_firestore_client()
col = db.collection("bounty_leads")
failed = list(col.where(filter=FieldFilter("status", "==", "failed")).limit(4).stream())

for doc in failed:
    doc.reference.update({"status": "pending_triage"})
    print(f"Reset {doc.id} to pending_triage.")
