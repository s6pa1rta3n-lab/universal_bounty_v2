from src.core.firestore_client import get_firestore_client
db = get_firestore_client()
docs = db.collection('bounty_leads').stream()
for doc in docs:
    data = doc.to_dict()
    if 'base-org' in data.get('repo', '') or '501' in doc.id:
        print(f"Found bad doc: {doc.id}")
        db.collection('bounty_leads').document(doc.id).update({"status": "abandoned"})
        print("Updated to abandoned.")
