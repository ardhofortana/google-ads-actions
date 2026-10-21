"""In-memory stand-in for the Google Ads account, seeded to match the brief.

``mutate`` checks every operation parses as a real MutateOperation proto, then
applies the batch to a copy of the state and only commits it when
validate_only is false, mirroring partial_failure=false atomicity.
"""

import copy
import itertools

from ads_actions import ops
from ads_actions.errors import AdsApiError

CUSTOMER = "6775198830"
CAMPAIGN = "24378039028"
BUDGET_RN = f"customers/{CUSTOMER}/campaignBudgets/15954093257"

INITIAL_SCHEDULE = [("MONDAY", 19, 24)] + [
    s for d in ("TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY") for s in ((d, 0, 7), (d, 19, 24))
] + [("SATURDAY", 0, 7)]


def seed_state() -> dict:
    ids = itertools.count(900001)
    ad_groups = {
        "201460749979": {"name": "AI Integration Services", "status": "ENABLED"},
        "201460750019": {"name": "AI Automation", "status": "ENABLED"},
        "201460750059": {"name": "AI Consulting", "status": "ENABLED"},
        "201460750219": {"name": "Custom AI Solutions", "status": "PAUSED"},
        "201460750259": {"name": "AI Development Partner", "status": "PAUSED"},
    }
    keywords = {}
    for ag, text, mt in [
        ("201460749979", "ai integration services", "PHRASE"),
        ("201460749979", "ai integration company", "EXACT"),
        ("201460750019", "business process automation ai", "PHRASE"),
        ("201460750059", "ai consulting firm", "PHRASE"),
        ("201460750219", "custom ai solutions", "PHRASE"),
    ]:
        cid = str(next(ids))
        keywords[(ag, cid)] = {"text": text, "match_type": mt, "status": "ENABLED"}
    schedule = {
        str(next(ids)): {"day": d, "start_hour": a, "end_hour": b, "start_minute": "ZERO", "end_minute": "ZERO", "bid_modifier": None}
        for d, a, b in INITIAL_SCHEDULE
    }
    return {
        "campaign": {
            "id": CAMPAIGN,
            "name": "Search | AI Integration | US",
            "status": "ENABLED",
            "budget_resource_name": BUDGET_RN,
            "budget_amount_micros": 900_000 * 1_000_000,
            "budget_explicitly_shared": False,
            "budget_reference_count": 1,
            "bidding_strategy_type": "TARGET_SPEND",
            "portfolio_bidding_strategy": None,
            "cpc_bid_ceiling_micros": 270_000 * 1_000_000,
            "target_cpa_micros": None,
        },
        "ad_groups": ad_groups,
        "keywords": keywords,
        "negatives": {str(next(ids)): {"text": "jobs at google", "match_type": "PHRASE"}},
        "schedule": schedule,
        "ads": {
            "700000000001": {
                "ad_group_id": "201460749979",
                "type": "RESPONSIVE_SEARCH_AD",
                "status": "ENABLED",
                "final_urls": ["https://djavaweb.com/ai-integration/"],
                "path1": "ai",
                "path2": "integration",
                "headlines": [{"text": "AI Integration Services", "pinned_field": "HEADLINE_1"}, {"text": "Custom AI For Your Stack"}, {"text": "Talk To An Engineer Today"}],
                "descriptions": [{"text": "We connect AI to your existing tools and data."}, {"text": "Senior engineers. Clear scope. Fixed timelines."}],
            }
        },
        "assets": {},
        "campaign_assets": {},
        "metrics_today": {"cost_micros": 400_000 * 1_000_000, "clicks": 30, "impressions": 900, "conversions": 1.0},
        "hourly": [],
        "next_id": 990000,
    }


