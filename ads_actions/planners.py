"""One planner per change type.

A planner turns a request into a ``Plan``: guardrail checks, the live "before"
snapshot, and the exact mutate operations. Planners are deterministic for a
given request and account state, which is what makes execute safe: execute
re-runs the planner and refuses unless it produces the same "before" and the
same operations that were validated and shown at proposal time.

Each change type also knows how to read its "after" values and how to build
the request that undoes it.
"""

import re
from dataclasses import dataclass, field
from typing import Callable

from ads_actions import ops, schedule, text_rules
from ads_actions.errors import Refused
from ads_actions.gateway import Gateway
from ads_actions.guardrails import Guardrails

MICROS = 1_000_000
STATUSES = ("ENABLED", "PAUSED")
KEYWORD_MAX_CHARS = 80
KEYWORD_MAX_WORDS = 10
MAX_ASSETS_PER_PROPOSAL = 10
ASSET_FIELD_TYPES = ("SITELINK", "CALLOUT", "STRUCTURED_SNIPPET")
SNIPPET_HEADERS = (
    "Amenities", "Brands", "Courses", "Degree programs", "Destinations",
    "Featured hotels", "Insurance coverage", "Models", "Neighborhoods",
    "Service catalog", "Shows", "Styles", "Types",
)
PIN_FIELDS = ("HEADLINE_1", "HEADLINE_2", "HEADLINE_3", "DESCRIPTION_1", "DESCRIPTION_2")
STRATEGY_FROM_API = {"TARGET_SPEND": "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS": "MAXIMIZE_CONVERSIONS", "MANUAL_CPC": "MANUAL_CPC"}


@dataclass
class Ctx:
    gw: Gateway
    g: Guardrails
    # The executed audit-log entry being undone. Set only by the engine for
    # propose_undo and for executing an undo proposal; never from tool input.
    # Its presence is what exempts a restore from the ±% change limits.
    undo_of: dict | None = None

    @property
    def rn(self) -> ops.RN:
        return ops.RN(self.gw.customer_id)


@dataclass
class Plan:
    summary: str
    before: dict
    operations: list[dict]
    rows: list[dict]  # {"item", "before", "after"}
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class ChangeType:
    plan: Callable[[Ctx, dict], Plan]
    after: Callable[[Ctx, dict, dict], dict]  # (ctx, request, mutate_result)
    inverse: Callable[[dict], tuple[str, dict]]  # (stored doc) -> (type, request)


# -- shared helpers ------------------------------------------------------------


def _id(value, label: str) -> str:
    s = str(value).strip()
    if not s.isdigit():
        raise Refused(f"{label} must be a numeric ID, got {value!r}.")
    return s


def _int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refused(f"{label} must be a whole number, got {value!r}.")
    return value


def _status(value) -> str:
    s = str(value).strip().upper()
    if s not in STATUSES:
        raise Refused(f"Status must be ENABLED or PAUSED, got {value!r}.")
    return s


def _campaign(ctx: Ctx, campaign_id) -> dict:
    cid = ctx.g.check_campaign(_id(campaign_id, "campaign_id"))
    camp = ctx.gw.campaign(cid)
    if not camp:
        raise Refused(f"Campaign {cid} was not found in the account.")
    return camp


def _ad_group(ctx: Ctx, ad_group_id) -> dict:
    ag = ctx.gw.ad_group(_id(ad_group_id, "ad_group_id"))
    if not ag:
        raise Refused(f"Ad group {ad_group_id} was not found.")
    if ag["campaign_id"] not in ctx.g.allowed_campaign_ids:
        raise Refused(f"Ad group {ad_group_id} belongs to campaign {ag['campaign_id']}, which is not in the allowlist.")
    if ag["status"] == "REMOVED":
        raise Refused(f"Ad group {ad_group_id} is removed.")
    return ag


def _norm_kw(text) -> str:
    if not isinstance(text, str):
        raise Refused(f"Keyword {text!r} must be text.")
    t = " ".join(text.strip().lower().split())
    if not t:
        raise Refused("Empty keyword.")
    if len(t) > KEYWORD_MAX_CHARS or len(t.split()) > KEYWORD_MAX_WORDS:
        raise Refused(f"Keyword {t!r} is too long (max {KEYWORD_MAX_CHARS} characters, {KEYWORD_MAX_WORDS} words).")
    return t


def _tokens(text: str) -> list[str]:
    return re.sub(r'[\[\]"+]', " ", text.lower()).split()


def negative_blocks(neg_text: str, neg_match: str, kw_text: str) -> bool:
    """True if a negative keyword would block an ad group keyword.

    Phrase negatives block when their words appear, in order and adjacent, in
    the keyword. Exact negatives block when the words are identical.
    """
    n, k = _tokens(neg_text), _tokens(kw_text)
    if neg_match == "EXACT":
        return n == k
    return any(k[i : i + len(n)] == n for i in range(len(k) - len(n) + 1))


def _id_from_rn(resource_name: str) -> str:
    return resource_name.rsplit("~", 1)[-1] if "~" in resource_name else resource_name.rsplit("/", 1)[-1]


def _status_rows(item: str, before: str, after: str) -> list[dict]:
    return [{"item": item, "before": before, "after": after}]


# -- negative keywords -----------------------------------------------------------


def _negative_items(ctx: Ctx, req: dict) -> list[dict]:
    if "items" in req:  # internal form, used by undo
        raw = [(i["text"], i["match_type"]) for i in req["items"]]
    else:
        kws = req.get("keywords")
        if not isinstance(kws, list) or not kws:
            raise Refused("keywords must be a non-empty list.")
        raw = [(k, req.get("match_type")) for k in kws]
    if len(raw) > ctx.g.max_negatives_per_proposal:
        raise Refused(f"{len(raw)} negatives given; the limit is {ctx.g.max_negatives_per_proposal} per proposal.")
    seen, items = set(), []
    for text, mt in raw:
        item = {"text": _norm_kw(text), "match_type": ctx.g.check_match_type(mt)}
        key = (item["text"], item["match_type"])
        if key not in seen:
            seen.add(key)
            items.append(item)
    return items


