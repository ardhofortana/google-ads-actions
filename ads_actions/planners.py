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
STRATEGY_FROM_API = {"TARGET_SPEND": "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS": "MAXIMIZE_CONVERSIONS"}


@dataclass
class Ctx:
    gw: Gateway
    g: Guardrails

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
    existing = {(n["text"].lower(), n["match_type"]): n for n in ctx.gw.campaign_negatives(camp["id"])}
    keywords = ctx.gw.campaign_keywords(camp["id"])
    active = [k for k in keywords if k["status"] == "ENABLED" and k["ad_group_status"] == "ENABLED"]
    dormant = [k for k in keywords if k["status"] == "ENABLED" and k["ad_group_status"] != "ENABLED"]

    to_add, present, warnings, skipped = [], [], [], []
    for it in items:
        label = f'"{it["text"]}" ({it["match_type"]})'
        if (it["text"], it["match_type"]) in existing:
            present.append(dict(it))
            skipped.append(f"{label} already exists")
            continue
        blocked = [k for k in active if negative_blocks(it["text"], it["match_type"], k["text"])]
        if blocked:
            names = ", ".join(f'"{k["text"]}" in {k["ad_group_name"]}' for k in blocked)
            warnings.append(f"Rejected {label}: it would block active keyword(s) {names}.")
            continue
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
        before={"campaign_id": camp["id"], "already_present": present, "to_add": to_add},
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
    if pct > ctx.g.budget_max_change_pct:
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


def _bidding_label(strategy, ceiling_micros, cpa_micros) -> str:
    if strategy == "MAXIMIZE_CLICKS":
        return "Maximize clicks" + (f", CPC ceiling Rp{ceiling_micros // MICROS:,}" if ceiling_micros else ", no CPC ceiling")
    if strategy == "MAXIMIZE_CONVERSIONS":
        return "Maximize conversions" + (f", target CPA Rp{cpa_micros // MICROS:,}" if cpa_micros else ", no target CPA")
    return strategy


def plan_set_bidding(ctx: Ctx, req: dict) -> Plan:
    camp = _campaign(ctx, req.get("campaign_id"))
    strategy = str(req.get("strategy", "")).strip().upper()
    ceiling, cpa = req.get("cpc_ceiling_idr"), req.get("target_cpa_idr")
    if strategy not in ("MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS"):
        raise Refused("strategy must be MAXIMIZE_CLICKS or MAXIMIZE_CONVERSIONS.")
    if camp["portfolio_bidding_strategy"]:
        raise Refused("The campaign uses a portfolio bid strategy; changing it is not allowed here.")
    if strategy == "MAXIMIZE_CLICKS":
        if cpa is not None:
            raise Refused("target_cpa_idr only applies to MAXIMIZE_CONVERSIONS.")
        if ceiling is not None:
            ctx.g.cpc_ceiling.check("CPC ceiling", _int(ceiling, "cpc_ceiling_idr"))
    else:
        if ceiling is not None:
            raise Refused("cpc_ceiling_idr only applies to MAXIMIZE_CLICKS.")
        if cpa is not None:
            ctx.g.target_cpa.check("Target CPA", _int(cpa, "target_cpa_idr"))

    cur_strategy = STRATEGY_FROM_API.get(camp["bidding_strategy_type"], camp["bidding_strategy_type"])
    cur_ceiling, cur_cpa = camp["cpc_bid_ceiling_micros"], camp["target_cpa_micros"]
    new_ceiling = ceiling * MICROS if ceiling is not None else None
    new_cpa = cpa * MICROS if cpa is not None else None
    same_strategy = cur_strategy == strategy
    if same_strategy and (
        (strategy == "MAXIMIZE_CLICKS" and cur_ceiling == new_ceiling)
        or (strategy == "MAXIMIZE_CONVERSIONS" and cur_cpa == new_cpa)
    ):
        raise Refused("Bidding is already set that way.")

    resource = {"resource_name": ctx.rn.campaign(camp["id"])}
    if strategy == "MAXIMIZE_CLICKS":
        resource["target_spend"] = {"cpc_bid_ceiling_micros": str(new_ceiling)} if new_ceiling else {}
        mask = ["target_spend.cpc_bid_ceiling_micros"] if new_ceiling or same_strategy else ["target_spend"]
    else:
        resource["maximize_conversions"] = {"target_cpa_micros": str(new_cpa)} if new_cpa else {}
        mask = ["maximize_conversions.target_cpa_micros"] if new_cpa or same_strategy else ["maximize_conversions"]

    warnings = []
    if not same_strategy:
        warnings.append("Switching bid strategy restarts Smart Bidding learning.")
    return Plan(
        summary=f"Set bidding of {camp['name']} to {_bidding_label(strategy, new_ceiling, new_cpa)}",
        before={"bidding_strategy_type": camp["bidding_strategy_type"], "cpc_bid_ceiling_micros": cur_ceiling, "target_cpa_micros": cur_cpa},
        operations=[ops.update("campaign", resource, mask)],
        rows=_status_rows(f"Bidding, {camp['name']}", _bidding_label(cur_strategy, cur_ceiling, cur_cpa), _bidding_label(strategy, new_ceiling, new_cpa)),
        warnings=warnings,
    )


