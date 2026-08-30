import os
import subprocess
import json
from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter
from src.core.firestore_client import get_firestore_client

load_dotenv()
db = get_firestore_client()
docs = db.collection("bounty_leads").where(filter=FieldFilter("status", "==", "pr_open")).stream()

closed_count = 0
for doc in docs:
    d = doc.to_dict()
    if "grantfox" in str(d).lower():
        pr_url = d.get("pr_url")
        if pr_url and "github.com" in pr_url:
            # Check state
            result = subprocess.run(["gh", "pr", "view", pr_url, "--json", "state"], capture_output=True, text=True)
            if result.returncode == 0:
                state = json.loads(result.stdout).get("state")
                if state == "OPEN":
                    print(f"Closing PR: {pr_url}")
                    close_res = subprocess.run(["gh", "pr", "close", pr_url], capture_output=True, text=True)
                    if close_res.returncode == 0:
                        # Update Firestore
                        db.collection("bounty_leads").document(doc.id).update({"status": "failed"})
                        closed_count += 1
                        print(f"Successfully closed and marked as failed: {pr_url}")
                    else:
                        print(f"Failed to close PR via gh: {close_res.stderr}")
                elif state == "MERGED":
                    print(f"Updating {pr_url} to completed in Firestore since it was merged!")
                    db.collection("bounty_leads").document(doc.id).update({"status": "completed"})
            else:
                pass

print(f"\nCleanup complete. Closed {closed_count} stalled GrantFox PRs.")
