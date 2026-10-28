"""Propose / execute engine and housekeeping.

The rule enforced here: nothing reaches Google Ads with validate_only=false
except (a) execute_change on a stored, unexpired proposal whose re-plan matches
what was proposed, and (b) the emergency pause when live data justifies it.
"""

import asyncio
import datetime as dt
import json
import secrets
from dataclasses import dataclass
from typing import Callable
from zoneinfo import ZoneInfo

from ads_actions import planners
from ads_actions.errors import AdsApiError, Refused
from ads_actions.gateway import Gateway
from ads_actions.guardrails import Guardrails
from ads_actions.planners import Ctx, Plan

ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
EXECUTED = ("executed", "emergency_executed")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _iso(value):
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {k: _iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_iso(v) for v in value]
    return value


def table(rows: list[dict]) -> str:
    def cell(s):
        return str(s).replace("|", "\\|").replace("\n", " ")

    lines = ["| Item | Before | After |", "|---|---|---|"]
    lines += [f"| {cell(r['item'])} | {cell(r['before'])} | {cell(r['after'])} |" for r in rows]
    return "\n".join(lines)


@dataclass
class Session:
    """Who is calling, and how to reach Google Ads as them."""

    email: str | None
    make_gateway: Callable[[str], Gateway]


class Engine:
    def __init__(self, guardrails: Guardrails, store, clock: Callable[[], dt.datetime] = utcnow):
        self.g = guardrails
        self.store = store
        self.clock = clock

    # -- helpers ---------------------------------------------------------------

    def _ctx(self, session: Session, customer_id, undo_of: dict | None = None) -> Ctx:
        self.g.check_user(session.email)
        cid = self.g.check_customer(customer_id)
        return Ctx(gw=session.make_gateway(cid), g=self.g, undo_of=undo_of)

    def _undo_live_mismatch(self, ctx: Ctx, original: dict) -> str | None:
        """None if the live account still shows what ``original`` left behind."""
        ct = planners.TYPES[original["type"]]
        result = {"resource_names": (original.get("mutate_result") or {}).get("resource_names", []), "request_ids": []}
        live = planners.undo_view(original, ct.after(ctx, original["request"], result))
        left = planners.undo_view(original, original["after"])
        if _canon(live) == _canon(left):
            return None
        return (
            f"Undo refused, nothing changed: the live values no longer match what {original['change_id']} left behind "
            "(the account was changed since). Make a normal proposal instead.\n"
            f"Left behind: {_canon(left)[:400]}\nNow: {_canon(live)[:400]}"
        )

    async def _new_id(self, doc: dict) -> str:
        for _ in range(20):
            doc["change_id"] = "chg_" + "".join(secrets.choice(ID_ALPHABET) for _ in range(4))
            if await self.store.create(doc):
                return doc["change_id"]
        raise RuntimeError("Could not allocate a change id.")

    async def _load(self, ctx: Ctx, change_id: str) -> dict:
        doc = await self.store.get(str(change_id).strip())
        if not doc or doc.get("customer_id") != ctx.gw.customer_id:
            raise Refused(f"Change {change_id} was not found.")
        return doc

    async def _expire_if_due(self, doc: dict) -> dict:
        if doc["status"] == "proposed" and self.clock() > doc["expires_at"]:
            if await self.store.claim(doc["change_id"], "proposed", "expired"):
                doc["status"] = "expired"
        return doc

    @staticmethod
    def _proposal_view(doc: dict) -> dict:
        return _iso(
            {
                "change_id": doc["change_id"],
                "type": doc["type"],
                "status": doc["status"],
                "summary": doc["summary"],
                "before_after": table(doc["rows"]),
                "warnings": doc.get("warnings", []),
                "skipped": doc.get("skipped", []),
                "created_at": doc["created_at"],
                "expires_at": doc.get("expires_at"),
                **({"undo_of": doc["undo_of"]} if doc.get("undo_of") else {}),
            }
        )

    # -- propose ---------------------------------------------------------------

    async def propose(self, session: Session, customer_id, change_type: str, request: dict, *, undo_doc: dict | None = None) -> dict:
        """``undo_doc`` is passed only by propose_undo; tools can't reach it."""
        ctx = self._ctx(session, customer_id, undo_doc)
        ct = planners.TYPES[change_type]
        plan: Plan = await asyncio.to_thread(ct.plan, ctx, request)
        validation = await asyncio.to_thread(ctx.gw.mutate, plan.operations, True)
        now = self.clock()
        doc = {
            "type": change_type,
            "status": "proposed",
            "customer_id": ctx.gw.customer_id,
            "request": request,
            "summary": plan.summary,
            "before": plan.before,
            "after": None,
            "rows": plan.rows,
            "warnings": plan.warnings,
            "skipped": plan.skipped,
            "operations": plan.operations,
            "validate_only_result": {"ok": True, **validation},
            "created_at": now,
            "expires_at": now + dt.timedelta(hours=self.g.proposal_ttl_hours),
            "executed_at": None,
            "user_email": session.email,
            "google_request_ids": list(validation.get("request_ids", [])),
            "error": None,
            "undo_of": undo_doc["change_id"] if undo_doc else None,
        }
        await self._new_id(doc)
        view = self._proposal_view(doc)
        view["next_step"] = f"Show the table to the user. Only after explicit approval, call execute_change('{doc['change_id']}')."
        return view

    async def propose_undo(self, session: Session, customer_id, change_id: str) -> dict:
        ctx = self._ctx(session, customer_id)
        doc = await self._load(ctx, change_id)
        if doc["status"] not in EXECUTED:
            raise Refused(f"Change {change_id} is {doc['status']}; only executed changes can be undone.")
        if doc.get("undone_by"):
            raise Refused(f"Change {change_id} was already undone by {doc['undone_by']}.")
        if not doc.get("after") or doc["after"].get("read_error"):
            raise Refused(f"Change {change_id} has no recorded after-state, so it can't be undone automatically.")
        mismatch = await asyncio.to_thread(self._undo_live_mismatch, ctx, doc)
        if mismatch:
            raise Refused(mismatch)
        # The undo request is built only from the audit-log entry, never from input.
        undo_type, undo_request = planners.TYPES[doc["type"]].inverse(doc)
        undo_request = {"customer_id": ctx.gw.customer_id, **undo_request}
        return await self.propose(session, customer_id, undo_type, undo_request, undo_doc=doc)

    # -- execute ---------------------------------------------------------------

    async def execute(self, session: Session, customer_id, change_id: str) -> dict:
        ctx = self._ctx(session, customer_id)
        doc = await self._expire_if_due(await self._load(ctx, change_id))
        cid = doc["change_id"]
        if doc["status"] != "proposed":
            hint = " Make a new proposal." if doc["status"] in ("expired", "failed") else ""
            raise Refused(f"Change {cid} is {doc['status']} and can't be executed.{hint}")
        if not await self.store.claim(cid, "proposed", "executing"):
            raise Refused(f"Change {cid} is no longer pending.")

        async def fail(message: str, kind: str, **extra):
            # Refused executions stay in the log with a reason code and, for drift,
            # the live values found at execute time.
            await self.store.update(
                cid, {"status": "failed", "error": message, "failure_kind": kind, "failed_at": self.clock(), "failed_by": session.email, **extra}
            )
            raise Refused(message)

        if doc.get("undo_of"):
            original = await self.store.get(doc["undo_of"])
            if not original or original["status"] not in EXECUTED:
                await fail(f"Refused, nothing changed: the change being undone ({doc['undo_of']}) is missing or not executed.", "undo_mismatch")
            if original.get("undone_by"):
                await fail(f"Refused, nothing changed: {original['change_id']} was already undone by {original['undone_by']}.", "undo_mismatch")
            expected_type, expected = planners.TYPES[original["type"]].inverse(original)
            expected = {"customer_id": doc["customer_id"], **expected}
            if expected_type != doc["type"] or _canon(expected) != _canon(doc["request"]):
                await fail(
                    f"Refused, nothing changed: this undo's values don't match the 'before' snapshot of {original['change_id']} in the audit log.",
                    "undo_mismatch",
                )
            ctx.undo_of = original
            try:
                mismatch = await asyncio.to_thread(self._undo_live_mismatch, ctx, original)
            except Exception as e:
                await fail(f"Could not re-read the account, nothing changed: {e}", "read_error")
            if mismatch:
                await fail(mismatch, "undo_live_changed")

        ct = planners.TYPES[doc["type"]]
        try:
            plan: Plan = await asyncio.to_thread(ct.plan, ctx, doc["request"])
        except Refused as e:
            await fail(f"Refused on re-check, nothing changed: {e} Make a new proposal if still wanted.", "recheck_refused")
        except Exception as e:
            await fail(f"Could not re-read the account, nothing changed: {e}", "read_error")
        if _canon(plan.before) != _canon(doc["before"]):
            await fail(
                "Refused, nothing changed: the live values no longer match the 'before' snapshot "
                "(the account was changed since the proposal). Make a new proposal.\n"
                f"Now:\n{table(plan.rows)}",
                "drift",
                live_before=plan.before,
                live_rows=plan.rows,
            )
        if _canon(plan.operations) != _canon(doc["operations"]):
            await fail(
                "Refused, nothing changed: the change would now be different from what was proposed. Make a new proposal.",
                "plan_changed",
                live_before=plan.before,
                live_rows=plan.rows,
            )

        try:
            result = await asyncio.to_thread(ctx.gw.mutate, doc["operations"], False)
        except AdsApiError as e:
            await self.store.update(cid, {"google_request_ids": doc["google_request_ids"] + ([e.request_id] if e.request_id else [])})
            await fail(str(e), "google_error")
        except Exception as e:
            await fail(f"The mutate call failed ({e}); the outcome is unknown, check the account before retrying.", "unknown_outcome")

        try:
            after = await asyncio.to_thread(ct.after, ctx, doc["request"], result)
        except Exception as e:  # the change went through; record it even if the re-read failed
            after = {"read_error": str(e), "resource_names": result["resource_names"]}
        now = self.clock()
        await self.store.update(
            cid,
            {
                "status": "executed",
                "after": after,
                "executed_at": now,
                "executed_by": session.email,
                "mutate_result": {"resource_names": result["resource_names"]},
                "google_request_ids": doc["google_request_ids"] + list(result.get("request_ids", [])),
            },
        )
        if doc.get("undo_of"):
            await self.store.update(doc["undo_of"], {"undone_by": cid})
        return _iso(
            {
                "change_id": cid,
                "status": "executed",
                "summary": doc["summary"],
                "changed": table(doc["rows"]),
                "resource_names": result["resource_names"],
                "after": after,
                "executed_at": now,
                "google_request_ids": result.get("request_ids", []),
            }
        )

    # -- housekeeping ----------------------------------------------------------

    async def list_pending(self, session: Session, customer_id) -> list[dict]:
        ctx = self._ctx(session, customer_id)
        docs = [await self._expire_if_due(d) for d in await self.store.by_status("proposed")]
        docs = [d for d in docs if d["status"] == "proposed" and d["customer_id"] == ctx.gw.customer_id]
        docs.sort(key=lambda d: d["created_at"], reverse=True)
        return [self._proposal_view(d) for d in docs]

    async def get_change(self, session: Session, customer_id, change_id: str) -> dict:
        ctx = self._ctx(session, customer_id)
        doc = await self._expire_if_due(await self._load(ctx, change_id))
        doc["before_after"] = table(doc["rows"])
        return _iso(doc)

    async def cancel(self, session: Session, customer_id, change_id: str) -> dict:
        ctx = self._ctx(session, customer_id)
        doc = await self._expire_if_due(await self._load(ctx, change_id))
        if doc["status"] != "proposed" or not await self.store.claim(doc["change_id"], "proposed", "cancelled"):
            raise Refused(f"Change {change_id} is {doc['status']}; only pending proposals can be cancelled.")
        await self.store.update(doc["change_id"], {"cancelled_at": self.clock(), "cancelled_by": session.email})
        return {"change_id": doc["change_id"], "status": "cancelled"}

    async def change_log(self, session: Session, customer_id, days: int = 30, include_failed: bool = False) -> list[dict]:
        ctx = self._ctx(session, customer_id)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise Refused("days must be a whole number from 1 to 3650.")
        since = self.clock() - dt.timedelta(days=days)
        docs = [
            d for d in await self.store.executed_since(since)
            if d["status"] in EXECUTED and d["customer_id"] == ctx.gw.customer_id
        ]
        if include_failed:
            docs += [
                d for d in await self.store.failed_since(since)
                if d["status"] == "failed" and d["customer_id"] == ctx.gw.customer_id
            ]
        docs.sort(key=lambda d: d.get("executed_at") or d.get("failed_at"), reverse=True)
        return [
            _iso(
                {
                    "change_id": d["change_id"],
                    "type": d["type"],
                    "status": d["status"],
                    "summary": d["summary"],
                    "executed_at": d.get("executed_at"),
                    "user_email": d.get("executed_by") or d.get("failed_by") or d.get("user_email"),
                    "before_after": table(d["rows"]),
                    "before": d["before"],
                    "after": d["after"],
                    **({"undo_of": d["undo_of"]} if d.get("undo_of") else {}),
                    **({"undone_by": d["undone_by"]} if d.get("undone_by") else {}),
                    **({"reason": d["reason"], "evidence": d["evidence"]} if d.get("reason") else {}),
                    **(
                        {"failed_at": d["failed_at"], "failure_kind": d.get("failure_kind"), "error": d["error"], "live_before": d.get("live_before")}
                        if d["status"] == "failed"
                        else {}
                    ),
                }
            )
            for d in docs
        ]

    # -- emergency pause -------------------------------------------------------

    def _emergency_evidence(self, ctx: Ctx, camp: dict) -> dict:
        tz = ZoneInfo(ctx.gw.time_zone())
        now_local = self.clock().astimezone(tz)
        today = ctx.gw.today_metrics(camp["id"])
        budget = camp["budget_amount_micros"]
        cost_limit = self.g.emergency_cost_multiple * budget

        window = {
            ((now_local - dt.timedelta(hours=k)).strftime("%Y-%m-%d"), (now_local - dt.timedelta(hours=k)).hour)
            for k in range(self.g.emergency_window_hours)
        }
        start = (now_local - dt.timedelta(hours=self.g.emergency_window_hours)).strftime("%Y-%m-%d")
        hourly = ctx.gw.hourly_metrics(camp["id"], start, now_local.strftime("%Y-%m-%d"))
        rows = [h for h in hourly if (h["date"], h["hour"]) in window]
        clicks = sum(h["clicks"] for h in rows)
        impressions = sum(h["impressions"] for h in rows)
        conversions = sum(h["conversions"] for h in rows)
        ctr = clicks / impressions if impressions else 0.0

        over_budget = today["cost_micros"] > cost_limit
        invalid_traffic = clicks > self.g.emergency_min_clicks and conversions == 0 and ctr > self.g.emergency_min_ctr
        return {
            "account_time": now_local.isoformat(),
            "cost_today_idr": today["cost_micros"] // planners.MICROS,
            "daily_budget_idr": budget // planners.MICROS,
            "cost_limit_idr": int(cost_limit // planners.MICROS),
            "over_budget": over_budget,
            "window_hours": self.g.emergency_window_hours,
            "window_clicks": clicks,
            "window_impressions": impressions,
            "window_conversions": conversions,
            "window_ctr": round(ctr, 4),
            "invalid_traffic": invalid_traffic,
        }

    async def emergency_pause(self, session: Session, customer_id, campaign_id, reason: str) -> dict:
        ctx = self._ctx(session, customer_id)
        if not isinstance(reason, str) or not reason.strip():
            raise Refused("A reason is required.")
        request = {"customer_id": ctx.gw.customer_id, "campaign_id": str(campaign_id), "status": "PAUSED"}
        plan: Plan = await asyncio.to_thread(planners.plan_set_campaign_status, ctx, request)
        camp = await asyncio.to_thread(ctx.gw.campaign, request["campaign_id"])
        evidence = await asyncio.to_thread(self._emergency_evidence, ctx, camp)
        if not (evidence["over_budget"] or evidence["invalid_traffic"]):
            g = self.g
            raise Refused(
                "Emergency pause refused: live data doesn't meet either condition. "
                f"Cost today Rp{evidence['cost_today_idr']:,} vs limit Rp{evidence['cost_limit_idr']:,} "
                f"({g.emergency_cost_multiple:g} x budget); last {g.emergency_window_hours}h: "
                f"{evidence['window_clicks']} clicks (need > {g.emergency_min_clicks}), "
                f"{evidence['window_conversions']:g} conversions (need 0), CTR {evidence['window_ctr']:.1%} "
                f"(need > {g.emergency_min_ctr:.0%}). Use propose_set_campaign_status instead."
            )
        now = self.clock()
        doc = {
            "type": "set_campaign_status",
            "status": "executing",
            "customer_id": ctx.gw.customer_id,
            "request": request,
            "summary": "EMERGENCY " + plan.summary,
            "reason": reason.strip(),
            "evidence": evidence,
            "before": plan.before,
            "after": None,
            "rows": plan.rows,
            "warnings": [],
            "skipped": [],
            "operations": plan.operations,
            "validate_only_result": None,
            "created_at": now,
            "expires_at": None,
            "executed_at": None,
            "user_email": session.email,
            "google_request_ids": [],
            "error": None,
            "undo_of": None,
        }
        cid = await self._new_id(doc)
        try:
            result = await asyncio.to_thread(ctx.gw.mutate, plan.operations, False)
        except AdsApiError as e:
            await self.store.update(
                cid,
                {"status": "failed", "error": str(e), "failure_kind": "google_error", "failed_at": self.clock(), "failed_by": session.email, "google_request_ids": [e.request_id] if e.request_id else []},
            )
            raise
        after = await asyncio.to_thread(planners.after_set_campaign_status, ctx, request, result)
        await self.store.update(
            cid,
            {
                "status": "emergency_executed",
                "after": after,
                "executed_at": self.clock(),
                "mutate_result": {"resource_names": result["resource_names"]},
                "google_request_ids": list(result.get("request_ids", [])),
            },
        )
        return _iso(
            {
                "change_id": cid,
                "status": "emergency_executed",
                "changed": table(plan.rows),
                "reason": reason.strip(),
                "evidence": evidence,
                "after": after,
                "next_step": "Report this to the user now. To resume: propose_undo(change_id) then execute_change after approval.",
            }
        )
