import os
from dotenv import load_dotenv
from src.core.firestore_client import get_firestore_client

load_dotenv()

def main():
    db = get_firestore_client()
    docs = db.collection("bounty_memory").stream()
    
    grantfox_counts = {}
    
    for doc in docs:
        data = doc.to_dict()
        category = data.get("category", "").lower()
        title = data.get("title", "").lower()
        body = data.get("body", "").lower()
        labels = [l.lower() for l in data.get("labels", [])]
        
        is_grantfox = (
            "grantfox" in category or 
            "grantfox" in title or 
            "grantfox" in body or 
            any("grantfox" in l for l in labels)
        )
        
        if is_grantfox:
            status = data.get("status", "unknown")
            grantfox_counts[status] = grantfox_counts.get(status, 0) + 1
            
    print("GrantFox PR Status Breakdown:")
    for status, count in grantfox_counts.items():
        print(f"{status}: {count}")

if __name__ == "__main__":
    main()
