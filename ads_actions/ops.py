"""Builds Google Ads MutateOperations as canonical proto-JSON dicts.

Operations are stored in Firestore exactly as they are sent, so they are kept
as dicts and converted to protos only at the API boundary. ``canonical`` runs
every dict through the real MutateOperation proto, which rejects unknown fields
and normalises enums, int64s and field masks.
"""

from typing import Any

from google.ads.googleads.v26.services.types.google_ads_service import MutateOperation
from google.protobuf import json_format


def _camel(path: str) -> str:
    head, *rest = path.split("_")
    return head + "".join(w.title() for w in rest)


def _mask(paths: list[str]) -> str:
    return ",".join(".".join(_camel(p) for p in path.split(".")) for path in paths)


def to_proto(op: dict) -> Any:
    return json_format.ParseDict(op, MutateOperation()._pb)


def canonical(op: dict) -> dict:
    return json_format.MessageToDict(to_proto(op), preserving_proto_field_name=True)


def create(kind: str, resource: dict) -> dict:
    return canonical({f"{kind}_operation": {"create": resource}})


def update(kind: str, resource: dict, mask: list[str]) -> dict:
    return canonical({f"{kind}_operation": {"update": resource, "update_mask": _mask(mask)}})


def remove(kind: str, resource_name: str) -> dict:
    return canonical({f"{kind}_operation": {"remove": resource_name}})


def describe(op: dict) -> tuple[str, str, Any]:
    """Returns (kind, action, payload) for a canonical operation."""
    (key, body), = op.items()
    kind = key.removesuffix("_operation")
    for action in ("create", "update", "remove"):
        if action in body:
            return kind, action, body[action]
    raise ValueError(f"Unrecognised operation: {op}")


class RN:
    """Resource-name helpers."""

    def __init__(self, customer_id: str):
        self.c = customer_id

    def campaign(self, cid) -> str:
        return f"customers/{self.c}/campaigns/{cid}"

    def campaign_criterion(self, cid, crit) -> str:
        return f"customers/{self.c}/campaignCriteria/{cid}~{crit}"

    def ad_group(self, ag) -> str:
        return f"customers/{self.c}/adGroups/{ag}"

    def ad_group_criterion(self, ag, crit) -> str:
        return f"customers/{self.c}/adGroupCriteria/{ag}~{crit}"

    def ad(self, ad_id) -> str:
        return f"customers/{self.c}/ads/{ad_id}"

    def asset(self, asset_id) -> str:
        return f"customers/{self.c}/assets/{asset_id}"

    def campaign_asset(self, cid, asset_id, field_type) -> str:
        return f"customers/{self.c}/campaignAssets/{cid}~{asset_id}~{field_type}"
