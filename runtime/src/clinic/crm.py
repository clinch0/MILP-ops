"""Single owner of the clinic mirror and the team's 10 RPM budget.

The dialogue only receives 'ok' after a definitive stand acknowledgement.
An uncertain network result quarantines its slot for manual reconciliation.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from calls.crm.module import CrmModule, slot_payload
from calls.crm.operator import OperatorTicket, build_ticket
from calls.crm.registry import MockRegistry, RegistryError
from calls.crm.remote import TokenBucket
from calls.notify.sms import render
from calls.tts.verbalize import date_to_words, time_to_words

from app.mis.client import MisClient
from app.types import MisError

CLINIC_TZ = timezone(timedelta(hours=4))


def now():
    frozen = os.environ.get("CRM_NOW")
    if frozen:
        value = datetime.fromisoformat(frozen)
        return (
            value.replace(tzinfo=CLINIC_TZ)
            if value.tzinfo is None
            else value.astimezone(CLINIC_TZ)
        )
    return datetime.now(CLINIC_TZ)


class StrictStand:
    """Reuse MILP's HTTP/error contracts, with no automatic mock fallback or POST retry."""

    def __init__(self, url: str, token: str, rpm=10):
        if not token:
            raise ValueError("STAND_TOKEN required")
        if (
            not url.startswith("https://")
            and not os.environ.get("ALLOW_LOCAL_STAND") == "1"
        ):
            raise ValueError("live stand requires HTTPS")
        self.client = MisClient(base_url=url.rstrip("/"), token=token)
        self.bucket = TokenBucket(rate=min(int(rpm), 10))
        self.lock = threading.Lock()

    def request(self, method, path, body=None, query=None):
        from .mis_contract import lookup_http
        try:
            plan = lookup_http(method, path, body, query)
        except ValueError as error:
            raise MisError("Неизвестная операция МИС", 400, "MIS_UNKNOWN_OPERATION") from error
        if plan['missing_fields']:
            raise MisError("Не хватает полей МИС: " + ', '.join(plan['missing_fields']), 400, "MIS_MISSING_FIELDS")
        if plan['invalid_fields']:
            raise MisError("Некорректные поля МИС: " + ', '.join(plan['invalid_fields']), 400, "MIS_INVALID_FIELDS")
        method, path, body, query = plan['method'], plan['path'], plan['body'], plan['query']
        if not self.lock.acquire(timeout=2):
            raise MisError("Очередь МИС занята", 429, "MIS_LOCAL_BUSY")
        try:
            if not self.bucket.acquire(timeout=5):
                raise MisError("Лимит МИС: повторите позже", 429, "MIS_LOCAL_BUSY")
            url = self.client.base_url + path
            if query:
                url += "?" + urllib.parse.urlencode(query)
            payload = (
                json.dumps(body, ensure_ascii=False).encode()
                if body is not None
                else None
            )
            status, headers, raw = self.client._send(method, url, payload, 5, path)
            if status >= 400:
                raise MisError(
                    "МИС отклонила запрос",
                    status,
                    "STAND_ERROR",
                    retry_after=self.client._retry_after(headers),
                )
            return json.loads(raw) if raw else {}
        finally:
            self.lock.release()

    def pages(self, path):
        result = []
        for offset in range(0, 100001, 50):
            page = self.request("GET", path, query={"limit": 50, "offset": offset})
            if not isinstance(page, list):
                raise TypeError("stand must return an array")
            result.extend(page)
            if len(page) < 50:
                return result
        raise ValueError("pagination limit exceeded")


EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS verified_referrals(patient_id INTEGER NOT NULL, specialty TEXT NOT NULL, valid_until TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(patient_id,specialty));
CREATE TABLE IF NOT EXISTS operations (
 key TEXT PRIMARY KEY, op TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
 args TEXT NOT NULL, result TEXT, remote_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remote_clients (patient_id INTEGER PRIMARY KEY, remote_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sms_outbox (
 id INTEGER PRIMARY KEY, key TEXT UNIQUE, phone TEXT NOT NULL, text TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', provider_id TEXT, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dial_attempts (
 id INTEGER PRIMARY KEY, phone TEXT NOT NULL, booking_id INTEGER NOT NULL, at TEXT NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS callbacks (
 call_id TEXT PRIMARY KEY, phone TEXT NOT NULL, requested_at TEXT NOT NULL,
 preferred_at TEXT, reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
"""


class ClinicCrm:
    def __init__(self, reg: MockRegistry, stand=None):
        self.reg, self.stand = reg, stand
        self.module = CrmModule(reg, today=lambda: now().date())
        from .identity_service import IdentityService
        self.identity_form = IdentityService(reg, today=lambda: now().date())
        self.mutations = threading.Lock()
        self._last_slots_sync = None
        self.reg._conn.executescript(EXTRA_SCHEMA)
        from . import booking_conflict, recovery
        self.reg._conn.executescript(booking_conflict.SCHEMA + recovery.SCHEMA)
        self.recovery_reasoner = None  # Explicit production integration only; never a CI dependency.
        self.recovery_analyses = set()
        self.recovery_analysis_lock = threading.Lock()
        self.recovery_analysis_budget = threading.BoundedSemaphore(1)
        self.doctor_bank_sync = None
        from .planner_store import SCHEMA as PLAN_SCHEMA

        self.reg._conn.executescript(PLAN_SCHEMA)
        from .queueing import WaitingQueue
        from .insights import Insights

        self.waiting = WaitingQueue(self)
        self.insights = Insights(self.reg)
        from . import hours

        hours.initialize(self.reg)
        self.hours = hours
        from .learning import Learning
        self.learning = Learning(self.reg)
        self.clinic = {
            "clinic_name": os.environ.get(
                "CLINIC_NAME", "Демонстрационная поликлиника"
            ),
            "clinic_phone": os.environ.get("CLINIC_PHONE", "+7 (000) 000-00-00"),
        }

    def handle(self, op, args):
        try:
            if op == "booking_conflict":
                from .booking_conflict import check
                if not self.mutations.acquire(timeout=2):
                    return "busy", {"error": "mutation_queue_busy", "retry_after": 2}
                try:
                    return check(self, args, now())
                finally:
                    self.mutations.release()
            if op == "recovery.route":
                from .recovery import route
                return route(self, args, now())
            if op == "booking_request":
                call_id = str(args.get("call_id") or "")
                if not call_id or args.get("consent") is not True:
                    return "invalid", {"error": "explicit_request_consent_required"}
                context = args.get("context", {})
                self.reg._x("INSERT OR IGNORE INTO booking_requests(call_id,patient_id,phone,context,created_at) VALUES(?,?,?,?,?)",
                            (call_id, args.get("patient_id"), str(args.get("phone") or ""), json.dumps(context, ensure_ascii=False), now().isoformat()))
                self.reg.record_operator_request(call_id, "booking_request_no_callback", json.dumps({"context": context, "contact_policy": "sms_after_confirmed_booking"}, ensure_ascii=False))
                return "ok", {"request_saved": True, "booking_confirmed": False, "sms_enqueued": False}
            if op == "prepare_mis":
                from .mis_contract import prepare
                operation = str(args.get('operation_id', ''))
                values = {key: value for key, value in args.items()
                          if key not in {'operation_id', 'call_id', 'request_id', 'operation_key'}}
                draft = prepare(operation)
                path_parameters = {'id': values.pop('id')} if 'id' in values else {}
                plan = prepare(operation, body=values if draft['method'] != 'GET' else None,
                               query=values if draft['method'] == 'GET' else None,
                               path_parameters=path_parameters)
                return ('ready' if plan['complete'] else 'missing' if plan['missing_fields'] else 'invalid'), plan
            if op == "identity_start": return self.identity_form.start(**args)
            if op == "identity_answer": return self.identity_form.answer(**args)
            if op == "identity_use_policy": return self.identity_form.use_policy(**args)
            if op == "identity_use_card": return self.identity_form.use_card(**args)
            if op == "control.insights":
                from .insights_access import AccessError, verify_request
                try:
                    controlled_op, controlled_args, actor = verify_request(args)
                except AccessError:
                    return "error", {"error": "control_access_denied"}
                return self.insights.handle(controlled_op, controlled_args, now(), not self.hours.is_open(self.reg, now()), actor=actor)
            if op.startswith("learning."):
                return self.learning.handle(op, args, now(), not self.hours.is_open(self.reg, now()))
            if op.startswith(("waiting.", "backfill.", "preparation.")):
                return self.waiting.handle(op, args, now())
            if op.startswith(
                ("feedback.", "insights.", "complaint.", "profile.", "nightly.")
            ):
                closed = not self.hours.is_open(self.reg, now())
                return self.insights.handle(op, args, now(), closed)
            if op == "identity.lookup":
                from .identity import IdentityLookup
                fields = {"surname", "card_no", "snils", "phone", "birth_date"}
                if set(args) - fields - {"call_id", "request_id", "operation_key", "locked_patient_id"}:
                    return "error", {"error": "invalid_identity_fields"}
                return IdentityLookup(self.reg).lookup({key: value for key, value in args.items() if key in fields}, locked_patient_id=args.get("locked_patient_id"), today=now().date())
            if op == "referral_required":
                from .specialty_dictionary import SpecialtyDictionary
                policy=SpecialtyDictionary().policy(str(args.get("specialty", "")))
                if policy is None:return "error", {"error":"unknown_specialty"}
                required=policy.get("referral_required")
                if required is None:return "error",{"error":"specialty_policy_not_configured"}
                return ("required" if required else "not_required"), {"needs_referral":required,"specialty_dative":policy['label'].lower()}
            if op == "check_referral":
                return ("ok" if self.referral_verified(args.get("patient_id"),args.get("specialty")) else "missing"), {}
            if op == "agent_slots":
                from .planner_store import slots

                # The API exposes occupied visits, not free-slot schedules. A
                # live offer requires a fresh successful occupancy reconciliation.
                with self.reg._lock:
                    if self.stand and (self._last_slots_sync is None or time.monotonic() - self._last_slots_sync > 300):
                        return "error", {"error": "availability_not_synchronized"}
                    from .specialty_dictionary import SpecialtyDictionary
                    policy=SpecialtyDictionary().policy(str(args.get('specialty','')))
                    if policy is None or policy.get('referral_required') is None:return 'error',{'error':'specialty_policy_not_configured'}
                    args={**args,"referral_verified":self.referral_verified(args.get("patient_id"),args.get("specialty"))}
                    status, result = slots(self.reg, args, now())
                    result["availability_source"] = "imported_schedule_and_synced_visits" if self.stand else "local_demo_schedule"
                    return status, result
            if op == "schedule.service":
                return "ok", {
                    "schedule": self.hours.schedule(
                        self.reg, now().date(), str(args.get("service", "registrar"))
                    ),
                    "open": self.hours.is_open(
                        self.reg, now(), str(args.get("service", "registrar"))
                    ),
                }
            if op == "meta":
                return "ok", {
                    **self.clinic,
                    "today": now().date().isoformat(),
                    "timezone": "Etc/GMT-4",
                    "stand_mode": "live" if self.stand else "demo",
                    "operator_available": os.environ.get("OPERATOR_AVAILABLE", "1")
                    == "1"
                    and self.hours.is_open(self.reg, now()),
                    "callback_hours": self.hours.callback_summary(self.reg),
                    "hours_source": "service_hours / service_calendar",
                    "doctor_bank_sync": self.doctor_bank_sync.status if self.doctor_bank_sync else "disabled",
                }
            if op == "callback_request":
                preferred = None
                if not args.get("unscheduled"):
                    if not args.get("preferred_time"):
                        return "invalid", {}
                    day = args.get("preferred_date") or now().date().isoformat()
                    try:
                        preferred = datetime.fromisoformat(
                            f"{day}T{args['preferred_time']}"
                        ).replace(tzinfo=CLINIC_TZ)
                    except (ValueError, TypeError):
                        return "invalid", {}
                    if not args.get("preferred_date") and preferred <= now():
                        preferred += timedelta(days=1)
                    if (
                        preferred <= now()
                        or preferred > now() + timedelta(days=365)
                        or not self.hours.callback_open(self.reg, preferred)
                    ):
                        return "invalid", {}
                self.reg._x(
                    "INSERT OR IGNORE INTO callbacks(call_id,phone,requested_at,preferred_at,reason) VALUES(?,?,?,?,?)",
                    (
                        str(args["call_id"]),
                        str(args.get("phone") or ""),
                        now().isoformat(),
                        preferred.isoformat() if preferred else None,
                        str(args.get("reason") or "requested"),
                    ),
                )
                context = args.get("context", {})
                variables = context.get("vars", {})
                ticket = build_ticket(
                    self.reg,
                    str(args["call_id"]),
                    "callback",
                    variables,
                    context.get("history", []),
                    [str(variables.get("callback_question", ""))],
                )
                ticket.notes.append(
                    "Обратный звонок: "
                    + (preferred.isoformat() if preferred else "уточнить время")
                )
                ticket.notes.append(
                    "Телефон: " + str(args.get("phone") or "не определён")
                )
                self.reg.record_operator_request(
                    str(args["call_id"]), "callback", ticket.to_json()
                )
                return "ok", {
                    "callback_at": (
                        date_to_words(preferred.date())
                        + ", "
                        + time_to_words(preferred.time())
                    )
                    if preferred
                    else "в рабочее время"
                }
            if op == "callback_list":
                return "ok", {
                    "requests": self.reg.execute(
                        "SELECT * FROM callbacks ORDER BY preferred_at,requested_at"
                    )
                }
            if op == "verify_card":
                patient = self.reg.patient_by_id(int(args["patient_id"]))
                supplied = str(args.get("card_no", ""))
                if patient is None or not supplied or supplied != str(patient.card_no):
                    return "mismatch", {}
                booking=self.reg.booking(int(args.get("booking_id") or 0))
                if not booking or booking.patient_id!=patient.id or booking.status!="booked":return "mismatch", {}
                self.reg._x(
                    "UPDATE dial_attempts SET outcome='verified' WHERE booking_id=? AND outcome IN ('dialing','answered')",
                    (int(args.get("booking_id", 0)),),
                )
                return "ok", {**self.module._patient_payload(patient), "booking_id":booking.id}
            if op in ("book", "cancel", "reschedule", "confirm"):
                if not self.mutations.acquire(timeout=2):
                    return "busy", {"error": "mutation_queue_busy", "retry_after": 2}
                try:
                    result = self.mutate(op, args)
                    if result[0] == "error" and "operation_state" not in result[1]:
                        self.reg._x("DELETE FROM transaction_failures WHERE call_id=?", (str(args.get("call_id") or ""),))
                    return result
                finally:
                    self.mutations.release()
            if op == "stand.status":
                rows = self.reg.execute(
                    "SELECT key,op,state,remote_id,created_at FROM operations ORDER BY created_at DESC LIMIT 100"
                )
                return "ok", {
                    "mode": "live" if self.stand else "demo",
                    "operations": rows,
                    "mirror": self.reg.sync_report(),
                }
            if op == "operator_transfer":
                vars = args.get("vars", {})
                ticket = build_ticket(
                    self.reg,
                    args.get("call_id", ""),
                    args.get("reason") or "unknown",
                    vars,
                    args.get("history", []),
                    [vars.get("last_utterance", "")],
                    dialogue=args.get("dialogue", []),
                )
                return "ok", {
                    "ticket_id": ticket.request_id,
                    "html": ticket.to_html(),
                    "ticket": json.loads(ticket.to_json()),
                }
            if op == "stand.operator_connected":
                return self.operator_connected(str(args.get('target_call_id') or args.get('call_id', '')))
            if op == "stand.operator_status":
                rows = self.reg.execute('SELECT sync_state,remote_id FROM operator_requests WHERE call_id=? ORDER BY id DESC LIMIT 1', (str(args.get('target_call_id') or args.get('call_id','')),))
                return ('ok', rows[0]) if rows else ('empty', {})
            if op == "operator_list":
                rows = self.reg.execute(
                    "SELECT payload,id FROM operator_requests ORDER BY id DESC LIMIT 30"
                )
                sections = []
                for row in rows:
                    data = json.loads(row["payload"])
                    if "highlight_sql" in data:
                        t = OperatorTicket(**data)
                        sections.append(
                            t.to_html().split("<body>", 1)[1].split("</body>", 1)[0]
                        )
                    else:
                        sections.append(
                            "<pre>"
                            + html.escape(json.dumps(data, ensure_ascii=False))
                            + "</pre>"
                        )
                return "ok", {
                    "html": "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Регистратура</title><body><h1 style='max-width:1040px;margin:20px auto;padding:0 20px'>Заявки оператору</h1><p style='max-width:1040px;margin:0 auto;padding:0 20px'><a href='/operator'>Обновить список</a></p>"
                    + "".join(sections)
                    + "</body></html>"
                }
            if op == "sms_enqueue":
                v = args.get("vars", {})
                booking = self.reg.booking(int(v.get("booking_id") or 0))
                expected = "cancelled" if args.get("template") == "cancelled" else "booked"
                synced = booking and self.reg._q("SELECT 1 FROM bookings WHERE id=? AND sync_state='synced'", (booking.id,))
                if booking is None or booking.status != expected or not synced or booking.patient_id != int(v.get("patient_id") or 0):
                    return "error", {"error": "confirmed_booking_required"}
                p = self.reg.patient_by_id(int(v.get("patient_id", 0)))
                if p is None or not p.phone:
                    return "error", {"error": "phone is missing"}
                message = render(args["template"], v, self.clinic)
                key = f"{args.get('call_id')}:{args['template']}:{v.get('booking_id')}"
                self.reg._x(
                    "INSERT OR IGNORE INTO sms_outbox(key,phone,text) VALUES(?,?,?)",
                    (key, p.phone, message),
                )
                row = self.reg._q(
                    "SELECT id,state,provider_id FROM sms_outbox WHERE key=?", (key,)
                )[0]
                return "ok", {**dict(row), "text": message, "delivery": row["state"]}
            if op == "sms_pending":
                return "ok", {
                    "messages": self.reg.execute(
                        "SELECT * FROM sms_outbox WHERE state='pending' ORDER BY id LIMIT 20"
                    )
                }
            if op == "sms_ack":
                self.reg._x(
                    "UPDATE sms_outbox SET state=?,provider_id=?,attempts=attempts+1 WHERE id=?",
                    (args["state"], args.get("provider_id"), int(args["id"])),
                )
                return "ok", {}
            if op == "snapshot":
                return "ok", {
                    "bookings": self.reg.execute(
                        "SELECT id,patient_id,slot_id,status,confirmed,remote_id,sync_state FROM bookings"
                    )
                }
            if op == "plan_lookup":
                allowed = {
                    "SELECT id, starts_at, status FROM bookings WHERE patient_id=:patient_id AND status='booked' ORDER BY starts_at",
                    "SELECT s.id, s.starts_at FROM slots s JOIN doctors d ON d.id=s.doctor_id WHERE s.status='free' AND d.active=1 AND d.specialty=:specialty ORDER BY s.starts_at LIMIT 50",
                    "SELECT 1 WHERE 0",
                }
                if args.get("sql") not in allowed:
                    return "error", {"error": "SQL template not allowed"}
                return "ok", {
                    "rows": self.reg.execute(args["sql"], args.get("params", {}))
                }
            if op == "sync_pull":
                return self.pull()
            if op == "dial_due":
                return "ok", {"items": self.dial_due()}
            if op == "dial_reserve":
                return self.dial_reserve(args)
            if op == "dial_result":
                self.reg._x(
                    "UPDATE dial_attempts SET outcome=? WHERE id=? AND outcome!='verified'",
                    (args["outcome"], int(args["attempt_id"])),
                )
                return "ok", {}
            # Optional private fields added by the bus are not DSL arguments.
            return self.module.handle(op, args)
        except (RegistryError, ValueError, TypeError, KeyError) as exc:
            return "error", {"error": type(exc).__name__}

    def referral_verified(self, patient, specialty):
        from .specialty_dictionary import SpecialtyDictionary
        policy=SpecialtyDictionary().policy(str(specialty or ""))
        if policy is None:return False
        return bool(self.reg._q("SELECT 1 FROM verified_referrals WHERE patient_id=? AND specialty=? AND valid_until>=? AND source<>''",
            (patient,policy['id'],now().date().isoformat())))

    def mutate(self, op, args):
        key = str(args.get("operation_key") or "")
        if not key:
            return "error", {"error": "operation_key required"}
        clean = {
            k: args[k]
            for k in ("patient_id", "booking_id", "slot_id")
            if args.get(k) is not None
        }
        digest = hashlib.sha256(
            json.dumps([op, clean], sort_keys=True).encode()
        ).hexdigest()
        rows = self.reg._q("SELECT * FROM operations WHERE key=?", (key,))
        if rows:
            row = rows[0]
            if row["fingerprint"] != digest:
                return "error", {
                    "error": "idempotency key reused with different arguments"
                }
            if row["state"] == "done":
                return "ok", json.loads(row["result"])
            return "error", {
                "error": "operation requires reconciliation",
                "operation_state": row["state"],
            }
        bid = int(clean.get("booking_id", 0))
        old = self.reg.booking(bid) if bid else None
        slot = (
            self.reg.slot(int(clean["slot_id"]))
            if op in ("book", "reschedule")
            else None
        )
        if op != "book" and (old is None or old.status != "booked"):
            return "error", {"error": "booking is not mutable"}
        if op in ("book", "reschedule") and slot is None:
            return "error", {"error": "slot not found"}
        if slot:
            from .availability_constraints import matches
            if not matches(args.get('availability_constraints'), slot.starts_at):
                return 'error', {'error': 'slot_violates_availability_constraints'}
            doctor = self.reg._q(
                "SELECT active FROM doctors WHERE id=?", (slot.doctor_id,)
            )[0]
            if not doctor["active"] or slot.starts_at <= now().replace(tzinfo=None):
                return "error", {"error": "doctor inactive or slot in past"}
        if old and clean.get("patient_id") is not None and int(clean["patient_id"])!=old.patient_id:
            return "error", {"error":"booking_patient_mismatch"}
        pid = int(clean.get("patient_id") or (old.patient_id if old else 0))
        p = self.reg.patient_by_id(pid)
        if p is None:
            return "error", {"error": "patient not found"}
        if not p.birth_date or not p.first_name or not p.last_name:
            return "error", {"error":"incomplete_patient_identity"}
        if slot:
            from .specialty_dictionary import SpecialtyDictionary
            policy=SpecialtyDictionary().policy(slot.specialty)
            doctors=self.reg._q("SELECT needs_referral FROM doctors WHERE id=?",(slot.doctor_id,))
            if policy is None:return "error", {"error":"unknown_specialty"}
            if policy.get('referral_required') is None:return 'error',{'error':'specialty_policy_not_configured'}
            required=policy['referral_required'] or bool(doctors[0]['needs_referral'])
            if required and not self.referral_verified(pid,slot.specialty):
                return "error", {"error":"referral_not_verified"}
            if self.stand and (self._last_slots_sync is None or time.monotonic()-self._last_slots_sync>300):
                return "error", {"error":"availability_not_synchronized"}
        if op == "book":
            from .specialty_dictionary import canonical_specialty
            # A lost acknowledgement must never become a second visit under a
            # new operation key, even when a new free slot was selected.
            for pending in self.reg._q("SELECT key,args FROM operations WHERE op IN ('book','reschedule') AND state IN ('pending','uncertain')"):
                previous = json.loads(pending["args"])
                previous_slot = self.reg.slot(int(previous.get("slot_id") or 0))
                if int(previous.get("patient_id") or 0) == pid and previous_slot and canonical_specialty(previous_slot.specialty) == canonical_specialty(slot.specialty):
                    self.reg._x("INSERT OR REPLACE INTO transaction_failures VALUES(?,?,?,?,?)",
                                (str(args.get("call_id") or ""), pending["key"], "uncertain", "RECONCILIATION_REQUIRED", now().isoformat()))
                    return "error", {"error": "operation requires reconciliation", "operation_state": "uncertain", "error_kind": "mis_transaction"}
            from .booking_conflict import check
            status, conflict = check(self, {"patient_id": pid, "specialty": slot.specialty, "call_id": args.get("call_id")}, now(), allow_consent=True)
            if status == "conflict":
                return status, conflict
        # Persist intent and reserve the target BEFORE contacting the remote API.
        with self.reg._lock:
            conn = self.reg._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                if slot:
                    cur = conn.execute(
                        "UPDATE slots SET status='reserved' WHERE id=? AND status='free'",
                        (slot.id,),
                    )
                    if cur.rowcount != 1:
                        conn.execute("ROLLBACK")
                        return "taken", {}
                conn.execute(
                    "INSERT INTO operations(key,op,fingerprint,state,args,created_at) VALUES(?,?,?,'pending',?,?)",
                    (key, op, digest, json.dumps(clean), now().isoformat()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        remote = None
        visit_mutation_started = False
        try:
            if self.stand and op != "confirm":
                if op == "book":
                    mapping = self.reg._q(
                        "SELECT remote_id FROM remote_clients WHERE patient_id=?",
                        (pid,),
                    )
                    if mapping:
                        remote_pid = int(mapping[0]["remote_id"])
                    else:
                        if p.birth_date is None:
                            raise ValueError(
                                "patient birth_date missing in local export"
                            )
                        # The stand explicitly makes creation idempotent by name+birth date.
                        client = self.stand.request(
                            "POST",
                            "/clients",
                            {
                                "last_name": p.last_name,
                                "first_name": p.first_name,
                                "middle_name": p.middle_name or None,
                                "birth_date": p.birth_date.strftime("%d.%m.%Y"),
                            },
                        )
                        remote_pid = int(client["id"])
                        self.reg._x(
                            "INSERT INTO remote_clients VALUES(?,?)", (pid, remote_pid)
                        )
                    visit_mutation_started = True
                    remote = self.stand.request(
                        "POST",
                        "/visits",
                        {
                            "client_id": remote_pid,
                            "doctor_id": slot.doctor_id,
                            "scheduled_at": slot.starts_at.strftime("%d.%m.%Y %H:%M"),
                        },
                    )
                else:
                    remote_id = self.reg.remote_id_of(bid)
                    if remote_id is None:
                        raise ValueError(
                            "stand id missing; import/sync the booking first"
                        )
                    body = (
                        {"status": "Отменена"}
                        if op == "cancel"
                        else {
                            "doctor_id": slot.doctor_id,
                            "scheduled_at": slot.starts_at.strftime("%d.%m.%Y %H:%M"),
                        }
                    )
                    visit_mutation_started = True
                    remote = self.stand.request(
                        "PATCH", f"/visits/{int(remote_id)}", body
                    )
                if not isinstance(remote, dict) or not isinstance(
                    remote.get("id"), int
                ):
                    raise ValueError("invalid acknowledgement")
                if op == "cancel":
                    if remote.get("status") != "Отменена":
                        raise ValueError("cancellation not acknowledged")
                elif (
                    remote.get("status") != "Запланирована"
                    or remote.get("scheduled_at")
                    != slot.starts_at.strftime("%d.%m.%Y %H:%M")
                    or remote.get("doctor", {}).get("id") != slot.doctor_id
                ):
                    raise ValueError("stand acknowledged another appointment")
            with self.reg._lock:
                conn = self.reg._conn
                conn.execute("BEGIN IMMEDIATE")
                try:
                    stamp = now().replace(tzinfo=None).isoformat(timespec="minutes")
                    if op == "book":
                        cur = conn.execute(
                            "INSERT INTO bookings(patient_id,slot_id,doctor_id,starts_at,status,remote_id,sync_state,created_at,updated_at) VALUES(?,?,?,?,'booked',?,'synced',?,?)",
                            (
                                pid,
                                slot.id,
                                slot.doctor_id,
                                slot.starts_at.isoformat(timespec="minutes"),
                                str(remote["id"]) if remote else None,
                                stamp,
                                stamp,
                            ),
                        )
                        bid = cur.lastrowid
                    elif op == "cancel":
                        conn.execute(
                            "UPDATE bookings SET status='cancelled',sync_state='synced',updated_at=? WHERE id=?",
                            (stamp, bid),
                        )
                        conn.execute(
                            "UPDATE slots SET status='free',booking_id=NULL WHERE id=?",
                            (old.slot_id,),
                        )
                    elif op == "reschedule":
                        # Preserve the original stand and local booking identifiers.
                        conn.execute(
                            "UPDATE bookings SET slot_id=?,doctor_id=?,starts_at=?,confirmed=0,sync_state='synced',updated_at=? WHERE id=?",
                            (
                                slot.id,
                                slot.doctor_id,
                                slot.starts_at.isoformat(timespec="minutes"),
                                stamp,
                                bid,
                            ),
                        )
                        conn.execute(
                            "UPDATE slots SET status='free',booking_id=NULL WHERE id=?",
                            (old.slot_id,),
                        )
                    else:
                        conn.execute(
                            "UPDATE bookings SET confirmed=1,updated_at=? WHERE id=?",
                            (stamp, bid),
                        )
                    if slot:
                        conn.execute(
                            "UPDATE slots SET status='booked',booking_id=? WHERE id=?",
                            (bid, slot.id),
                        )
                    result = {
                        "booking_id": bid,
                        "confirmed": op == "confirm",
                        "booking_status": "cancelled" if op == "cancel" else "booked",
                        "stand_acknowledged": bool(remote),
                        "confirmation_scope": "local"
                        if op == "confirm"
                        else "stand"
                        if self.stand
                        else "demo",
                    }
                    if slot:
                        result.update(slot_payload(slot))
                    conn.execute(
                        "UPDATE operations SET state='done',result=?,remote_id=? WHERE key=?",
                        (
                            json.dumps(result, ensure_ascii=False),
                            int(remote["id"]) if remote else None,
                            key,
                        ),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            return "ok", result
        except Exception as exc:  # noqa: BLE001 -- every failure after a remote write must quarantine its slot
            # A timeout/5xx after a write may mean it was committed remotely.
            definite = isinstance(exc, (ValueError, KeyError)) or (
                isinstance(exc, MisError) and 400 <= exc.status < 500
            )
            uncertain = visit_mutation_started and (not definite or remote is not None)
            state = "uncertain" if uncertain else "failed"
            self.reg._x("UPDATE operations SET state=? WHERE key=?", (state, key))
            if slot and not uncertain:
                self.reg._x(
                    "UPDATE slots SET status='free' WHERE id=? AND status='reserved'",
                    (slot.id,),
                )
            self.reg.record_operator_request(
                args.get("call_id", ""),
                "stand_reconciliation",
                json.dumps({"operation_key": key, "state": state}),
            )
            # Only a runtime remote transaction failure enters this route. Form
            # validation and CI failures never insert a transaction failure row.
            local_busy = getattr(exc, "code", "") == "MIS_LOCAL_BUSY"
            form_error = getattr(exc, "code", "") in {"MIS_MISSING_FIELDS", "MIS_INVALID_FIELDS", "MIS_UNKNOWN_OPERATION"}
            if self.stand and op != "confirm" and not form_error and not local_busy:
                self.reg._x("INSERT OR REPLACE INTO transaction_failures VALUES(?,?,?,?,?)",
                            (str(args.get("call_id") or ""), key, state, str(getattr(exc, "code", type(exc).__name__)), now().isoformat()))
            if local_busy:
                return "busy", {"error": "mis_budget_busy", "operation_state": state, "retry_after": 5}
            return "error", {
                "error": "МИС не подтвердила изменение; передаю оператору",
                "operation_state": state,
                "error_kind": "validation" if form_error else "mis_transaction" if self.stand else "local_transaction",
            }

    def pull(self):
        if not self.stand:
            return "error", {"error": "stand not configured"}
        self._last_slots_sync = None
        # Cache all pages once; use explicit local/remote patient mapping.
        doctors = self.stand.pages("/doctors")
        from .specialty_dictionary import canonical_specialty
        for d in doctors:
            self.reg._x(
                "INSERT INTO doctors(id,last_name,full_name,specialty,active) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name,specialty=excluded.specialty,active=excluded.active",
                (
                    d["id"],
                    d["full_name"].split()[0],
                    d["full_name"],
                    canonical_specialty(d["specialty"]),
                    int(d["is_active"]),
                ),
            )
        visits = self.stand.pages("/visits")
        with self.mutations, self.reg._lock:
            conn = self.reg._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                for visit in visits:
                    self.ingest_visit(visit)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._last_slots_sync = time.monotonic()
        bank_status = "disabled"
        if os.environ.get("TTS_DOCTOR_BANK_SYNC") == "1":
            if self.doctor_bank_sync is None:
                from .doctor_bank_sync import DoctorBankSync
                from .admin_rpc import module_call
                self.doctor_bank_sync = DoctorBankSync(module_call)
            bank_status = self.doctor_bank_sync.enqueue(doctors)
        return "ok", {"visits": len(visits), "doctors": len(doctors), "doctor_bank_sync": bank_status}

    def refresh_availability(self):
        """Read-only API reconciliation shares the single CRM rate limiter."""
        while True:
            try:
                status, _ = self.pull()
                if status != "ok":
                    self._last_slots_sync = None
            except (OSError, ValueError, TypeError, KeyError, MisError, RegistryError):
                self._last_slots_sync = None
            time.sleep(120)

    def ingest_visit(self, v):
        """Unknown patients still occupy time; manual moves preserve the original booking id."""
        b = self.reg.booking_by_remote_id(str(v["id"]))
        did = int(v["doctor"]["id"])
        starts = datetime.strptime(v["scheduled_at"], "%d.%m.%Y %H:%M")
        ts = starts.isoformat(timespec="minutes")
        if v["status"] != "Запланирована":
            if b and b.status == "booked":
                self.reg._x(
                    "UPDATE bookings SET status=?,sync_state='synced' WHERE id=?",
                    ("cancelled" if v["status"] == "Отменена" else "completed", b.id),
                )
                self.reg._x(
                    "UPDATE slots SET status='free',booking_id=NULL WHERE id=? AND booking_id=?",
                    (b.slot_id, b.id),
                )
            else:
                self.reg._x(
                    "UPDATE slots SET status='free' WHERE doctor_id=? AND starts_at=? AND status='external'",
                    (did, ts),
                )
            return
        self.reg._x(
            "INSERT OR IGNORE INTO slots(doctor_id,starts_at,status) VALUES(?,?,'free')",
            (did, ts),
        )
        target = self.reg._q(
            "SELECT * FROM slots WHERE doctor_id=? AND starts_at=?", (did, ts)
        )[0]
        if target["status"] not in ("free", "external") and (
            b is None or target["booking_id"] != b.id
        ):
            self.reg._x("UPDATE slots SET status='blocked' WHERE id=?", (target["id"],))
            self.reg.record_operator_request(
                "sync",
                "slot_conflict",
                json.dumps({"remote_id": v["id"], "slot_id": target["id"]}),
            )
            return
        maps = self.reg._q(
            "SELECT patient_id FROM remote_clients WHERE remote_id=?", (v["client_id"],)
        )
        if not maps:
            self.reg._x(
                "UPDATE slots SET status='external' WHERE id=?", (target["id"],)
            )
            return
        pid = int(maps[0]["patient_id"])
        if b is None:
            self.reg.import_booking(str(v["id"]), pid, did, starts)
        elif b.patient_id != pid or b.status != "booked":
            self.reg._x("UPDATE slots SET status='blocked' WHERE id=?", (target["id"],))
            self.reg.record_operator_request(
                "sync",
                "identity_or_status_conflict",
                json.dumps({"remote_id": v["id"]}),
            )
        elif b.slot_id != target["id"]:
            self.reg._x(
                "UPDATE slots SET status='free',booking_id=NULL WHERE id=? AND booking_id=?",
                (b.slot_id, b.id),
            )
            self.reg._x(
                "UPDATE bookings SET slot_id=?,doctor_id=?,starts_at=?,confirmed=0,sync_state='synced' WHERE id=?",
                (target["id"], did, ts, b.id),
            )
            self.reg._x(
                "UPDATE slots SET status='booked',booking_id=? WHERE id=?",
                (b.id, target["id"]),
            )

    def operator_connected(self, call_id):
        """Called by trusted ARI only after the operator answered and was bridged.

        Escalation POST is not idempotent in DBI: a lost acknowledgement remains
        uncertain for reconciliation, never automatically submitted twice.
        """
        with self.mutations:
            rows = self.reg.execute('SELECT id,payload,sync_state,remote_id FROM operator_requests WHERE call_id=? ORDER BY id DESC LIMIT 1', (call_id,))
            if not rows:
                return 'empty', {'reason':'no_transfer_ticket'}
            row = rows[0]
            if row['sync_state'] in {'synced','sending','uncertain','failed'}:
                return 'ok', {'state':row['sync_state'], 'remote_id':row['remote_id']}
            ticket = json.loads(row['payload'])
            patient = self.reg.patient_by_id(ticket.get('patient_id')) if ticket.get('patient_id') else None
            if not patient or not patient.birth_date:
                return 'ok', {'state':'local_only','reason':'identity_not_verified'}
            if not self.stand:
                return 'ok', {'state':'local_only','reason':'no_stand'}
            mapping = self.reg.execute('SELECT remote_id FROM remote_clients WHERE patient_id=?', (patient.id,))
            # A client is created only for an already verified local identity.
            if not mapping:
                client = self.stand.request('POST','/clients',{'last_name':patient.last_name,'first_name':patient.first_name,'middle_name':patient.middle_name or None,'birth_date':patient.birth_date.strftime('%d.%m.%Y')})
                remote_pid = int(client['id'])
                self.reg._x('INSERT INTO remote_clients VALUES(?,?)',(patient.id,remote_pid))
            else:
                remote_pid = int(mapping[0]['remote_id'])
            text = 'Разговор передан ответившему регистратору. Причина: '+str(ticket.get('reason','unknown'))+'. '+ ' '.join(str(x) for x in ticket.get('utterances',[])[-2:])
            text = ''.join(c for c in text if c in '\n\t' or (ord(c)>=32 and not 127<=ord(c)<=159))[:1000]
            self.reg.mark_operator_synced(row['id'],state='sending')
            try:
                result = self.stand.request('POST','/escalations',{'client_id':remote_pid,'description':text})
                if type(result.get('id')) is not int or result.get('client_id') != remote_pid:
                    raise ValueError('invalid escalation acknowledgement')
                self.reg.mark_operator_synced(row['id'],result['id'])
                return 'ok', {'state':'synced','remote_id':result['id'],'stand_acknowledged':True}
            except (MisError, OSError, ValueError, KeyError):
                self.reg.mark_operator_synced(row['id'],state='uncertain')
                return 'error', {'state':'uncertain','reconciliation_required':True}

    def dial_due(self):
        from .outreach import due_reminders

        return due_reminders(self, now())["items"]

    def dial_reserve(self, args):
        # One process owns the persistent, per-PHONE rolling budget, shared across bookings.
        with self.mutations:
            at = now()
            if not 10 <= at.hour < 22:
                return "blocked", {"reason": "outside 10:00–22:00 GMT+4"}
            bid = int(args["booking_id"])
            b = self.reg.booking(bid)
            if (
                b is None
                or b.status != "booked"
                or b.confirmed
            ):
                return "blocked", {"reason": "booking not due"}
            from .outreach import reminder_due

            decision = reminder_due(self, b, at)
            if not decision["due"]:
                return "blocked", {"reason": decision["reason"], "reminder": decision}
            p = self.reg.patient_by_id(b.patient_id)
            if not p or not p.phone:
                return "blocked", {"reason": "phone missing"}
            rows = self.reg._q(
                "SELECT at,outcome,booking_id FROM dial_attempts WHERE phone=? AND at>? ORDER BY at",
                (p.phone, (at - timedelta(hours=24)).isoformat()),
            )
            if len(rows) >= 6 or any(
                r["outcome"] == "dialing"
                or (r["outcome"] == "verified" and r["booking_id"] == bid)
                for r in rows
            ):
                return "blocked", {
                    "reason": "daily budget or contact already active/completed"
                }
            # 3 occasions of 2 calls; second attempt >=20 min, next occasion >=3h.
            if rows:
                gap = at - datetime.fromisoformat(rows[-1]["at"])
                required = (
                    timedelta(minutes=20) if len(rows) % 2 else timedelta(hours=3)
                )
                if gap < required:
                    return "blocked", {"reason": "retry gap"}
            attempt = self.reg._x(
                "INSERT INTO dial_attempts(phone,booking_id,at,outcome) VALUES(?,?,?,'dialing')",
                (p.phone, bid, at.isoformat()),
            )
            return "ok", {"attempt_id": attempt, "phone": p.phone, "booking_id": bid}
