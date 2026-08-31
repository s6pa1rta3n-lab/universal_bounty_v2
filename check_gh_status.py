import os
import subprocess
import json
from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter
from src.core.firestore_client import get_firestore_client

load_dotenv()
db = get_firestore_client()
docs = db.collection("bounty_leads").where(filter=FieldFilter("status", "==", "pr_open")).stream()

for doc in docs:
    d = doc.to_dict()
    if "grantfox" in str(d).lower():
        pr_url = d.get("pr_url")
        if pr_url and "github.com" in pr_url:
            try:
                # e.g., gh pr view https://github.com/org/repo/pull/1 --json state,isDraft
                result = subprocess.run(["gh", "pr", "view", pr_url, "--json", "state,isDraft"], capture_output=True, text=True)
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    state = data.get("state")
                    draft = data.get("isDraft")
                    print(f"{pr_url} -> State: {state}, Draft: {draft}")
                else:
                    print(f"{pr_url} -> Error: {result.stderr.strip()}")
            except Exception as e:
                print(f"{pr_url} -> Exception: {e}")
        else:
            print(f"Missing/Invalid PR URL for doc {doc.id}: {pr_url}")
