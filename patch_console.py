import re

with open("src/console_server.py", "r") as f:
    content = f.read()

# 1. Add firestore client import
if "from src.core.firestore_client import get_firestore_client" not in content:
    content = content.replace("from urllib.parse import urlparse", "from urllib.parse import urlparse\nfrom src.core.firestore_client import get_firestore_client")

# 2. Add pipeline_payload function
pipeline_func = """
def pipeline_payload() -> Dict[str, Any]:
    try:
        db = get_firestore_client()
        docs = db.collection("bounty_leads").stream()
        leads = []
        for doc in docs:
            d = doc.to_dict()
            leads.append({
                "id": doc.id,
                "repo": d.get("repo", "unknown"),
                "issue_number": d.get("issue_number"),
                "status": d.get("status", "queued"),
                "projected_payout": d.get("projected_payout", "PENDING DISCOVERY"),
                "title": d.get("title", "")
            })
        return {"leads": leads}
    except Exception as e:
        print(f"[ConsoleServer] Error fetching pipeline: {e}")
        return {"leads": []}
"""

if "def pipeline_payload" not in content:
    content = content.replace("def registry_payload() -> Dict[str, Any]:", pipeline_func + "\n\ndef registry_payload() -> Dict[str, Any]:")

# 3. Add dispatch route
route = """
    if route == "/api/pipeline":
        return 200, "application/json", json.dumps(pipeline_payload()).encode()
"""
if "/api/pipeline" not in content:
    content = content.replace("if route == \"/api/registry\":", route.strip() + "\n\n    if route == \"/api/registry\":")

with open("src/console_server.py", "w") as f:
    f.write(content)
