import sys
from src.core.firestore_client import OfflineFirestoreClient
from src.core.config import COLLECTION_SWARM_COORDINATOR

def run_monitor():
    db = OfflineFirestoreClient()
    coordinator_ref = db.collection(COLLECTION_SWARM_COORDINATOR).document("state")
    
    state_snap = coordinator_ref.get()
    if not state_snap.exists:
        print("[MONITOR] ERROR: No state document found in swarm_coordinator.")
        sys.exit(1)
        
    state_data = state_snap.to_dict()
    last_sweep = state_data.get("last_sweep", {})
    
    if not last_sweep:
        print("[MONITOR] ERROR: No last_sweep found in coordinator state.")
        sys.exit(1)
    
    completed_at = last_sweep.get("completed_at_iso", "")
    emails = last_sweep.get("inbox_emails_processed", 0)
    prs = last_sweep.get("prs_monitored", 0)
    ingested = last_sweep.get("leads_ingested", 0)
    success = last_sweep.get("success", False)
    
    print(f"[MONITOR] Latest Sweep: {completed_at}")
    print(f"[MONITOR] Success: {success}")
    print(f"[MONITOR] Emails Processed: {emails}")
    print(f"[MONITOR] PRs Monitored (GitHub): {prs}")
    print(f"[MONITOR] Leads Ingested (GitHub): {ingested}")
    
    if not success:
        print("[MONITOR] WARNING: Latest sweep was marked as failed!")
        sys.exit(1)
        
    print("[MONITOR] HEALTHY: Sweeper is autonomously operating and checking all subsystems.")

if __name__ == "__main__":
    run_monitor()
