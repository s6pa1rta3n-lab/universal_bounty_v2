import os
import json
from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter
from src.core.firestore_client import get_firestore_client

load_dotenv()
db = get_firestore_client()

docs = db.collection("bounty_leads").where(filter=FieldFilter("repo", "==", "s6pa1rta3n-lab/universal_bounty_fleet")).stream()

found = False
for doc in docs:
    found = True
    d = doc.to_dict()
    print("--- META BOUNTY LEAD ---")
    print(f"ID: {doc.id}")
    print(f"Title: {d.get('title')}")
    print(f"Author: {d.get('author')}")
    print(f"Status: {d.get('status')}")
    print(f"Priority: {d.get('priority')}")
    print(f"Payout USD: {d.get('projected_payout_usd')}")
    print(f"Reason: {d.get('qualification_reason')}")
    print("------------------------")

if not found:
    print("No meta issues found in Firestore!")
