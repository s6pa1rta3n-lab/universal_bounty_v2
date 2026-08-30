import os
import json
import subprocess
from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter
from src.core.firestore_client import get_firestore_client

load_dotenv()

def main():
    db = get_firestore_client()
    docs = db.collection("bounty_leads").where(filter=FieldFilter("status", "==", "queued")).stream()
    
    pruned_count = 0
    total_checked = 0
    
    print("Scrubbing 'queued' leads to remove issues assigned to competitors...")
    for doc in docs:
        data = doc.to_dict()
        issue_url = data.get("issue_url")
        if not issue_url or "github.com" not in issue_url:
            continue
            
        total_checked += 1
        
        try:
            # Check issue state and assignees via GitHub CLI
            res = subprocess.run(
                ["gh", "issue", "view", issue_url, "--json", "assignees,state"],
                capture_output=True, text=True
            )
            
            if res.returncode == 0:
                gh_data = json.loads(res.stdout)
                state = gh_data.get("state")
                assignees = gh_data.get("assignees", [])
                
                is_assigned = len(assignees) > 0
                is_assigned_to_us = any(a.get("login", "").lower() == "s6pa1rta3n-lab" for a in assignees)
                
                if state == "CLOSED" or (is_assigned and not is_assigned_to_us):
                    print(f"Pruning {issue_url} (State: {state}, Assignees: {[a.get('login') for a in assignees]})")
                    db.collection("bounty_leads").document(doc.id).update({"status": "abandoned"})
                    pruned_count += 1
            else:
                pass
        except Exception as e:
            print(f"Error checking {issue_url}: {e}")
            
    print(f"\nCleanup complete. Checked {total_checked} leads, pruned {pruned_count} that were closed or assigned to competitors.")

if __name__ == "__main__":
    main()
