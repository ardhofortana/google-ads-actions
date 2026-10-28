"""Loads guardrails.yaml and exposes typed accessors and range checks."""

import importlib.resources
from dataclasses import dataclass
from typing import Any

import yaml

from ads_actions.errors import Refused


@dataclass(frozen=True)
class Range:
    min: int
    max: int

    def check(self, label: str, value: int) -> None:
        if not self.min <= value <= self.max:
            raise Refused(
                f"{label} Rp{value:,} is outside the allowed range "
                f"Rp{self.min:,} to Rp{self.max:,}."
            )


@dataclass(frozen=True)
class Guardrails:
    allowed_user_emails: frozenset[str]
    allowed_customer_ids: frozenset[str]
    allowed_campaign_ids: frozenset[str]
    currency: str
    budget: Range
    budget_max_change_pct: float
    cpc_ceiling: Range
    target_cpa: Range
    bid: Range
    bid_max_change_pct: float
    max_keyword_bids_per_proposal: int
    device_adjustment_pct: Range
    keyword_match_types: tuple[str, ...]
    max_negatives_per_proposal: int
    max_keywords_per_proposal: int
    proposal_ttl_hours: int
    emergency_cost_multiple: float
    emergency_window_hours: int
    emergency_min_clicks: int
    emergency_min_ctr: float
    asset_url_prefix: str
    ad_text_banned: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Guardrails":
        budget = d["budget_idr"]
        ep = d["emergency_pause"]
        it = ep["invalid_traffic"]
        return cls(
            allowed_user_emails=frozenset(e.strip().lower() for e in d["allowed_user_emails"]),
            allowed_customer_ids=frozenset(str(c) for c in d["allowed_customer_ids"]),
            allowed_campaign_ids=frozenset(str(c) for c in d["allowed_campaign_ids"]),
            currency=d["currency"],
            budget=Range(int(budget["min"]), int(budget["max"])),
            budget_max_change_pct=float(budget["max_change_pct_per_proposal"]),
            cpc_ceiling=Range(int(d["cpc_ceiling_idr"]["min"]), int(d["cpc_ceiling_idr"]["max"])),
            target_cpa=Range(int(d["target_cpa_idr"]["min"]), int(d["target_cpa_idr"]["max"])),
            bid=Range(int(d["bid_idr"]["min"]), int(d["bid_idr"]["max"])),
            bid_max_change_pct=float(d["bid_idr"]["max_change_pct_per_proposal"]),
            max_keyword_bids_per_proposal=int(d["max_keyword_bids_per_proposal"]),
            device_adjustment_pct=Range(int(d["device_bid_adjustment_pct"]["min"]), int(d["device_bid_adjustment_pct"]["max"])),
            keyword_match_types=tuple(d["keyword_match_types"]),
            max_negatives_per_proposal=int(d["max_negatives_per_proposal"]),
            max_keywords_per_proposal=int(d["max_keywords_per_proposal"]),
            proposal_ttl_hours=int(d["proposal_ttl_hours"]),
            emergency_cost_multiple=float(ep["cost_multiple_of_budget"]),
            emergency_window_hours=int(it["window_hours"]),
            emergency_min_clicks=int(it["min_clicks"]),
            emergency_min_ctr=float(it["min_ctr"]),
            asset_url_prefix=d["asset_url_prefix"],
            ad_text_banned=tuple(d["ad_text_banned"]),
        )

    @classmethod
    def load(cls, path: str | None = None) -> "Guardrails":
        """Loads the bundled guardrails.yaml (``path`` is for tests only)."""
        if path:
            with open(path) as f:
                return cls.from_dict(yaml.safe_load(f))
        text = importlib.resources.files("ads_actions").joinpath("guardrails.yaml").read_text()
        return cls.from_dict(yaml.safe_load(text))

    # Checks shared by every tool.

    def check_user(self, email: str | None) -> None:
        from ads_actions.errors import Forbidden

        if not email or email.strip().lower() not in self.allowed_user_emails:
            raise Forbidden()

    def check_customer(self, customer_id: str | int) -> str:
        cid = "".join(ch for ch in str(customer_id) if ch.isdigit())
        if cid not in self.allowed_customer_ids:
            raise Refused(f"Customer {customer_id} is not allowed.")
        return cid

    def check_campaign(self, campaign_id: str | int) -> str:
        cid = str(campaign_id).strip()
        if cid not in self.allowed_campaign_ids:
            raise Refused(f"Campaign {campaign_id} is not in the allowlist.")
        return cid

    def check_match_type(self, match_type: str) -> str:
        mt = str(match_type).strip().upper()
        if mt not in self.keyword_match_types:
            raise Refused(
                f"Match type {match_type} is not allowed. Allowed: {', '.join(self.keyword_match_types)}."
            )
        return mt
