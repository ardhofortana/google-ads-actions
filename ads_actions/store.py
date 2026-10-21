"""Proposal and audit storage (Firestore collection ``ads_actions``).

Documents are never deleted. ``claim`` moves a proposal out of ``proposed`` in a
transaction, so two execute calls racing on two instances can't both run it.
"""

import copy
import datetime as dt
from typing import Protocol

COLLECTION = "ads_actions"


class Store(Protocol):
    async def create(self, doc: dict) -> bool: ...
    async def get(self, change_id: str) -> dict | None: ...
    async def update(self, change_id: str, fields: dict) -> None: ...
    async def claim(self, change_id: str, from_status: str, to_status: str) -> bool: ...
    async def by_status(self, status: str) -> list[dict]: ...
    async def executed_since(self, since: dt.datetime) -> list[dict]: ...
    async def failed_since(self, since: dt.datetime) -> list[dict]: ...


def _check_no_nested_arrays(value, path="doc"):
    """Firestore rejects arrays that directly contain arrays."""
    if isinstance(value, dict):
        for k, v in value.items():
            _check_no_nested_arrays(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            if isinstance(v, list):
                raise ValueError(f"Nested array at {path}[{i}]")
            _check_no_nested_arrays(v, f"{path}[{i}]")


class MemoryStore:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    async def create(self, doc):
        _check_no_nested_arrays(doc)
        if doc["change_id"] in self.docs:
            return False
        self.docs[doc["change_id"]] = copy.deepcopy(doc)
        return True

    async def get(self, change_id):
        d = self.docs.get(change_id)
        return copy.deepcopy(d) if d else None

    async def update(self, change_id, fields):
        _check_no_nested_arrays(fields)
        self.docs[change_id].update(copy.deepcopy(fields))

    async def claim(self, change_id, from_status, to_status):
        d = self.docs.get(change_id)
        if not d or d["status"] != from_status:
            return False
        d["status"] = to_status
        return True

    async def by_status(self, status):
        return [copy.deepcopy(d) for d in self.docs.values() if d["status"] == status]

    async def executed_since(self, since):
        return [
            copy.deepcopy(d)
            for d in self.docs.values()
            if d.get("executed_at") and d["executed_at"] >= since
        ]

    async def failed_since(self, since):
        return [copy.deepcopy(d) for d in self.docs.values() if d.get("failed_at") and d["failed_at"] >= since]


class FirestoreStore:
    def __init__(self, project: str | None = None, database: str | None = None):
        from google.cloud import firestore

        self._fs = firestore
        self._db = firestore.AsyncClient(project=project, database=database or "(default)")
        self._col = self._db.collection(COLLECTION)

    async def create(self, doc):
        from google.api_core.exceptions import AlreadyExists

        try:
            await self._col.document(doc["change_id"]).create(doc)
            return True
        except AlreadyExists:
            return False

    async def get(self, change_id):
        snap = await self._col.document(change_id).get()
        return snap.to_dict() if snap.exists else None

    async def update(self, change_id, fields):
        await self._col.document(change_id).update(fields)

    async def claim(self, change_id, from_status, to_status):
        ref = self._col.document(change_id)
        transaction = self._db.transaction()

        @self._fs.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            if not snap.exists or snap.get("status") != from_status:
                return False
            tx.update(ref, {"status": to_status})
            return True

        return await run(transaction)

    async def by_status(self, status):
        # Single-field filters only, so no composite index is needed.
        q = self._col.where(filter=self._fs.FieldFilter("status", "==", status))
        return [s.to_dict() async for s in q.stream()]

    async def executed_since(self, since):
        q = self._col.where(filter=self._fs.FieldFilter("executed_at", ">=", since))
        return [s.to_dict() async for s in q.stream()]

    async def failed_since(self, since):
        q = self._col.where(filter=self._fs.FieldFilter("failed_at", ">=", since))
        return [s.to_dict() async for s in q.stream()]
