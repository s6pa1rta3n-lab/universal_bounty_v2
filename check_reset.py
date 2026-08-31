import os
from google.cloud.firestore_v1.base_query import FieldFilter
from src.core.firestore_client import get_firestore_client
from dotenv import load_dotenv

load_dotenv()
db = get_firestore_client()
col = db.collection("bounty_leads")
docs = ["cylo_traders_agrocylo_pip_172", "earnquestone_stellar_earn_2357", "mdtechlabs_gasguard_932", "orbit_wal_contract_82"]
for doc_id in docs:
    d = col.document(doc_id).get().to_dict()
    print(f"{doc_id}: {d.get('status')}")