class FakeAds:
    def __init__(self, customer_id: str = CUSTOMER, state: dict | None = None):
        self.customer_id = customer_id
        self.state = state or seed_state()
        self.mutate_calls: list[tuple[list[dict], bool]] = []

    # -- reads -------------------------------------------------------------

    def campaign(self, campaign_id):
        c = self.state["campaign"]
        return copy.deepcopy(c) if c["id"] == str(campaign_id) else None

    def campaign_negatives(self, campaign_id):
        if str(campaign_id) != CAMPAIGN:
            return []
        return [{"criterion_id": k, **v} for k, v in self.state["negatives"].items()]

    def campaign_keywords(self, campaign_id):
        if str(campaign_id) != CAMPAIGN:
            return []
        ags = self.state["ad_groups"]
        return [
            {"ad_group_id": ag, "ad_group_name": ags[ag]["name"], "ad_group_status": ags[ag]["status"], "criterion_id": cid, **kw}
            for (ag, cid), kw in self.state["keywords"].items()
        ]

    def ad_group(self, ad_group_id):
        ag = self.state["ad_groups"].get(str(ad_group_id))
        return {"id": str(ad_group_id), "campaign_id": CAMPAIGN, **ag} if ag else None

    def ad_schedule(self, campaign_id):
        return [{"criterion_id": k, **v} for k, v in self.state["schedule"].items()]

    def rsa(self, ad_id):
        ad = self.state["ads"].get(str(ad_id))
        return copy.deepcopy({"ad_id": str(ad_id), "campaign_id": CAMPAIGN, **ad}) if ad else None

    def campaign_assets(self, campaign_id):
        out = []
        for (camp, asset_id, ft), link in self.state["campaign_assets"].items():
            a = self.state["assets"][asset_id]
            out.append(
                {
                    "resource_name": f"customers/{CUSTOMER}/campaignAssets/{camp}~{asset_id}~{ft}",
                    "asset_id": asset_id,
                    "field_type": ft,
                    "status": "ENABLED",
                    "asset_type": ft,
                    "final_urls": a.get("final_urls", []),
                    "link_text": a.get("sitelink_asset", {}).get("link_text", ""),
                    "description1": a.get("sitelink_asset", {}).get("description1", ""),
                    "description2": a.get("sitelink_asset", {}).get("description2", ""),
                    "callout_text": a.get("callout_asset", {}).get("callout_text", ""),
                    "header": a.get("structured_snippet_asset", {}).get("header", ""),
                    "values": a.get("structured_snippet_asset", {}).get("values", []),
                }
            )
        return out

    def time_zone(self):
        return "Asia/Jakarta"

    def today_metrics(self, campaign_id):
        return dict(self.state["metrics_today"])

    def hourly_metrics(self, campaign_id, start_date, end_date):
        return [h for h in self.state["hourly"] if start_date <= h["date"] <= end_date]

    # -- writes ------------------------------------------------------------

    def mutate(self, operations, validate_only):
        self.mutate_calls.append((copy.deepcopy(operations), validate_only))
        for o in operations:
            ops.to_proto(o)  # raises on anything that isn't a valid MutateOperation
        st = copy.deepcopy(self.state)
        temp: dict[str, str] = {}
        results = [self._apply(st, o, temp) for o in operations]
        if not validate_only:
            self.state = st
            return {"resource_names": results, "request_ids": ["fake-req-1"]}
        return {"resource_names": [], "request_ids": ["fake-req-vo"]}

    def _new_id(self, st):
        st["next_id"] += 1
        return str(st["next_id"])

    def _apply(self, st, op, temp):
        kind, action, body = ops.describe(op)
        pre = f"customers/{CUSTOMER}/"

        def fail(msg):
            raise AdsApiError([msg], "fake-req-err")

        if kind == "campaign_criterion":
            if action == "remove":
                camp, crit = body.removeprefix(pre + "campaignCriteria/").split("~")
                if crit in st["negatives"]:
                    del st["negatives"][crit]
                elif crit in st["schedule"]:
                    del st["schedule"][crit]
                else:
                    fail(f"RESOURCE_NOT_FOUND {body}")
                return body
            crit = self._new_id(st)
            if "keyword" in body:
                st["negatives"][crit] = {"text": body["keyword"]["text"], "match_type": body["keyword"]["match_type"]}
            else:
                s = body["ad_schedule"]
                st["schedule"][crit] = {
                    "day": s["day_of_week"], "start_hour": s.get("start_hour", 0), "end_hour": s["end_hour"],
                    "start_minute": "ZERO", "end_minute": "ZERO", "bid_modifier": body.get("bid_modifier"),
                }
            return f"{pre}campaignCriteria/{CAMPAIGN}~{crit}"
        if kind == "ad_group_criterion":
            if action == "create":
                ag = body["ad_group"].rsplit("/", 1)[1]
                crit = self._new_id(st)
                st["keywords"][(ag, crit)] = {"text": body["keyword"]["text"], "match_type": body["keyword"]["match_type"], "status": body["status"]}
                return f"{pre}adGroupCriteria/{ag}~{crit}"
            rn = body if action == "remove" else body["resource_name"]
            key = tuple(rn.removeprefix(pre + "adGroupCriteria/").split("~"))
            if key not in st["keywords"]:
                fail(f"RESOURCE_NOT_FOUND {rn}")
            if action == "remove":
                del st["keywords"][key]
            else:
                st["keywords"][key]["status"] = body["status"]
            return rn
        if kind == "ad_group":
            st["ad_groups"][body["resource_name"].rsplit("/", 1)[1]]["status"] = body["status"]
            return body["resource_name"]
        if kind == "campaign":
            c = st["campaign"]
            if "status" in body:
                c["status"] = body["status"]
            if "target_spend" in body:
                c["bidding_strategy_type"] = "TARGET_SPEND"
                c["cpc_bid_ceiling_micros"] = int(body["target_spend"].get("cpc_bid_ceiling_micros", 0)) or None
                c["target_cpa_micros"] = None
            if "maximize_conversions" in body:
                c["bidding_strategy_type"] = "MAXIMIZE_CONVERSIONS"
                c["target_cpa_micros"] = int(body["maximize_conversions"].get("target_cpa_micros", 0)) or None
                c["cpc_bid_ceiling_micros"] = None
            return body["resource_name"]
        if kind == "campaign_budget":
            st["campaign"]["budget_amount_micros"] = int(body["amount_micros"])
            return body["resource_name"]
        if kind == "ad":
            ad = st["ads"][body["resource_name"].rsplit("/", 1)[1]]
            assert "final_urls" not in body and "path1" not in body.get("responsive_search_ad", {})
            ad.update(copy.deepcopy(body["responsive_search_ad"]))
            return body["resource_name"]
        if kind == "asset":
            aid = self._new_id(st)
            temp[body["resource_name"]] = aid
            st["assets"][aid] = {k: v for k, v in body.items() if k != "resource_name"}
            return f"{pre}assets/{aid}"
        if kind == "campaign_asset":
            if action == "create":
                aid = temp.get(body["asset"]) or body["asset"].rsplit("/", 1)[1]
                if aid not in st["assets"]:
                    fail(f"RESOURCE_NOT_FOUND {body['asset']}")
                st["campaign_assets"][(CAMPAIGN, aid, body["field_type"])] = {}
                return f"{pre}campaignAssets/{CAMPAIGN}~{aid}~{body['field_type']}"
            key = tuple(body.removeprefix(pre + "campaignAssets/").split("~"))
            if key not in st["campaign_assets"]:
                fail(f"RESOURCE_NOT_FOUND {body}")
            del st["campaign_assets"][key]
            return body
        fail(f"Fake does not support {kind}")
