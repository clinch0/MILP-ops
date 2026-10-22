"""Originate a real SIP call; StasisStart creates the reminder session only after answer."""

import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from calls.telephony.ari_bridge import Ari

from .modules import CrmProxy


def run():
    if os.environ.get("OUTBOUND_ENABLED") != "1":
        print("Outbound disabled (OUTBOUND_ENABLED=1 to enable)", flush=True)
        return
    if os.environ.get("ANTI_FRAUD_PUBLISHED") != "1":
        raise ValueError(
            "Publish the clinic phone and anti-fraud notice before enabling outbound"
        )
    crm = CrmProxy()
    ari = Ari(
        os.environ.get("ARI_URL", "http://127.0.0.1:8088"),
        os.environ.get("ARI_USER", "calls"),
        os.environ["ARI_PASSWORD"],
    )
    endpoint = os.environ["SIP_OUTBOUND_ENDPOINT"]  # e.g. PJSIP/{phone}@clinic-trunk
    caller = os.environ["CLINIC_PHONE"]
    allowed = {p.strip() for p in os.environ.get('OUTBOUND_ALLOWED_PHONES','').split(',') if p.strip()}
    if not allowed:
        raise ValueError('Set the explicit consented OUTBOUND_ALLOWED_PHONES before dialing')

    def dial(item):
        attempt = None
        try:
            if item.get('phone') not in allowed:
                return
            status, attempt = crm.call("dial_reserve", item)
            if status != "ok":
                return
            if attempt['phone'] not in allowed or not re.fullmatch(r'[+a-zA-Z0-9_-]{1,32}',attempt['phone']):
                raise ValueError('Reserved destination changed or is not a safe SIP user')
            channel_id = str(uuid.uuid4())
            channel = ari._req(
                "POST",
                "/channels",
                endpoint=endpoint.format(phone=attempt["phone"]),
                app="calls",
                appArgs=f"reminder,{attempt['phone']},{item['booking_id']}",
                callerId=caller,
                timeout=40,
                channelId=channel_id,
            )
            # Asterisk reports Up only after the patient answers. Never label an originate as an answer.
            answered = False
            for _ in range(45):
                try:
                    state = ari._req("GET", f"/channels/{channel['id']}")
                    if state.get("state") == "Up":
                        answered = True
                        break
                except OSError:
                    break
                time.sleep(1)
            crm.call(
                "dial_result",
                {
                    "attempt_id": attempt["attempt_id"],
                    "outcome": "answered" if answered else "no_answer",
                },
            )
        except (OSError, ValueError, KeyError):
            if attempt and attempt.get("attempt_id"):
                crm.call(
                    "dial_result",
                    {"attempt_id": attempt["attempt_id"], "outcome": "failed"},
                )

    with ThreadPoolExecutor(max_workers=4) as pool:
        while True:
            _, result = crm.call("dial_due", {})
            list(pool.map(dial, result.get("items", [])))
            if os.environ.get('OUTBOUND_ONCE') == '1':
                return
            time.sleep(30)
