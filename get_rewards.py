import os
from dotenv import load_dotenv
from src.core.firestore_client import get_firestore_client

load_dotenv()
db = get_firestore_client()

docs = db.collection("bounty_leads").stream()
for doc in docs:
    d = doc.to_dict()
    if "grantfox" in str(d).lower():
        pr_url = d.get("pr_url") or d.get("issue_url")
        status = d.get("status")
        if status in ["completed", "pr_open", "failed"]:
            payout = d.get("projected_payout") or d.get("projected_payout_usd") or "Unknown"
            print(f"[{status.upper()}] {pr_url} -> {payout}")
