"""
Firebase Admin SDK & Google Cloud Firestore Client Module.

Provides Application Default Credentials (ADC) resolution, Firebase Admin app
initialization, Firestore client provisioning, collection accessors, ACID
transactions, and an offline JSONL fallback mode for Universal Bounty Engine V2.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("UniversalBountyV2.FirestoreClient")

# Standard configuration constants
DEFAULT_CREDENTIALS_PATH = "/Users/solveetcoagula/Desktop/activeProjects/bounty_operations/.agents/credentials.json"
DEFAULT_PROJECT_ID = "odin-500008"
DEFAULT_REGION = "us-central1"

# Firestore Collection Names
COLLECTION_BOUNTY_LEADS = "bounty_leads"
COLLECTION_SWARM_OPERATIONS = "swarm_operations"
COLLECTION_SWARM_COORDINATOR = "swarm_coordinator"
COLLECTION_BOUNTY_TASKS = "bounty_tasks"
COLLECTION_BOUNTY_MEMORY = "bounty_memory"
COLLECTION_BOUNTY_SETTLEMENTS = "bounty_settlements"


def resolve_credentials_path(custom_path: Optional[Union[str, Path]] = None) -> Optional[str]:
    """
    Resolves the Google Cloud service account credentials path.
    """
    candidate_paths = []
    if custom_path:
        candidate_paths.append(Path(custom_path))

    env_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path:
        candidate_paths.append(Path(env_path))

    candidate_paths.append(Path(DEFAULT_CREDENTIALS_PATH))

    # Search current directory and parents for .agents/credentials.json
    try:
        curr = Path.cwd()
        candidate_paths.append(curr / ".agents" / "credentials.json")
        for parent in curr.parents:
            candidate_paths.append(parent / ".agents" / "credentials.json")
    except Exception:
        pass

    for p in candidate_paths:
        try:
            resolved = p.expanduser().resolve()
            if resolved.is_file() and resolved.stat().st_size > 0:
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolved)
                return str(resolved)
        except Exception as e:
            logger.debug(f"Error inspecting credential candidate {p}: {e}")

    return None


def resolve_project_id(
    explicit_project: Optional[str] = None,
    credentials_path: Optional[str] = None,
) -> str:
    """
    Resolves the GCP project ID.
    """
    if explicit_project:
        return explicit_project

    for env_var in ["FIRESTORE_PROJECT_ID", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"]:
        val = os.getenv(env_var)
        if val:
            return val

    cred_path = credentials_path or resolve_credentials_path()
    if cred_path and os.path.isfile(cred_path):
        try:
            with open(cred_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "project_id" in data:
                    return data["project_id"]
        except Exception as e:
            logger.debug(f"Could not parse project_id from {cred_path}: {e}")

    return DEFAULT_PROJECT_ID


# =============================================================================
# Offline Firestore Implementation (ACID in-memory & JSONL persistence)
# =============================================================================


class OfflineDocumentSnapshot:
    """Snapshot representation of an offline document."""

    def __init__(self, doc_id: str, data: Optional[Dict[str, Any]], reference: Any):
        self.id = doc_id
        self._data = dict(data) if data is not None else None
        self.exists = data is not None
        self.reference = reference

    def to_dict(self) -> Optional[Dict[str, Any]]:
        return dict(self._data) if self._data is not None else None

    def get(self, field_path: str) -> Any:
        if self._data is None:
            return None
        parts = field_path.split(".")
        val = self._data
        for p in parts:
            if isinstance(val, dict) and p in val:
                val = val[p]
            else:
                return None
        return val


class OfflineDocumentReference:
    """Reference to an offline Firestore document."""

    def __init__(self, collection: "OfflineCollectionReference", doc_id: str):
        self.collection = collection
        self.id = doc_id
        self.path = f"{collection.id}/{doc_id}"

    def get(self, transaction: Optional["OfflineTransaction"] = None) -> OfflineDocumentSnapshot:
        if transaction:
            return transaction.get(self)
        with self.collection.client._lock:
            doc_data = self.collection.client._storage.get(self.collection.id, {}).get(self.id)
            return OfflineDocumentSnapshot(self.id, doc_data, self)

    def set(
        self,
        data: Dict[str, Any],
        merge: bool = False,
        transaction: Optional["OfflineTransaction"] = None,
    ) -> None:
        if transaction:
            transaction.set(self, data, merge=merge)
            return
        with self.collection.client._lock:
            col_data = self.collection.client._storage.setdefault(self.collection.id, {})
            if merge and self.id in col_data:
                col_data[self.id].update(data)
            else:
                col_data[self.id] = dict(data)
            self.collection.client._persist_collection(self.collection.id)

    def update(
        self,
        data: Dict[str, Any],
        transaction: Optional["OfflineTransaction"] = None,
    ) -> None:
        if transaction:
            transaction.update(self, data)
            return
        with self.collection.client._lock:
            col_data = self.collection.client._storage.setdefault(self.collection.id, {})
            if self.id not in col_data:
                raise KeyError(f"Document {self.path} does not exist for update")
            col_data[self.id].update(data)
            self.collection.client._persist_collection(self.collection.id)

    def delete(self, transaction: Optional["OfflineTransaction"] = None) -> None:
        if transaction:
            transaction.delete(self)
            return
        with self.collection.client._lock:
            col_data = self.collection.client._storage.get(self.collection.id, {})
            if self.id in col_data:
                del col_data[self.id]
                self.collection.client._persist_collection(self.collection.id)


class OfflineQuery:
    """Query builder for offline collections."""

    def __init__(
        self,
        collection: "OfflineCollectionReference",
        filters: Optional[List[Tuple[str, str, Any]]] = None,
        orders: Optional[List[Tuple[str, str]]] = None,
        limit_count: Optional[int] = None,
    ):
        self.collection = collection
        self.filters = filters or []
        self.orders = orders or []
        self.limit_count = limit_count

    def where(self, field: str, op: str, value: Any) -> "OfflineQuery":
        new_filters = list(self.filters)
        new_filters.append((field, op, value))
        return OfflineQuery(self.collection, new_filters, self.orders, self.limit_count)

    def order_by(self, field: str, direction: str = "ASCENDING") -> "OfflineQuery":
        new_orders = list(self.orders)
        new_orders.append((field, direction.upper()))
        return OfflineQuery(self.collection, self.filters, new_orders, self.limit_count)

    def limit(self, count: int) -> "OfflineQuery":
        return OfflineQuery(self.collection, self.filters, self.orders, count)

    def _matches_filters(self, data: Dict[str, Any]) -> bool:
        for field, op, val in self.filters:
            item_val = data.get(field)
            if op in ("==", "="):
                if item_val != val:
                    return False
            elif op == "!=":
                if item_val == val:
                    return False
            elif op == ">":
                if item_val is None or not (item_val > val):
                    return False
            elif op == ">=":
                if item_val is None or not (item_val >= val):
                    return False
            elif op == "<":
                if item_val is None or not (item_val < val):
                    return False
            elif op == "<=":
                if item_val is None or not (item_val <= val):
                    return False
            elif op == "in":
                if item_val not in val:
                    return False
            elif op == "array_contains":
                if not isinstance(item_val, (list, tuple, set)) or val not in item_val:
                    return False
        return True

    def stream(self) -> List[OfflineDocumentSnapshot]:
        with self.collection.client._lock:
            col_data = self.collection.client._storage.get(self.collection.id, {})
            results: List[OfflineDocumentSnapshot] = []

            for doc_id, data in col_data.items():
                if self._matches_filters(data):
                    ref = OfflineDocumentReference(self.collection, doc_id)
                    results.append(OfflineDocumentSnapshot(doc_id, data, ref))

            # Apply ordering
            for field, direction in reversed(self.orders):
                reverse = direction in ("DESC", "DESCENDING")
                results.sort(
                    key=lambda snap: snap.get(field) if snap.get(field) is not None else "",
                    reverse=reverse,
                )

            # Apply limit
            if self.limit_count is not None:
                results = results[: self.limit_count]

            return results

    def get(self) -> List[OfflineDocumentSnapshot]:
        return self.stream()


class OfflineCollectionReference(OfflineQuery):
    """Collection reference for offline mode."""

    def __init__(self, client: "OfflineFirestoreClient", collection_id: str):
        super().__init__(self)
        self.client = client
        self.id = collection_id

    def document(self, doc_id: Optional[str] = None) -> OfflineDocumentReference:
        if not doc_id:
            import uuid

            doc_id = uuid.uuid4().hex
        return OfflineDocumentReference(self, str(doc_id))

    def doc(self, doc_id: Optional[str] = None) -> OfflineDocumentReference:
        return self.document(doc_id)

    def add(self, data: Dict[str, Any]) -> Tuple[Any, OfflineDocumentReference]:
        ref = self.document()
        ref.set(data)
        return (None, ref)


class OfflineBatch:
    """Atomic write batch for offline Firestore."""

    def __init__(self, client: "OfflineFirestoreClient"):
        self.client = client
        self._mutations: List[Tuple[str, OfflineDocumentReference, Dict[str, Any]]] = []

    def set(
        self,
        doc_ref: OfflineDocumentReference,
        data: Dict[str, Any],
        merge: bool = False,
    ) -> "OfflineBatch":
        op = "set_merge" if merge else "set"
        self._mutations.append((op, doc_ref, dict(data)))
        return self

    def update(
        self,
        doc_ref: OfflineDocumentReference,
        data: Dict[str, Any],
    ) -> "OfflineBatch":
        self._mutations.append(("update", doc_ref, dict(data)))
        return self

    def delete(self, doc_ref: OfflineDocumentReference) -> "OfflineBatch":
        self._mutations.append(("delete", doc_ref, {}))
        return self

    def commit(self) -> None:
        with self.client._lock:
            for op, ref, data in self._mutations:
                col_data = self.client._storage.setdefault(ref.collection.id, {})
                if op == "set":
                    col_data[ref.id] = dict(data)
                elif op == "set_merge":
                    if ref.id in col_data:
                        col_data[ref.id].update(data)
                    else:
                        col_data[ref.id] = dict(data)
                elif op == "update":
                    if ref.id in col_data:
                        col_data[ref.id].update(data)
                    else:
                        col_data[ref.id] = dict(data)
                elif op == "delete":
                    col_data.pop(ref.id, None)

            # Persist affected collections
            affected_cols = {ref.collection.id for _, ref, _ in self._mutations}
            for col_id in affected_cols:
                self.client._persist_collection(col_id)
            self._mutations.clear()


class OfflineTransaction:
    """ACID Transaction for offline Firestore client."""

    def __init__(self, client: "OfflineFirestoreClient"):
        self.client = client
        self._read_snapshot: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._write_log: List[Tuple[str, OfflineDocumentReference, Dict[str, Any]]] = []

    def get(self, doc_ref: OfflineDocumentReference) -> OfflineDocumentSnapshot:
        col_id = doc_ref.collection.id
        doc_id = doc_ref.id

        with self.client._lock:
            # Capture snapshot on first read
            if col_id not in self._read_snapshot:
                self._read_snapshot[col_id] = {
                    k: dict(v) for k, v in self.client._storage.get(col_id, {}).items()
                }

            doc_data = self._read_snapshot[col_id].get(doc_id)
            return OfflineDocumentSnapshot(doc_id, doc_data, doc_ref)

    def set(
        self,
        doc_ref: OfflineDocumentReference,
        data: Dict[str, Any],
        merge: bool = False,
    ) -> None:
        op = "set_merge" if merge else "set"
        self._write_log.append((op, doc_ref, dict(data)))

    def update(
        self,
        doc_ref: OfflineDocumentReference,
        data: Dict[str, Any],
    ) -> None:
        self._write_log.append(("update", doc_ref, dict(data)))

    def delete(self, doc_ref: OfflineDocumentReference) -> None:
        self._write_log.append(("delete", doc_ref, {}))

    def _commit(self) -> None:
        with self.client._lock:
            for op, ref, data in self._write_log:
                col_data = self.client._storage.setdefault(ref.collection.id, {})
                if op == "set":
                    col_data[ref.id] = dict(data)
                elif op == "set_merge":
                    if ref.id in col_data:
                        col_data[ref.id].update(data)
                    else:
                        col_data[ref.id] = dict(data)
                elif op == "update":
                    if ref.id in col_data:
                        col_data[ref.id].update(data)
                    else:
                        col_data[ref.id] = dict(data)
                elif op == "delete":
                    col_data.pop(ref.id, None)

            affected_cols = {ref.collection.id for _, ref, _ in self._write_log}
            for col_id in affected_cols:
                self.client._persist_collection(col_id)


class OfflineFirestoreClient:
    """
    Offline mock/JSONL fallback for Google Cloud Firestore.
    Supports full ACID transaction semantics, batch writes, queries, and document CRUD.
    """

    def __init__(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        state_dir: Optional[Union[str, Path]] = None,
    ):
        self.project = project_id
        self._lock = threading.RLock()
        self._storage: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.state_dir = (
            Path(state_dir).expanduser().resolve()
            if state_dir
            else Path.cwd() / "logs" / "offline_firestore"
        )
        self._load_all_collections()

    def _get_collection_file(self, col_id: str) -> Path:
        return self.state_dir / f"{col_id}.jsonl"

    def _load_all_collections(self) -> None:
        if not self.state_dir.exists():
            return
        with self._lock:
            for jsonl_file in self.state_dir.glob("*.jsonl"):
                col_id = jsonl_file.stem
                col_data: Dict[str, Dict[str, Any]] = {}
                try:
                    with open(jsonl_file, "r", encoding="utf-8") as f:
                        for line in f:
                            clean = line.strip()
                            if clean:
                                record = json.loads(clean)
                                doc_id = record.get("_id") or record.get("id") or record.get("doc_id")
                                if doc_id:
                                    col_data[str(doc_id)] = record
                    self._storage[col_id] = col_data
                except Exception as e:
                    logger.debug(f"Could not load offline collection {col_id}: {e}")

    def _persist_collection(self, col_id: str) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            target_file = self._get_collection_file(col_id)
            temp_file = self.state_dir / f".tmp_{col_id}_{os.getpid()}"

            col_data = self._storage.get(col_id, {})
            with open(temp_file, "w", encoding="utf-8") as f:
                for doc_id, data in col_data.items():
                    save_data = dict(data)
                    if "_id" not in save_data:
                        save_data["_id"] = doc_id
                    f.write(json.dumps(save_data, ensure_ascii=False) + "\n")

            temp_file.replace(target_file)
        except Exception as e:
            logger.debug(f"Could not persist offline collection {col_id}: {e}")

    def collection(self, collection_name: str) -> OfflineCollectionReference:
        return OfflineCollectionReference(self, collection_name)

    def batch(self) -> OfflineBatch:
        return OfflineBatch(self)

    def transaction(self) -> OfflineTransaction:
        return OfflineTransaction(self)

    def run_transaction(self, transaction_func: Callable[[OfflineTransaction], Any]) -> Any:
        """Executes a function inside an ACID offline transaction."""
        with self._lock:
            txn = self.transaction()
            result = transaction_func(txn)
            txn._commit()
            return result


# =============================================================================
# Firebase & Real Firestore Factory
# =============================================================================


def initialize_firebase_app(
    credentials_path: Optional[str] = None,
    project_id: Optional[str] = None,
    app_name: Optional[str] = None,
) -> Any:
    """
    Initializes or retrieves the Firebase Admin App using ADC or service account credentials.
    """
    try:
        import firebase_admin
        from firebase_admin import credentials as fb_credentials
    except ImportError:
        logger.warning("firebase_admin not installed; operating in fallback mode.")
        return None

    cred_file = resolve_credentials_path(credentials_path)
    proj_id = resolve_project_id(project_id, cred_file)
    target_name = app_name or firebase_admin._DEFAULT_APP_NAME

    try:
        existing_app = firebase_admin.get_app(name=target_name)
        if existing_app:
            return existing_app
    except ValueError:
        pass

    options: Dict[str, Any] = {"projectId": proj_id}

    try:
        if cred_file and os.path.isfile(cred_file):
            fb_cred = fb_credentials.Certificate(cred_file)
        else:
            fb_cred = fb_credentials.ApplicationDefault()

        if target_name == firebase_admin._DEFAULT_APP_NAME:
            app = firebase_admin.initialize_app(fb_cred, options=options)
        else:
            app = firebase_admin.initialize_app(fb_cred, options=options, name=target_name)

        logger.info(f"Firebase Admin App '{target_name}' initialized for project '{proj_id}'")
        return app
    except Exception as e:
        logger.warning(f"Could not initialize Firebase Admin App: {e}")
        return None


def get_firestore_client(
    project_id: Optional[str] = None,
    credentials_path: Optional[str] = None,
    database: Optional[str] = None,
    force_offline: bool = False,
    offline_fallback: bool = True,
) -> Union[Any, OfflineFirestoreClient]:
    """
    Returns a configured Firestore Client instance (Real Google Cloud client or Offline client).
    """
    if force_offline or os.getenv("FIRESTORE_OFFLINE") in ("1", "true", "True"):
        logger.info("Using OfflineFirestoreClient due to force_offline/FIRESTORE_OFFLINE setting")
        return OfflineFirestoreClient(project_id=project_id or DEFAULT_PROJECT_ID)

    cred_file = resolve_credentials_path(credentials_path)
    proj_id = resolve_project_id(project_id, cred_file)

    try:
        from google.cloud import firestore
        from google.oauth2 import service_account

        if cred_file and os.path.isfile(cred_file):
            client_credentials = service_account.Credentials.from_service_account_file(cred_file)
            if database and database != "(default)":
                client = firestore.Client(project=proj_id, credentials=client_credentials, database=database)
            else:
                client = firestore.Client(project=proj_id, credentials=client_credentials)
        else:
            if database and database != "(default)":
                client = firestore.Client(project=proj_id, database=database)
            else:
                client = firestore.Client(project=proj_id)

        # Test connectivity by accessing project property
        _ = client.project
        logger.info(f"Connected to live Google Cloud Firestore for project '{proj_id}'")
        return client
    except Exception as e:
        if offline_fallback:
            logger.warning(f"Live Firestore connection unavailable ({e}); falling back to OfflineFirestoreClient.")
            return OfflineFirestoreClient(project_id=proj_id)
        raise


# =============================================================================
# Collection Accessors
# =============================================================================


def get_collection(
    collection_name: str,
    db: Optional[Any] = None,
) -> Any:
    """Returns a Firestore CollectionReference."""
    client = db if db is not None else get_firestore_client()
    return client.collection(collection_name)


def get_leads_collection(
    db: Optional[Any] = None,
    collection_name: str = COLLECTION_BOUNTY_LEADS,
) -> Any:
    """Returns the bounty_leads collection reference."""
    return get_collection(collection_name, db)


def get_operations_collection(
    db: Optional[Any] = None,
    collection_name: str = COLLECTION_SWARM_OPERATIONS,
) -> Any:
    """Returns the swarm_operations collection reference."""
    return get_collection(collection_name, db)


def get_memory_collection(
    db: Optional[Any] = None,
    collection_name: str = COLLECTION_BOUNTY_MEMORY,
) -> Any:
    """Returns the bounty_memory collection reference."""
    return get_collection(collection_name, db)


def get_settlements_collection(
    db: Optional[Any] = None,
    collection_name: str = COLLECTION_BOUNTY_SETTLEMENTS,
) -> Any:
    """Returns the bounty_settlements collection reference."""
    return get_collection(collection_name, db)


def get_coordinator_collection(
    db: Optional[Any] = None,
    collection_name: str = COLLECTION_SWARM_COORDINATOR,
) -> Any:
    """Returns the swarm_coordinator collection reference."""
    return get_collection(collection_name, db)


# =============================================================================
# Atomic Lead Claiming Transaction
# =============================================================================


def claim_lead_atomic(
    db: Any,
    lead_id: str,
    worker_id: str,
    collection_name: str = COLLECTION_BOUNTY_LEADS,
    lock_timeout_sec: int = 300,
) -> bool:
    """
    Atomically claims a lead within a Firestore ACID transaction.

    Verification rules:
    - If document does not exist, claim fails.
    - If status is 'claimed' or 'running_orbstack' and lock has not expired, claim fails.
    - If available or lock expired, transitions status to 'claimed', records worker_id,
      updates lock_acquired_at, and commits.

    Returns:
        True if successfully claimed; False if already claimed or unavailable.
    """
    col = db.collection(collection_name)
    doc_ref = col.document(lead_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    now_ts = time.time()

    # Check if we are running with live Google Cloud Firestore or Offline client
    is_offline = isinstance(db, OfflineFirestoreClient)

    def _txn_logic(transaction: Any) -> bool:
        snapshot = doc_ref.get(transaction=transaction) if is_offline else doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False

        data = snapshot.to_dict() or {}
        current_status = data.get("status")

        # Check if already active and unexpired
        if current_status in ("claimed", "running_orbstack", "completed"):
            if current_status == "completed":
                return False

            lock_acquired_str = data.get("lock_acquired_at")
            if lock_acquired_str:
                try:
                    lock_time = datetime.fromisoformat(lock_acquired_str).timestamp()
                    if now_ts - lock_time < lock_timeout_sec:
                        return False  # Still actively locked
                except Exception:
                    pass

        # Update lead to claimed
        update_data = {
            "status": "claimed",
            "claimed_by": worker_id,
            "lock_acquired_at": now_iso,
            "updated_at": now_iso,
        }

        if is_offline:
            transaction.update(doc_ref, update_data)
        else:
            transaction.update(doc_ref, update_data)

        return True

    try:
        if is_offline:
            return db.run_transaction(_txn_logic)
        else:
            from google.cloud import firestore

            @firestore.transactional
            def _live_txn(transaction: firestore.Transaction) -> bool:
                return _txn_logic(transaction)

            txn = db.transaction()
            return _live_txn(txn)
    except Exception as e:
        logger.warning(f"Failed atomic claim on lead {lead_id}: {e}")
        return False


def claim_lead_transaction(
    db: Any,
    lead_id: str,
    worker_id: str,
    collection_name: str = COLLECTION_BOUNTY_LEADS,
    lock_timeout_sec: int = 300,
) -> bool:
    """Alias for claim_lead_atomic to satisfy interface contracts."""
    return claim_lead_atomic(
        db=db,
        lead_id=lead_id,
        worker_id=worker_id,
        collection_name=collection_name,
        lock_timeout_sec=lock_timeout_sec,
    )