def plan_add_campaign_negatives(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    items = _negative_items(ctx, req)
    all_negatives = ctx.gw.campaign_negatives(camp["id"])
    existing = {(n["text"].lower(), n["match_type"]): n for n in all_negatives}
    # Every existing negative with a requested text, in any match type, goes into
    # the "before" snapshot, so adding one by hand in between counts as drift.
    requested = {it["text"] for it in items}
    same_text = sorted(
        ({"text": n["text"].lower(), "match_type": n["match_type"]} for n in all_negatives if n["text"].lower() in requested),
        key=lambda d: (d["text"], d["match_type"]),
    )
    keywords = ctx.gw.campaign_keywords(camp["id"])
    active = [k for k in keywords if k["status"] == "ENABLED" and k["ad_group_status"] == "ENABLED"]
    dormant = [k for k in keywords if k["status"] == "ENABLED" and k["ad_group_status"] != "ENABLED"]

    to_add, warnings, skipped = [], [], []
    for it in items:
        label = f'"{it["text"]}" ({it["match_type"]})'
        if (it["text"], it["match_type"]) in existing:
            skipped.append(f"{label} already exists")
            continue
        blocked = [k for k in active if negative_blocks(it["text"], it["match_type"], k["text"])]
        if blocked:
            names = ", ".join(f'"{k["text"]}" in {k["ad_group_name"]}' for k in blocked)
            warnings.append(f"Rejected {label}: it would block active keyword(s) {names}.")
            continue
        others = [n["match_type"] for n in same_text if n["text"] == it["text"] and n["match_type"] != it["match_type"]]
        if others:
            warnings.append(f'{label}: the same text already exists as a {"/".join(others)} negative.')
        sleeping = [k for k in dormant if negative_blocks(it["text"], it["match_type"], k["text"])]
        if sleeping:
            names = ", ".join(f'"{k["text"]}" in {k["ad_group_name"]} (paused ad group)' for k in sleeping)
            warnings.append(f"{label} would also block {names} if that ad group is re-enabled.")
        to_add.append(it)

    if not to_add:
        raise Refused("Nothing to add. " + " ".join(warnings + skipped))
    rn = ctx.rn
    operations = [
        ops.create(
            "campaign_criterion",
            {"campaign": rn.campaign(camp["id"]), "negative": True, "keyword": it},
        )
        for it in to_add
    ]
    rows = [{"item": f'Negative "{it["text"]}" ({it["match_type"]})', "before": "not present", "after": "added"} for it in to_add]
    return Plan(
        summary=f"Add {len(to_add)} negative keyword(s) to campaign {camp['name']}",
        before={"campaign_id": camp["id"], "existing_same_text": same_text, "to_add": to_add},
        operations=operations,
        rows=rows,
        warnings=warnings,
        skipped=skipped,
    )


def after_add_campaign_negatives(ctx, req, result):
    camp_id = ctx.g.check_campaign(_id(req["campaign_id"], "campaign_id"))
    created = {_id_from_rn(r) for r in result["resource_names"] if r}
    live = [n for n in ctx.gw.campaign_negatives(camp_id) if n["criterion_id"] in created]
    return {"added": live, "resource_names": result["resource_names"]}


def inverse_add_campaign_negatives(doc):
    ids = [n["criterion_id"] for n in doc["after"]["added"]]
    if not ids:
        raise Refused("The change log has no record of which negatives were created.")
    return "remove_campaign_negatives", {"campaign_id": doc["request"]["campaign_id"], "criterion_ids": ids}


def plan_remove_campaign_negatives(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    ids = req.get("criterion_ids")
    if not isinstance(ids, list) or not ids:
        raise Refused("criterion_ids must be a non-empty list.")
    if len(ids) > ctx.g.max_negatives_per_proposal:
        raise Refused(f"At most {ctx.g.max_negatives_per_proposal} negatives per proposal.")
    ids = sorted({_id(i, "criterion_id") for i in ids})
    existing = {n["criterion_id"]: n for n in ctx.gw.campaign_negatives(camp["id"])}
    missing = [i for i in ids if i not in existing]
    if missing:
        raise Refused(f"Not negative keywords of campaign {camp['id']}: {', '.join(missing)}.")
    rn = ctx.rn
    targets = [existing[i] for i in ids]
    return Plan(
        summary=f"Remove {len(targets)} negative keyword(s) from campaign {camp['name']}",
        before={"campaign_id": camp["id"], "negatives": targets},
        operations=[ops.remove("campaign_criterion", rn.campaign_criterion(camp["id"], t["criterion_id"])) for t in targets],
        rows=[{"item": f'Negative "{t["text"]}" ({t["match_type"]}) #{t["criterion_id"]}', "before": "present", "after": "removed"} for t in targets],
    )


def after_remove_campaign_negatives(ctx, req, result):
    camp_id = ctx.g.check_campaign(_id(req["campaign_id"], "campaign_id"))
    ids = {str(i) for i in req["criterion_ids"]}
    return {"still_present": [n for n in ctx.gw.campaign_negatives(camp_id) if n["criterion_id"] in ids]}


def inverse_remove_campaign_negatives(doc):
    items = [{"text": n["text"], "match_type": n["match_type"]} for n in doc["before"]["negatives"]]
    return "add_campaign_negatives", {"campaign_id": doc["request"]["campaign_id"], "items": items}


# -- keywords --------------------------------------------------------------------


def plan_add_keywords(ctx: Ctx, req: dict) -> Plan:
    ag = _ad_group(ctx, req.get("ad_group_id"))
    raw = req.get("keywords")
    if not isinstance(raw, list) or not raw:
        raise Refused("keywords must be a non-empty list of {text, match_type}.")
    if len(raw) > ctx.g.max_keywords_per_proposal:
        raise Refused(f"{len(raw)} keywords given; the limit is {ctx.g.max_keywords_per_proposal} per proposal.")
    items, seen = [], set()
    for k in raw:
        if not isinstance(k, dict):
            raise Refused("Each keyword must be {text, match_type}.")
        mt = str(k.get("match_type", "")).strip().upper()
        if mt == "BROAD":
            raise Refused(f'Broad match is never allowed (keyword "{k.get("text")}").')
        item = {"text": _norm_kw(k.get("text")), "match_type": ctx.g.check_match_type(mt)}
        if (item["text"], item["match_type"]) not in seen:
            seen.add((item["text"], item["match_type"]))
            items.append(item)

    in_group = {
        (k["text"].lower(), k["match_type"])
        for k in ctx.gw.campaign_keywords(ag["campaign_id"])
        if k["ad_group_id"] == ag["id"]
    }
    negatives = ctx.gw.campaign_negatives(ag["campaign_id"])
    to_add, present, warnings, skipped = [], [], [], []
    for it in items:
        label = f'"{it["text"]}" ({it["match_type"]})'
        if (it["text"], it["match_type"]) in in_group:
            present.append(it)
            skipped.append(f"{label} already exists in {ag['name']}")
            continue
        blockers = [n for n in negatives if negative_blocks(n["text"], n["match_type"], it["text"])]
        if blockers:
            warnings.append(
                f"{label} is blocked by campaign negative(s) "
                + ", ".join(f'"{n["text"]}" ({n["match_type"]})' for n in blockers)
            )
        to_add.append(it)
    if not to_add:
        raise Refused("Nothing to add. " + " ".join(skipped))
    rn = ctx.rn
    return Plan(
        summary=f"Add {len(to_add)} keyword(s) to ad group {ag['name']}",
        before={"ad_group_id": ag["id"], "already_present": present, "to_add": to_add},
        operations=[
            ops.create("ad_group_criterion", {"ad_group": rn.ad_group(ag["id"]), "status": "ENABLED", "keyword": it})
            for it in to_add
        ],
        rows=[{"item": f'Keyword "{it["text"]}" ({it["match_type"]}) in {ag["name"]}', "before": "not present", "after": "added, enabled"} for it in to_add],
        warnings=warnings,
        skipped=skipped,
    )


def _keywords_in_group(ctx, ad_group_id):
    ag = _ad_group(ctx, ad_group_id)
    return ag, {k["criterion_id"]: k for k in ctx.gw.campaign_keywords(ag["campaign_id"]) if k["ad_group_id"] == ag["id"]}


def after_add_keywords(ctx, req, result):
    _, kws = _keywords_in_group(ctx, req["ad_group_id"])
    created = [_id_from_rn(r) for r in result["resource_names"] if r]
    return {"added": [kws[c] for c in created if c in kws], "resource_names": result["resource_names"]}


def inverse_add_keywords(doc):
    ids = [k["criterion_id"] for k in doc["after"]["added"]]
    if not ids:
        raise Refused("The change log has no record of which keywords were created.")
    return "remove_keywords", {"ad_group_id": doc["request"]["ad_group_id"], "criterion_ids": ids}


def plan_remove_keywords(ctx: Ctx, req: dict) -> Plan:
    """Only reachable through propose_undo of add_keywords."""
    ag, kws = _keywords_in_group(ctx, req.get("ad_group_id"))
    ids = sorted({_id(i, "criterion_id") for i in req.get("criterion_ids") or []})
    if not ids:
        raise Refused("criterion_ids must be a non-empty list.")
    missing = [i for i in ids if i not in kws]
    if missing:
        raise Refused(f"Not keywords of ad group {ag['id']}: {', '.join(missing)}.")
    rn = ctx.rn
    return Plan(
        summary=f"Remove {len(ids)} keyword(s) from ad group {ag['name']}",
        before={"ad_group_id": ag["id"], "keywords": [kws[i] for i in ids]},
        operations=[ops.remove("ad_group_criterion", rn.ad_group_criterion(ag["id"], i)) for i in ids],
        rows=[{"item": f'Keyword "{kws[i]["text"]}" ({kws[i]["match_type"]}) #{i}', "before": kws[i]["status"].lower(), "after": "removed"} for i in ids],
    )


def after_remove_keywords(ctx, req, result):
    _, kws = _keywords_in_group(ctx, req["ad_group_id"])
    return {"still_present": [kws[str(i)] for i in req["criterion_ids"] if str(i) in kws]}


def inverse_remove_keywords(doc):
    kws = [{"text": k["text"], "match_type": k["match_type"]} for k in doc["before"]["keywords"]]
    return "add_keywords", {"ad_group_id": doc["request"]["ad_group_id"], "keywords": kws}


def plan_set_keyword_status(ctx: Ctx, req: dict) -> Plan:
    ag, kws = _keywords_in_group(ctx, req.get("ad_group_id"))
    crit = _id(req.get("criterion_id"), "criterion_id")
    status = _status(req.get("status"))
    kw = kws.get(crit)
    if not kw:
        raise Refused(f"Keyword {crit} is not in ad group {ag['id']}.")
    if kw["status"] == status:
        raise Refused(f'Keyword "{kw["text"]}" is already {status}.')
    return Plan(
        summary=f'Set keyword "{kw["text"]}" to {status}',
        before={"status": kw["status"]},
        operations=[ops.update("ad_group_criterion", {"resource_name": ctx.rn.ad_group_criterion(ag["id"], crit), "status": status}, ["status"])],
        rows=_status_rows(f'Keyword "{kw["text"]}" ({kw["match_type"]}) in {ag["name"]}', kw["status"], status),
    )


def after_set_keyword_status(ctx, req, result):
    _, kws = _keywords_in_group(ctx, req["ad_group_id"])
    return {"status": kws.get(str(req["criterion_id"]), {}).get("status")}


def inverse_set_keyword_status(doc):
    return "set_keyword_status", {**doc["request"], "status": doc["before"]["status"]}


# -- ad group and campaign status ---------------------------------------------------


def plan_set_ad_group_status(ctx: Ctx, req: dict) -> Plan:
    ag = _ad_group(ctx, req.get("ad_group_id"))
    status = _status(req.get("status"))
    if ag["status"] == status:
        raise Refused(f"Ad group {ag['name']} is already {status}.")
    return Plan(
        summary=f"Set ad group {ag['name']} to {status}",
        before={"status": ag["status"]},
        operations=[ops.update("ad_group", {"resource_name": ctx.rn.ad_group(ag["id"]), "status": status}, ["status"])],
        rows=_status_rows(f"Ad group {ag['name']} ({ag['id']})", ag["status"], status),
    )


def after_set_ad_group_status(ctx, req, result):
    return {"status": ctx.gw.ad_group(str(req["ad_group_id"]))["status"]}


def inverse_set_ad_group_status(doc):
    return "set_ad_group_status", {**doc["request"], "status": doc["before"]["status"]}


def plan_set_campaign_status(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    status = _status(req.get("status"))
    if camp["status"] == status:
        raise Refused(f"Campaign {camp['name']} is already {status}.")
    return Plan(
        summary=f"Set campaign {camp['name']} to {status}",
        before={"status": camp["status"]},
        operations=[ops.update("campaign", {"resource_name": ctx.rn.campaign(camp["id"]), "status": status}, ["status"])],
        rows=_status_rows(f"Campaign {camp['name']} ({camp['id']})", camp["status"], status),
    )


def after_set_campaign_status(ctx, req, result):
    return {"status": ctx.gw.campaign(str(req["campaign_id"]))["status"]}


def inverse_set_campaign_status(doc):
    return "set_campaign_status", {"campaign_id": doc["request"]["campaign_id"], "status": doc["before"]["status"]}


# -- budget ---------------------------------------------------------------------


def plan_set_daily_budget(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    amount = _int(req.get("amount_idr"), "amount_idr")
    ctx.g.budget.check("Daily budget", amount)
    if camp["budget_explicitly_shared"] or camp["budget_reference_count"] > 1:
        raise Refused("This campaign's budget is shared with another campaign; changing it is not allowed.")
    current = camp["budget_amount_micros"] // MICROS
    if amount == current:
        raise Refused(f"The daily budget is already Rp{current:,}.")
    pct = abs(amount - current) / current * 100 if current else float("inf")
    if pct > ctx.g.budget_max_change_pct and ctx.undo_of is None:  # an undo restores a value that was live before
        raise Refused(
            f"Rp{current:,} to Rp{amount:,} is a {pct:.0f}% change; the limit is "
            f"{ctx.g.budget_max_change_pct:g}% per proposal."
        )
    return Plan(
        summary=f"Set daily budget of {camp['name']} to Rp{amount:,}",
        before={"budget_resource_name": camp["budget_resource_name"], "amount_micros": camp["budget_amount_micros"]},
        operations=[ops.update("campaign_budget", {"resource_name": camp["budget_resource_name"], "amount_micros": str(amount * MICROS)}, ["amount_micros"])],
        rows=_status_rows(f"Daily budget, {camp['name']}", f"Rp{current:,}", f"Rp{amount:,} ({(amount - current) / current * 100:+.0f}%)"),
    )


def after_set_daily_budget(ctx, req, result):
    return {"amount_micros": ctx.gw.campaign(str(req["campaign_id"]))["budget_amount_micros"]}


def inverse_set_daily_budget(doc):
    return "set_daily_budget", {"campaign_id": doc["request"]["campaign_id"], "amount_idr": doc["before"]["amount_micros"] // MICROS}


# -- bidding ----------------------------------------------------------------------

STRATEGIES = ("MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS", "MANUAL_CPC")
DEVICE_CRITERION_IDS = {"DESKTOP": "30000", "MOBILE": "30001", "TABLET": "30002"}


def _rp(micros) -> str:
    return f"Rp{micros // MICROS:,}" if micros else "not set"


def _bidding_label(strategy, ceiling_micros, cpa_micros) -> str:
    if strategy == "MAXIMIZE_CLICKS":
        return "Maximize clicks" + (f", CPC ceiling Rp{ceiling_micros // MICROS:,}" if ceiling_micros else ", no CPC ceiling")
    if strategy == "MAXIMIZE_CONVERSIONS":
        return "Maximize conversions" + (f", target CPA Rp{cpa_micros // MICROS:,}" if cpa_micros else ", no target CPA")
    if strategy == "MANUAL_CPC":
        return "Manual CPC (Enhanced CPC off)"
    return strategy


def _check_bid(ctx: Ctx, label: str, new_idr: int, current_micros: int | None, restore: bool = False) -> str:
    """Range and per-proposal change limit for one bid. Returns the change as text."""
    ctx.g.bid.check(f"{label}: bid", new_idr)
    return _check_bid_change(ctx, label, new_idr * MICROS, current_micros, restore)


def _check_bid_change(ctx: Ctx, label: str, new_micros: int, current_micros: int | None, restore: bool = False) -> str:
    """``restore`` (an undo, see Ctx.undo_of) skips the ±% limit, never the range:
    undoing a -50% change needs +100%, and the old value was live before."""
    if not current_micros:
        return "new"
    pct = (new_micros - current_micros) / current_micros * 100
    if abs(pct) > ctx.g.bid_max_change_pct and not restore:
        raise Refused(
            f"{label}: {_rp(current_micros)} to {_rp(new_micros)} is a {pct:+.0f}% change; "
            f"the limit is ±{ctx.g.bid_max_change_pct:g}% per proposal."
        )
    return f"{pct:+.0f}%"


def _require_manual_cpc(camp: dict, what: str) -> None:
    if camp["bidding_strategy_type"] != "MANUAL_CPC":
        current = _bidding_label(STRATEGY_FROM_API.get(camp["bidding_strategy_type"], camp["bidding_strategy_type"]), camp["cpc_bid_ceiling_micros"], camp["target_cpa_micros"])
        raise Refused(
            f"{what} is only allowed while the campaign uses Manual CPC (it uses {current}). "
            "Under automated bidding the bid has no effect."
        )


def plan_set_bidding(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    strategy = str(req.get("strategy", "")).strip().upper()
    ceiling, cpa = req.get("cpc_ceiling_idr"), req.get("target_cpa_idr")
    default_cpc = req.get("ad_group_default_cpc_idr")
    raw_bids = req.get("ad_group_bids") or {}
    # An undo touches only the ad groups it lists, and may skip the ±% limit.
    only_listed = restore = ctx.undo_of is not None
    if strategy not in STRATEGIES:
        raise Refused("strategy must be MAXIMIZE_CLICKS, MAXIMIZE_CONVERSIONS or MANUAL_CPC.")
    if camp["portfolio_bidding_strategy"]:
        raise Refused("The campaign uses a portfolio bid strategy; changing it is not allowed here.")
    if strategy != "MAXIMIZE_CLICKS" and ceiling is not None:
        raise Refused("cpc_ceiling_idr only applies to MAXIMIZE_CLICKS.")
    if strategy != "MAXIMIZE_CONVERSIONS" and cpa is not None:
        raise Refused("target_cpa_idr only applies to MAXIMIZE_CONVERSIONS.")
    if ceiling is not None:
        ctx.g.cpc_ceiling.check("CPC ceiling", _int(ceiling, "cpc_ceiling_idr"))
    if cpa is not None:
        ctx.g.target_cpa.check("Target CPA", _int(cpa, "target_cpa_idr"))
    if strategy == "MANUAL_CPC":
        if default_cpc is None and not only_listed:
            raise Refused(
                "ad_group_default_cpc_idr is required for MANUAL_CPC: it replaces the current ad group bids, "
                "so Manual CPC never runs on the old ones."
            )
    else:
        if default_cpc is not None:
            raise Refused("ad_group_default_cpc_idr only applies to MANUAL_CPC.")
        if raw_bids and not only_listed:
            raise Refused("ad_group_bids only applies to MANUAL_CPC.")
    if not isinstance(raw_bids, dict):
        raise Refused("ad_group_bids must be an object of {ad_group_id: cpc_idr}.")
    if default_cpc is not None:
        default_cpc = _int(default_cpc, "ad_group_default_cpc_idr")

    groups = {g["id"]: g for g in ctx.gw.campaign_ad_groups(camp["id"])}
    bids = {}
    for k, v in raw_bids.items():
        gid = _id(k, "ad_group_bids key")
        if gid not in groups:
            raise Refused(f"Ad group {gid} is not in campaign {camp['id']}.")
        bids[gid] = _int(v, f"bid for ad group {gid}")
    enabled = sorted(g for g, info in groups.items() if info["status"] == "ENABLED")

    # Enabled ad groups get the default; ad_group_bids override it, and are the
    # only way to change a paused ad group.
    targets = {} if (only_listed or default_cpc is None) else {g: default_cpc for g in enabled}
    targets.update(bids)
    changes = {}
    for gid in sorted(targets):
        g = groups[gid]
        new = targets[gid]
        if g["cpc_bid_micros"] != new * MICROS:
            changes[gid] = (new, _check_bid(ctx, f"Ad group {g['name']}", new, g["cpc_bid_micros"], restore))

    cur_strategy = STRATEGY_FROM_API.get(camp["bidding_strategy_type"], camp["bidding_strategy_type"])
    cur_ceiling, cur_cpa = camp["cpc_bid_ceiling_micros"], camp["target_cpa_micros"]
    new_ceiling = ceiling * MICROS if ceiling is not None else None
    new_cpa = cpa * MICROS if cpa is not None else None
    same_strategy = cur_strategy == strategy
    campaign_unchanged = same_strategy and (
        (strategy == "MAXIMIZE_CLICKS" and cur_ceiling == new_ceiling)
        or (strategy == "MAXIMIZE_CONVERSIONS" and cur_cpa == new_cpa)
        or (strategy == "MANUAL_CPC" and not camp.get("enhanced_cpc_enabled"))
    )
    if campaign_unchanged and not changes:
        raise Refused("Bidding is already set that way.")

    operations, rows, warnings, skipped = [], [], [], []
    rn = ctx.rn
    for gid, (new, change) in changes.items():
        g = groups[gid]
        operations.append(ops.update("ad_group", {"resource_name": rn.ad_group(gid), "cpc_bid_micros": str(new * MICROS)}, ["cpc_bid_micros"]))
        rows.append({"item": f"Ad group {g['name']} ({gid}), default max CPC", "before": _rp(g["cpc_bid_micros"]), "after": f"Rp{new:,} ({change})"})
    if not campaign_unchanged:
        resource = {"resource_name": rn.campaign(camp["id"])}
        if strategy == "MAXIMIZE_CLICKS":
            resource["target_spend"] = {"cpc_bid_ceiling_micros": str(new_ceiling)} if new_ceiling else {}
            mask = ["target_spend.cpc_bid_ceiling_micros"] if new_ceiling or same_strategy else ["target_spend"]
        elif strategy == "MAXIMIZE_CONVERSIONS":
            resource["maximize_conversions"] = {"target_cpa_micros": str(new_cpa)} if new_cpa else {}
            mask = ["maximize_conversions.target_cpa_micros"] if new_cpa or same_strategy else ["maximize_conversions"]
        else:
            resource["manual_cpc"] = {"enhanced_cpc_enabled": False}
            mask = ["manual_cpc.enhanced_cpc_enabled"]
        operations.append(ops.update("campaign", resource, mask))
        rows.insert(0, {"item": f"Bidding, {camp['name']}", "before": _bidding_label(cur_strategy, cur_ceiling, cur_cpa), "after": _bidding_label(strategy, new_ceiling, new_cpa)})
        if strategy != "MANUAL_CPC" and not same_strategy:
            warnings.append("Switching bid strategy restarts Smart Bidding learning.")

    if strategy == "MANUAL_CPC" and not only_listed:
        for gid, g in sorted(groups.items()):
            if gid not in targets:
                skipped.append(f"Ad group {g['name']} ({g['status'].lower()}) not changed: stays at {_rp(g['cpc_bid_micros'])}")
        own = [k for k in ctx.gw.campaign_keywords(camp["id"]) if k["cpc_bid_micros"] and k["ad_group_id"] in targets]
        if own:
            names = ", ".join(f'"{k["text"]}" {_rp(k["cpc_bid_micros"])}' for k in own[:5])
            warnings.append(f"{len(own)} keyword(s) have their own bid and keep it ({names}). Change them with propose_set_keyword_cpc.")

    snapshot = sorted(set(enabled) | set(targets))
    return Plan(
        summary=f"Set bidding of {camp['name']} to {_bidding_label(strategy, new_ceiling, new_cpa)}"
        + (f", {len(changes)} ad group bid(s) changed" if changes else "")
        + (" (one atomic change)" if changes and not campaign_unchanged else ""),
        before={
            "bidding_strategy_type": camp["bidding_strategy_type"],
            "cpc_bid_ceiling_micros": cur_ceiling,
            "target_cpa_micros": cur_cpa,
            "enhanced_cpc_enabled": bool(camp.get("enhanced_cpc_enabled")),
            "ad_groups": [{"ad_group_id": g, "status": groups[g]["status"], "cpc_bid_micros": groups[g]["cpc_bid_micros"]} for g in snapshot],
        },
        # Ad group bids first, then the strategy switch; all in one atomic mutate.
        operations=operations,
        rows=rows,
        warnings=warnings,
        skipped=skipped,
    )


def after_set_bidding(ctx, req, result):
    c = ctx.gw.campaign(str(req["campaign_id"]))
    groups = ctx.gw.campaign_ad_groups(c["id"])
    out = {k: c[k] for k in ("bidding_strategy_type", "cpc_bid_ceiling_micros", "target_cpa_micros", "enhanced_cpc_enabled")}
    out["ad_groups"] = [{"ad_group_id": g["id"], "status": g["status"], "cpc_bid_micros": g["cpc_bid_micros"]} for g in sorted(groups, key=lambda g: g["id"])]
    return out


def inverse_set_bidding(doc):
    """Previous strategy and previous ad group bids, as one atomic proposal."""
    b = doc["before"]
    strategy = STRATEGY_FROM_API.get(b["bidding_strategy_type"])
    if not strategy:
        raise Refused(f"The previous strategy {b['bidding_strategy_type']} can't be restored by this server.")
    req = {"campaign_id": doc["request"]["campaign_id"], "strategy": strategy, "cpc_ceiling_idr": None, "target_cpa_idr": None}
    if strategy == "MAXIMIZE_CLICKS" and b["cpc_bid_ceiling_micros"]:
        req["cpc_ceiling_idr"] = b["cpc_bid_ceiling_micros"] // MICROS
    if strategy == "MAXIMIZE_CONVERSIONS" and b["target_cpa_micros"]:
        req["target_cpa_idr"] = b["target_cpa_micros"] // MICROS
    after = {g["ad_group_id"]: g["cpc_bid_micros"] for g in (doc.get("after") or {}).get("ad_groups", [])}
    changed = {
        g["ad_group_id"]: g["cpc_bid_micros"] // MICROS
        for g in b.get("ad_groups", [])
        if g["cpc_bid_micros"] and after.get(g["ad_group_id"]) != g["cpc_bid_micros"]
    }
    req["ad_group_bids"] = changed
    return "set_bidding", req


def plan_set_ad_group_cpc(ctx: Ctx, req: dict) -> Plan:
    ag = _ad_group(ctx, req.get("ad_group_id"))
    camp = _campaign(ctx, ag["campaign_id"])
    _require_manual_cpc(camp, "Changing an ad group's max CPC")
    new = _int(req.get("cpc_idr"), "cpc_idr")
    if ag["cpc_bid_micros"] == new * MICROS:
        raise Refused(f"Ad group {ag['name']} is already at Rp{new:,}.")
    change = _check_bid(ctx, f"Ad group {ag['name']}", new, ag["cpc_bid_micros"], ctx.undo_of is not None)
    return Plan(
        summary=f"Set default max CPC of ad group {ag['name']} to Rp{new:,}",
        before={"bidding_strategy_type": camp["bidding_strategy_type"], "cpc_bid_micros": ag["cpc_bid_micros"]},
        operations=[ops.update("ad_group", {"resource_name": ctx.rn.ad_group(ag["id"]), "cpc_bid_micros": str(new * MICROS)}, ["cpc_bid_micros"])],
        rows=_status_rows(f"Ad group {ag['name']} ({ag['id']}), default max CPC", _rp(ag["cpc_bid_micros"]), f"Rp{new:,} ({change})"),
    )


def after_set_ad_group_cpc(ctx, req, result):
    return {"cpc_bid_micros": ctx.gw.ad_group(str(req["ad_group_id"]))["cpc_bid_micros"]}


def inverse_set_ad_group_cpc(doc):
    if not doc["before"]["cpc_bid_micros"]:
        raise Refused("The ad group had no default bid before; set one with propose_set_ad_group_cpc.")
    return "set_ad_group_cpc", {"ad_group_id": doc["request"]["ad_group_id"], "cpc_idr": doc["before"]["cpc_bid_micros"] // MICROS}


def plan_set_keyword_cpc(ctx: Ctx, req: dict) -> Plan:
    items = req.get("items")
    if not isinstance(items, list) or not items:
        raise Refused("items must be a non-empty list of {ad_group_id, criterion_id, cpc_idr}.")
    if len(items) > ctx.g.max_keyword_bids_per_proposal:
        raise Refused(f"{len(items)} keywords given; the limit is {ctx.g.max_keyword_bids_per_proposal} per proposal.")
    cache, parsed, seen = {}, [], set()
    for it in items:
        if not isinstance(it, dict):
            raise Refused("Each item must be {ad_group_id, criterion_id, cpc_idr}.")
        ag_id = _id(it.get("ad_group_id"), "ad_group_id")
        crit = _id(it.get("criterion_id"), "criterion_id")
        if (ag_id, crit) in seen:
            raise Refused(f"Keyword {crit} in ad group {ag_id} is listed twice.")
        seen.add((ag_id, crit))
        if ag_id not in cache:
            cache[ag_id] = _keywords_in_group(ctx, ag_id)
        ag, kws = cache[ag_id]
        kw = kws.get(crit)
        if not kw:
            raise Refused(f"Keyword {crit} is not in ad group {ag_id}.")
        cpc = it.get("cpc_idr")
        cpc = None if cpc in (None, 0) else _int(cpc, "cpc_idr")
        parsed.append((ag, kw, cpc))

    camp = _campaign(ctx, parsed[0][0]["campaign_id"])
    _require_manual_cpc(camp, "Changing keyword bids")

    operations, rows, before, skipped = [], [], [], []
    rn = ctx.rn
    restore = ctx.undo_of is not None
    for ag, kw, cpc in parsed:
        own, default = kw["cpc_bid_micros"], kw["ad_group_cpc_bid_micros"]
        label = f'Keyword "{kw["text"]}" ({kw["match_type"]}) in {ag["name"]}'
        current = f"{_rp(own)} (own bid)" if own else f"ad group default {_rp(default)}"
        if cpc is None:
            if not own:
                skipped.append(f"{label} has no own bid; nothing to remove")
                continue
            change = _check_bid_change(ctx, label, default, own, restore) if default else "n/a"
            after_label = f"ad group default {_rp(default)} ({change})"
            resource = {"resource_name": rn.ad_group_criterion(ag["id"], kw["criterion_id"])}  # field left out = cleared
        else:
            if own == cpc * MICROS:
                skipped.append(f"{label} is already at Rp{cpc:,}")
                continue
            change = _check_bid(ctx, label, cpc, own or default, restore)
            after_label = f"Rp{cpc:,} (own bid, {change})"
            resource = {"resource_name": rn.ad_group_criterion(ag["id"], kw["criterion_id"]), "cpc_bid_micros": str(cpc * MICROS)}
        operations.append(ops.update("ad_group_criterion", resource, ["cpc_bid_micros"]))
        rows.append({"item": label, "before": current, "after": after_label})
        before.append({"ad_group_id": ag["id"], "criterion_id": kw["criterion_id"], "cpc_bid_micros": own, "ad_group_cpc_bid_micros": default})
    if not operations:
        raise Refused("Nothing to change. " + " ".join(skipped))
    return Plan(
        summary=f"Set max CPC of {len(operations)} keyword(s)",
        before={"bidding_strategy_type": camp["bidding_strategy_type"], "keywords": before},
        operations=operations,
        rows=rows,
        skipped=skipped,
    )


def after_set_keyword_cpc(ctx, req, result):
    out = []
    for it in req["items"]:
        _, kws = _keywords_in_group(ctx, it["ad_group_id"])
        kw = kws.get(str(it["criterion_id"]), {})
        out.append({"ad_group_id": str(it["ad_group_id"]), "criterion_id": str(it["criterion_id"]), "cpc_bid_micros": kw.get("cpc_bid_micros")})
    return {"keywords": out}


def inverse_set_keyword_cpc(doc):
    items = [
        {"ad_group_id": k["ad_group_id"], "criterion_id": k["criterion_id"], "cpc_idr": (k["cpc_bid_micros"] // MICROS) if k["cpc_bid_micros"] else 0}
        for k in doc["before"]["keywords"]
    ]
    return "set_keyword_cpc", {"items": items}


def _device_pct(modifier) -> int:
    return round((modifier - 1) * 100) if modifier else 0


def plan_set_device_bid_adjustment(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    device = str(req.get("device", "")).strip().upper()
    if device not in DEVICE_CRITERION_IDS:
        raise Refused("device must be DESKTOP, MOBILE or TABLET.")
    pct = _int(req.get("percent"), "percent")
    lo, hi = ctx.g.device_adjustment_pct.min, ctx.g.device_adjustment_pct.max
    if not lo <= pct <= hi:
        raise Refused(f"Device bid adjustment {pct:+d}% is outside the allowed range {lo:+d}% to {hi:+d}%.")
    _require_manual_cpc(camp, "A device bid adjustment")
    live = {d["device"]: d for d in ctx.gw.device_criteria(camp["id"])}
    cur = live.get(device)
    cur_mod = cur["bid_modifier"] if cur else None
    if _device_pct(cur_mod) == pct:
        raise Refused(f"{device.title()} is already at {pct:+d}%.")
    crit = cur["criterion_id"] if cur else DEVICE_CRITERION_IDS[device]
    new_mod = round(1 + pct / 100, 2)
    return Plan(
        summary=f"Set {device.lower()} bid adjustment of {camp['name']} to {pct:+d}%",
        before={"bidding_strategy_type": camp["bidding_strategy_type"], "device": device, "bid_modifier": cur_mod},
        operations=[ops.update("campaign_criterion", {"resource_name": ctx.rn.campaign_criterion(camp["id"], crit), "bid_modifier": new_mod}, ["bid_modifier"])],
        rows=_status_rows(f"{device.title()} bid adjustment, {camp['name']}", f"{_device_pct(cur_mod):+d}%", f"{pct:+d}% (bid x{new_mod:g})"),
    )


def after_set_device_bid_adjustment(ctx, req, result):
    live = {d["device"]: d for d in ctx.gw.device_criteria(str(req["campaign_id"]))}
    return {"bid_modifier": (live.get(str(req["device"]).upper()) or {}).get("bid_modifier")}


def inverse_set_device_bid_adjustment(doc):
    return "set_device_bid_adjustment", {"campaign_id": doc["request"]["campaign_id"], "device": doc["before"]["device"], "percent": _device_pct(doc["before"]["bid_modifier"])}


# -- ad schedule ------------------------------------------------------------------


def _live_slots(ctx, camp_id) -> list[dict]:
    return sorted(ctx.gw.ad_schedule(camp_id), key=lambda s: (schedule.DAYS.index(s["day"]), s["start_hour"]))


def _slot_label(s: dict) -> str:
    sm = {"ZERO": "00", "FIFTEEN": "15", "THIRTY": "30", "FORTY_FIVE": "45"}
    mod = f" (bid x{s['bid_modifier']:g})" if s.get("bid_modifier") is not None else ""
    return f"{s['start_hour']:02d}:{sm.get(s.get('start_minute', 'ZERO'), '00')}-{s['end_hour']:02d}:{sm.get(s.get('end_minute', 'ZERO'), '00')}{mod}"


def _schedule_plan(ctx: Ctx, camp: dict, new: list[schedule.Slot], summary: str) -> Plan:
    current = _live_slots(ctx, camp["id"])
    cur_cmp = [(s["day"], s["start_hour"], s["start_minute"], s["end_hour"], s["end_minute"], s["bid_modifier"]) for s in current]
    new_cmp = [(s.day, s.start_hour, "ZERO", s.end_hour, "ZERO", s.bid_modifier) for s in new]
    if sorted(cur_cmp, key=str) == sorted(new_cmp, key=str):
        raise Refused("The ad schedule already matches.")
    rn = ctx.rn
    operations = [ops.remove("campaign_criterion", rn.campaign_criterion(camp["id"], s["criterion_id"])) for s in current]
    for s in new:
        crit = {
            "campaign": rn.campaign(camp["id"]),
            "ad_schedule": {"day_of_week": s.day, "start_hour": s.start_hour, "start_minute": "ZERO", "end_hour": s.end_hour, "end_minute": "ZERO"},
        }
        if s.bid_modifier is not None:
            crit["bid_modifier"] = s.bid_modifier
        operations.append(ops.create("campaign_criterion", crit))

    rows = []
    for day in schedule.DAYS:
        b = [_slot_label(s) for s in current if s["day"] == day]
        a = [_slot_label(s.to_dict()) for s in new if s.day == day]
        if b or a:
            rows.append({"item": day.title(), "before": ", ".join(b) or "(not running)", "after": ", ".join(a) or "(not running)"})
    warnings = []
    if not new:
        warnings.append("With no schedule slots the campaign runs at all hours, every day.")
    tz_note = "Hours are in the account time zone."
    return Plan(
        summary=f"{summary} ({len(current)} slot(s) to {len(new)} slot(s)). {tz_note}",
        before={"slots": current},
        operations=operations,
        rows=rows,
        warnings=warnings,
    )


def plan_set_ad_schedule(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    raw = req.get("slots")
    if not isinstance(raw, list):
        raise Refused("slots must be a list of {day, start_hour, end_hour}.")
    return _schedule_plan(ctx, camp, schedule.parse_slots(raw), f"Replace the ad schedule of {camp['name']}")


def plan_shift_ad_schedule(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    hours = _int(req.get("hours"), "hours")
    if hours == 0 or abs(hours) > 23:
        raise Refused("hours must be between -23 and 23, and not 0.")
    current = _live_slots(ctx, camp["id"])
    if not current:
        raise Refused("The campaign has no ad schedule to shift.")
    if any(s["start_minute"] != "ZERO" or s["end_minute"] != "ZERO" for s in current):
        raise Refused("Some slots don't start or end on the hour; use propose_set_ad_schedule instead.")
    slots = [schedule.Slot(s["day"], s["start_hour"], s["end_hour"], s["bid_modifier"]) for s in current]
    return _schedule_plan(ctx, camp, schedule.shift_slots(slots, hours), f"Shift the ad schedule of {camp['name']} by {hours:+d} hour(s)")


def after_ad_schedule(ctx, req, result):
    return {"slots": _live_slots(ctx, ctx.g.check_campaign(_id(req["campaign_id"], "campaign_id")))}


def inverse_ad_schedule(doc):
    slots = doc["before"]["slots"]
    if any(s["start_minute"] != "ZERO" or s["end_minute"] != "ZERO" for s in slots):
        raise Refused("The previous schedule had slots that don't start on the hour; restore it manually.")
    out = []
    for s in slots:
        d = {"day": s["day"], "start_hour": s["start_hour"], "end_hour": s["end_hour"]}
        if s["bid_modifier"] is not None:
            d["bid_modifier"] = s["bid_modifier"]
        out.append(d)
    return "set_ad_schedule", {"campaign_id": doc["request"]["campaign_id"], "slots": out}


# -- responsive search ads ------------------------------------------------------------


def _text_assets(raw, existing: list[dict], kind: str) -> list[dict]:
    """Normalises headline/description input to [{text, pinned_field?}].

    Plain strings keep the pin of an existing asset with the same text.
    """
    if not isinstance(raw, list):
        raise Refused(f"{kind} must be a list.")
    pins = {e["text"].lower(): e.get("pinned_field") for e in existing if e.get("pinned_field")}
    out = []
    for item in raw:
        if isinstance(item, str):
            d = {"text": item}
            if pins.get(" ".join(item.split()).lower()):
                d["pinned_field"] = pins[" ".join(item.split()).lower()]
        elif isinstance(item, dict) and "text" in item:
            d = {"text": item["text"]}
            if item.get("pinned_field"):
                d["pinned_field"] = item["pinned_field"]
        else:
            raise Refused(f"Each {kind[:-1]} must be text.")
        out.append(d)
    return out


def _check_assets(ctx, assets: list[dict], kind: str, live: list[dict]) -> tuple[list[dict], set[str]]:
    """Validates the full list. Returns it plus the texts kept from the live ad.

    The copy rules (length, dashes, "!", AI names, prices) apply only to lines
    that are new or changed. A line whose trimmed text exactly matches a live
    line of the same type is kept as it is, so a price a person put in the ad
    doesn't lock it. Count, uniqueness and pin checks still cover every line.
    """
    is_h = kind == "headlines"
    max_len = text_rules.HEADLINE_MAX if is_h else text_rules.DESCRIPTION_MAX
    lo, hi = (text_rules.HEADLINES_MIN, text_rules.HEADLINES_MAX) if is_h else (text_rules.DESCRIPTIONS_MIN, text_rules.DESCRIPTIONS_MAX)
    text_rules.check_count(kind.title(), len(assets), lo, hi)
    live_texts = {l["text"].strip(): l["text"] for l in live}
    out, kept = [], set()
    for i, a in enumerate(assets, 1):
        raw = a["text"] if isinstance(a["text"], str) else None
        if raw is not None and raw.strip() in live_texts:
            t = live_texts[raw.strip()]
            kept.add(t)
        else:
            t = text_rules.check_text(f"{kind[:-1].title()} {i}", a["text"], max_len, ctx.g.ad_text_banned)
        pin = a.get("pinned_field")
        if pin and (pin not in PIN_FIELDS or pin.startswith("HEADLINE") != is_h):
            raise Refused(f"Invalid pin {pin} on {kind[:-1]} {i}.")
        out.append({"text": t, **({"pinned_field": pin} if pin else {})})
    text_rules.check_unique(kind.title(), [a["text"] for a in out])
    return out, kept


def _asset_label(a: dict) -> str:
    return a["text"] + (f" [pinned {a['pinned_field'].replace('_', ' ').lower()}]" if a.get("pinned_field") else "")


def plan_update_rsa(ctx: Ctx, req: dict) -> Plan:
    ad_id = _id(req.get("ad_id"), "ad_id")
    ad = ctx.gw.rsa(ad_id)
    if not ad:
        raise Refused(f"Ad {ad_id} was not found.")
    if ad["campaign_id"] not in ctx.g.allowed_campaign_ids:
        raise Refused(f"Ad {ad_id} is in campaign {ad['campaign_id']}, which is not in the allowlist.")
    if ad["type"] != "RESPONSIVE_SEARCH_AD":
        raise Refused(f"Ad {ad_id} is a {ad['type']}, not a responsive search ad.")
    h_raw, d_raw, pin_first = req.get("headlines"), req.get("descriptions"), req.get("pin_first_headline")
    if h_raw is None and d_raw is None and pin_first is None:
        raise Refused("Nothing to change: give headlines, descriptions or pin_first_headline.")

    mask, rsa, rows, kept = [], {}, [], {"headlines": set(), "descriptions": set()}
    headlines = _text_assets(h_raw, ad["headlines"], "headlines") if h_raw is not None else [dict(h) for h in ad["headlines"]]
    if pin_first is not None:
        for h in headlines:
            if h.get("pinned_field") == "HEADLINE_1":
                h.pop("pinned_field")
        if pin_first:
            headlines[0]["pinned_field"] = "HEADLINE_1"
    if h_raw is not None or pin_first is not None:
        headlines, kept["headlines"] = _check_assets(ctx, headlines, "headlines", ad["headlines"])
        if headlines != ad["headlines"]:
            rsa["headlines"] = headlines
            mask.append("responsive_search_ad.headlines")
    if d_raw is not None:
        descriptions, kept["descriptions"] = _check_assets(
            ctx, _text_assets(d_raw, ad["descriptions"], "descriptions"), "descriptions", ad["descriptions"]
        )
        if descriptions != ad["descriptions"]:
            rsa["descriptions"] = descriptions
            mask.append("responsive_search_ad.descriptions")
    if not mask:
        raise Refused("The ad already has exactly that copy.")

    for kind in ("headlines", "descriptions"):
        if kind not in rsa:
            continue
        old, new = ad[kind], rsa[kind]
        for i in range(max(len(old), len(new))):
            b = _asset_label(old[i]) if i < len(old) else "(none)"
            a = _asset_label(new[i]) if i < len(new) else "(removed)"
            if i < len(new) and new[i]["text"] in kept[kind]:
                a += " (unchanged, existing)"
            rows.append({"item": f"{kind[:-1].title()} {i + 1}", "before": b, "after": a})
    return Plan(
        summary=f"Update responsive search ad {ad_id} ({', '.join(k for k in ('headlines', 'descriptions') if k in rsa)}). Final URL and paths stay the same.",
        before={"headlines": ad["headlines"], "descriptions": ad["descriptions"]},
        operations=[ops.update("ad", {"resource_name": ctx.rn.ad(ad_id), "responsive_search_ad": rsa}, mask)],
        rows=rows,
        warnings=["Edited ads go back through Google's policy review before serving again."],
    )


def after_update_rsa(ctx, req, result):
    ad = ctx.gw.rsa(str(req["ad_id"]))
    return {"headlines": ad["headlines"], "descriptions": ad["descriptions"]}


def inverse_update_rsa(doc):
    return "update_rsa", {"ad_id": doc["request"]["ad_id"], "headlines": doc["before"]["headlines"], "descriptions": doc["before"]["descriptions"], "pin_first_headline": None}


# -- campaign assets -----------------------------------------------------------------


def _asset_key(field_type: str, a: dict) -> str:
    if field_type == "SITELINK":
        return a["link_text"].lower()
    if field_type == "CALLOUT":
        return a["callout_text"].lower()
    return a["header"].lower() + ":" + "|".join(v.lower() for v in a["values"])


def _asset_desc(field_type: str, a: dict) -> str:
    if field_type == "SITELINK":
        extra = f" ({a['description1']} / {a['description2']})" if a.get("description1") else ""
        return f'Sitelink "{a["link_text"]}" to {a["final_urls"][0] if a.get("final_urls") else a.get("final_url")}{extra}'
    if field_type == "CALLOUT":
        return f'Callout "{a["callout_text"]}"'
    return f'Snippet {a["header"]}: {", ".join(a["values"])}'


def _parse_asset_item(ctx, field_type, item, n) -> tuple[dict, dict]:
    """Returns (asset resource body without resource_name, comparable view)."""
    banned = ctx.g.ad_text_banned
    if field_type == "SITELINK":
        if not isinstance(item, dict):
            raise Refused(f"Sitelink {n} must be {{link_text, final_url, description1?, description2?}}.")
        link = text_rules.check_text(f"Sitelink {n} text", item.get("link_text", ""), text_rules.SITELINK_TEXT_MAX, banned)
        url = str(item.get("final_url", "")).strip()
        if not url.startswith(ctx.g.asset_url_prefix):
            raise Refused(f"Sitelink {n} URL {url!r} must start with {ctx.g.asset_url_prefix}.")
        d1, d2 = item.get("description1"), item.get("description2")
        if bool(d1) != bool(d2):
            raise Refused(f"Sitelink {n}: give both descriptions or neither.")
        body = {"link_text": link}
        if d1:
            body["description1"] = text_rules.check_text(f"Sitelink {n} description 1", d1, text_rules.SITELINK_DESC_MAX, banned)
            body["description2"] = text_rules.check_text(f"Sitelink {n} description 2", d2, text_rules.SITELINK_DESC_MAX, banned)
        view = {**body, "final_urls": [url]}
        return {"sitelink_asset": body, "final_urls": [url]}, view
    if field_type == "CALLOUT":
        raw = item.get("text") if isinstance(item, dict) else item
        t = text_rules.check_text(f"Callout {n}", raw, text_rules.CALLOUT_MAX, banned)
        return {"callout_asset": {"callout_text": t}}, {"callout_text": t}
    if not isinstance(item, dict):
        raise Refused(f"Snippet {n} must be {{header, values}}.")
    header = next((h for h in SNIPPET_HEADERS if h.lower() == str(item.get("header", "")).strip().lower()), None)
    if not header:
        raise Refused(f"Snippet {n} header must be one of: {', '.join(SNIPPET_HEADERS)}.")
    values = item.get("values")
    if not isinstance(values, list) or not 3 <= len(values) <= 10:
        raise Refused(f"Snippet {n} needs 3 to 10 values.")
    values = [text_rules.check_text(f"Snippet {n} value", v, text_rules.SNIPPET_VALUE_MAX, banned) for v in values]
    text_rules.check_unique(f"Snippet {n} values", values)
    return {"structured_snippet_asset": {"header": header, "values": values}}, {"header": header, "values": values}


def plan_add_campaign_assets(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    field_type = str(req.get("type", "")).strip().upper()
    if field_type not in ASSET_FIELD_TYPES:
        raise Refused(f"type must be one of {', '.join(ASSET_FIELD_TYPES)}.")
    items = req.get("items")
    if not isinstance(items, list) or not items:
        raise Refused("items must be a non-empty list.")
    if len(items) > MAX_ASSETS_PER_PROPOSAL:
        raise Refused(f"At most {MAX_ASSETS_PER_PROPOSAL} assets per proposal.")
    linked = {_asset_key(field_type, a): a for a in ctx.gw.campaign_assets(camp["id"]) if a["field_type"] == field_type}
    rn = ctx.rn
    asset_ops, link_ops, rows, present, skipped, seen = [], [], [], [], [], set()
    for n, item in enumerate(items, 1):
        body, view = _parse_asset_item(ctx, field_type, item, n)
        key = _asset_key(field_type, view)
        if key in seen:
            continue
        seen.add(key)
        if key in linked:
            present.append(view)
            skipped.append(f"{_asset_desc(field_type, view)} is already on the campaign")
            continue
        temp = rn.asset(-(len(asset_ops) + 1))
        asset_ops.append(ops.create("asset", {"resource_name": temp, **body}))
        link_ops.append(ops.create("campaign_asset", {"asset": temp, "campaign": rn.campaign(camp["id"]), "field_type": field_type}))
        rows.append({"item": _asset_desc(field_type, view), "before": "not linked", "after": "new asset, linked"})
    if not asset_ops:
        raise Refused("Nothing to add. " + " ".join(skipped))
    return Plan(
        summary=f"Add {len(asset_ops)} {field_type.lower().replace('_', ' ')} asset(s) to {camp['name']}",
        before={"campaign_id": camp["id"], "already_present": present},
        operations=asset_ops + link_ops,
        rows=rows,
        skipped=skipped,
    )


def after_add_campaign_assets(ctx, req, result):
    camp_id = ctx.g.check_campaign(_id(req["campaign_id"], "campaign_id"))
    links = {r for r in result["resource_names"] if r and "/campaignAssets/" in r}
    return {
        "added": [a for a in ctx.gw.campaign_assets(camp_id) if a["resource_name"] in links],
        "resource_names": result["resource_names"],
    }


def _links_inverse_to_remove(doc):
    links = [{"asset_id": a["asset_id"], "field_type": a["field_type"]} for a in doc["after"]["added"]]
    if not links:
        raise Refused("The change log has no record of which assets were linked.")
    return "remove_campaign_assets", {"campaign_id": doc["request"]["campaign_id"], "links": links}


def _parse_links(req) -> list[dict]:
    links = req.get("links")
    if not isinstance(links, list) or not links:
        raise Refused("No assets given.")
    out = []
    for l in links:
        ft = str(l.get("field_type", "")).strip().upper()
        if ft not in ASSET_FIELD_TYPES:
            raise Refused(f"field_type must be one of {', '.join(ASSET_FIELD_TYPES)}.")
        out.append({"asset_id": _id(l.get("asset_id"), "asset_id"), "field_type": ft})
    return out


def plan_remove_campaign_assets(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    links = _parse_links(req)
    current = {(a["asset_id"], a["field_type"]): a for a in ctx.gw.campaign_assets(camp["id"])}
    targets = []
    for l in links:
        a = current.get((l["asset_id"], l["field_type"]))
        if not a:
            raise Refused(f"Asset {l['asset_id']} is not linked to campaign {camp['id']} as {l['field_type']}.")
        targets.append(a)
    rn = ctx.rn
    return Plan(
        summary=f"Remove {len(targets)} asset(s) from {camp['name']} (the assets stay in the account library)",
        before={"campaign_id": camp["id"], "links": targets},
        operations=[ops.remove("campaign_asset", rn.campaign_asset(camp["id"], a["asset_id"], a["field_type"])) for a in targets],
        rows=[{"item": _asset_desc(a["field_type"], a) + f" #{a['asset_id']}", "before": "linked", "after": "unlinked (asset stays in the library)"} for a in targets],
    )


def after_remove_campaign_assets(ctx, req, result):
    camp_id = ctx.g.check_campaign(_id(req["campaign_id"], "campaign_id"))
    wanted = {(l["asset_id"], l["field_type"]) for l in _parse_links(req)}
    return {"still_linked": [a for a in ctx.gw.campaign_assets(camp_id) if (a["asset_id"], a["field_type"]) in wanted]}


def inverse_remove_campaign_assets(doc):
    links = [{"asset_id": a["asset_id"], "field_type": a["field_type"]} for a in doc["before"]["links"]]
    return "link_campaign_assets", {"campaign_id": doc["request"]["campaign_id"], "links": links}


def plan_link_campaign_assets(ctx: Ctx, req: dict) -> Plan:
    """Only reachable through propose_undo of a removal: re-links existing assets."""
    camp = _campaign(ctx, req.get("campaign_id"))
    links = _parse_links(req)
    current = {(a["asset_id"], a["field_type"]) for a in ctx.gw.campaign_assets(camp["id"])}
    already = [l for l in links if (l["asset_id"], l["field_type"]) in current]
    if already:
        raise Refused("Already linked: " + ", ".join(l["asset_id"] for l in already))
    rn = ctx.rn
    return Plan(
        summary=f"Re-link {len(links)} existing asset(s) to {camp['name']}",
        before={"campaign_id": camp["id"], "linked": []},
        operations=[ops.create("campaign_asset", {"asset": rn.asset(l["asset_id"]), "campaign": rn.campaign(camp["id"]), "field_type": l["field_type"]}) for l in links],
        rows=[{"item": f"{l['field_type'].title()} asset #{l['asset_id']}", "before": "not linked", "after": "linked"} for l in links],
    )


def after_link_campaign_assets(ctx, req, result):
    camp_id = ctx.g.check_campaign(_id(req["campaign_id"], "campaign_id"))
    wanted = {(l["asset_id"], l["field_type"]) for l in _parse_links(req)}
    return {"added": [a for a in ctx.gw.campaign_assets(camp_id) if (a["asset_id"], a["field_type"]) in wanted]}


# -- undo guard ----------------------------------------------------------------------


def _sorted_tuples(items, keys):
    return sorted(tuple(str(i.get(k)) for k in keys) for i in items or [])


_UNDO_VIEWS = {
    "add_campaign_negatives": lambda doc, v: {"added": _sorted_tuples(v.get("added"), ("criterion_id", "text", "match_type"))},
    "add_keywords": lambda doc, v: {"added": _sorted_tuples(v.get("added"), ("ad_group_id", "criterion_id", "text", "match_type"))},
    "add_campaign_assets": lambda doc, v: {"added": _sorted_tuples(v.get("added"), ("asset_id", "field_type"))},
    "link_campaign_assets": lambda doc, v: {"added": _sorted_tuples(v.get("added"), ("asset_id", "field_type"))},
    "remove_campaign_negatives": lambda doc, v: {"still_present": _sorted_tuples(v.get("still_present"), ("criterion_id",))},
    "remove_keywords": lambda doc, v: {"still_present": _sorted_tuples(v.get("still_present"), ("criterion_id",))},
    "remove_campaign_assets": lambda doc, v: {"still_linked": _sorted_tuples(v.get("still_linked"), ("asset_id", "field_type"))},
    "set_bidding": lambda doc, v: {
        **{k: x for k, x in v.items() if k != "ad_groups"},
        **(
            {"ad_groups": {g["ad_group_id"]: g["cpc_bid_micros"] for g in v.get("ad_groups") or [] if g["ad_group_id"] in {b["ad_group_id"] for b in doc["before"]["ad_groups"]}}}
            if "ad_groups" in v and "ad_groups" in doc["before"]
            else {}
        ),
    },
}


def undo_view(doc: dict, after: dict) -> dict:
    """The part of an "after" record an undo depends on, in comparable form.

    Only keys present in the stored "after" are compared, so entries written by
    older versions of this server still work.
    """
    ref = doc.get("after") or {}
    view = {k: after.get(k) for k in ref if k not in ("resource_names", "read_error")}
    project = _UNDO_VIEWS.get(doc["type"])
    return project(doc, view) if project else view


# -- registry ------------------------------------------------------------------------


TYPES: dict[str, ChangeType] = {
    "add_campaign_negatives": ChangeType(plan_add_campaign_negatives, after_add_campaign_negatives, inverse_add_campaign_negatives),
    "remove_campaign_negatives": ChangeType(plan_remove_campaign_negatives, after_remove_campaign_negatives, inverse_remove_campaign_negatives),
    "add_keywords": ChangeType(plan_add_keywords, after_add_keywords, inverse_add_keywords),
    "remove_keywords": ChangeType(plan_remove_keywords, after_remove_keywords, inverse_remove_keywords),
    "set_keyword_status": ChangeType(plan_set_keyword_status, after_set_keyword_status, inverse_set_keyword_status),
    "set_ad_group_status": ChangeType(plan_set_ad_group_status, after_set_ad_group_status, inverse_set_ad_group_status),
    "set_campaign_status": ChangeType(plan_set_campaign_status, after_set_campaign_status, inverse_set_campaign_status),
    "set_daily_budget": ChangeType(plan_set_daily_budget, after_set_daily_budget, inverse_set_daily_budget),
    "set_bidding": ChangeType(plan_set_bidding, after_set_bidding, inverse_set_bidding),
    "set_ad_group_cpc": ChangeType(plan_set_ad_group_cpc, after_set_ad_group_cpc, inverse_set_ad_group_cpc),
    "set_keyword_cpc": ChangeType(plan_set_keyword_cpc, after_set_keyword_cpc, inverse_set_keyword_cpc),
    "set_device_bid_adjustment": ChangeType(plan_set_device_bid_adjustment, after_set_device_bid_adjustment, inverse_set_device_bid_adjustment),
    "set_ad_schedule": ChangeType(plan_set_ad_schedule, after_ad_schedule, inverse_ad_schedule),
    "shift_ad_schedule": ChangeType(plan_shift_ad_schedule, after_ad_schedule, inverse_ad_schedule),
    "update_rsa": ChangeType(plan_update_rsa, after_update_rsa, inverse_update_rsa),
    "add_campaign_assets": ChangeType(plan_add_campaign_assets, after_add_campaign_assets, _links_inverse_to_remove),
    "remove_campaign_assets": ChangeType(plan_remove_campaign_assets, after_remove_campaign_assets, inverse_remove_campaign_assets),
    "link_campaign_assets": ChangeType(plan_link_campaign_assets, after_link_campaign_assets, _links_inverse_to_remove),
}