def after_set_bidding(ctx, req, result):
    c = ctx.gw.campaign(str(req["campaign_id"]))
    return {k: c[k] for k in ("bidding_strategy_type", "cpc_bid_ceiling_micros", "target_cpa_micros")}


def inverse_set_bidding(doc):
    b = doc["before"]
    strategy = STRATEGY_FROM_API.get(b["bidding_strategy_type"])
    if not strategy:
        raise Refused(f"The previous strategy {b['bidding_strategy_type']} can't be restored by this server.")
    req = {"campaign_id": doc["request"]["campaign_id"], "strategy": strategy, "cpc_ceiling_idr": None, "target_cpa_idr": None}
    if strategy == "MAXIMIZE_CLICKS" and b["cpc_bid_ceiling_micros"]:
        req["cpc_ceiling_idr"] = b["cpc_bid_ceiling_micros"] // MICROS
    if strategy == "MAXIMIZE_CONVERSIONS" and b["target_cpa_micros"]:
        req["target_cpa_idr"] = b["target_cpa_micros"] // MICROS
    return "set_bidding", req


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


def _check_assets(ctx, assets: list[dict], kind: str) -> list[dict]:
    is_h = kind == "headlines"
    max_len = text_rules.HEADLINE_MAX if is_h else text_rules.DESCRIPTION_MAX
    lo, hi = (text_rules.HEADLINES_MIN, text_rules.HEADLINES_MAX) if is_h else (text_rules.DESCRIPTIONS_MIN, text_rules.DESCRIPTIONS_MAX)
    text_rules.check_count(kind.title(), len(assets), lo, hi)
    out = []
    for i, a in enumerate(assets, 1):
        t = text_rules.check_text(f"{kind[:-1].title()} {i}", a["text"], max_len, ctx.g.ad_text_banned)
        pin = a.get("pinned_field")
        if pin and (pin not in PIN_FIELDS or pin.startswith("HEADLINE") != is_h):
            raise Refused(f"Invalid pin {pin} on {kind[:-1]} {i}.")
        out.append({"text": t, **({"pinned_field": pin} if pin else {})})
    text_rules.check_unique(kind.title(), [a["text"] for a in out])
    return out


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

    mask, rsa, rows = [], {}, []
    headlines = _text_assets(h_raw, ad["headlines"], "headlines") if h_raw is not None else [dict(h) for h in ad["headlines"]]
    if pin_first is not None:
        for h in headlines:
            if h.get("pinned_field") == "HEADLINE_1":
                h.pop("pinned_field")
        if pin_first:
            headlines[0]["pinned_field"] = "HEADLINE_1"
    if h_raw is not None or pin_first is not None:
        headlines = _check_assets(ctx, headlines, "headlines")
        if headlines != ad["headlines"]:
            rsa["headlines"] = headlines
            mask.append("responsive_search_ad.headlines")
    if d_raw is not None:
        descriptions = _check_assets(ctx, _text_assets(d_raw, ad["descriptions"], "descriptions"), "descriptions")
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
            rows.append({"item": f"{kind[:-1].title()} {i + 1}", "before": b, "after": a + ("" if a != b else " (unchanged)")})
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
        rows.append({"item": _asset_desc(field_type, view), "before": "not on campaign", "after": "added"})
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
        rows=[{"item": _asset_desc(a["field_type"], a) + f" #{a['asset_id']}", "before": "on campaign", "after": "removed from campaign"} for a in targets],
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
        rows=[{"item": f"{l['field_type'].title()} asset #{l['asset_id']}", "before": "not on campaign", "after": "linked"} for l in links],
    )


def after_link_campaign_assets(ctx, req, result):
    camp_id = ctx.g.check_campaign(_id(req["campaign_id"], "campaign_id"))
    wanted = {(l["asset_id"], l["field_type"]) for l in _parse_links(req)}
    return {"added": [a for a in ctx.gw.campaign_assets(camp_id) if (a["asset_id"], a["field_type"]) in wanted]}


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
    "set_ad_schedule": ChangeType(plan_set_ad_schedule, after_ad_schedule, inverse_ad_schedule),
    "shift_ad_schedule": ChangeType(plan_shift_ad_schedule, after_ad_schedule, inverse_ad_schedule),
    "update_rsa": ChangeType(plan_update_rsa, after_update_rsa, inverse_update_rsa),
    "add_campaign_assets": ChangeType(plan_add_campaign_assets, after_add_campaign_assets, _links_inverse_to_remove),
    "remove_campaign_assets": ChangeType(plan_remove_campaign_assets, after_remove_campaign_assets, inverse_remove_campaign_assets),
    "link_campaign_assets": ChangeType(plan_link_campaign_assets, after_link_campaign_assets, _links_inverse_to_remove),
}
