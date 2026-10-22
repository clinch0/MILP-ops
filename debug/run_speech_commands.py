"""Mandatory acceptance: audible generated Russian -> ASR audio endpoint -> real Rust command.

No text is supplied to ASR, no transcript injection, no silent backend.
"""

import argparse
import base64
import json
import math
import os
import time
import urllib.request
import uuid
import wave
from array import array
from pathlib import Path


def request(base, op, args):
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/" + op,
        data=json.dumps(args).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ.get("API_TOKEN", ""),
        },
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        result = json.load(response)
    if result.get("status") != "ok":
        raise AssertionError(result)
    return result["payload"]


def execute(base, tts_url, rate=1.5, mode="speech-only", output_dir=None):
    prefix = "voice-" + uuid.uuid4().hex
    records = []
    folder = Path(output_dir or f"debug/runs/{mode}")
    (folder / "audio").mkdir(parents=True, exist_ok=True)

    def post(op, **args):
        return request(base, op, args)

    def say(call_id, phrase):
        start = time.perf_counter()
        clip = request(
            tts_url,
            "speech.synth" if tts_url.rstrip("/") == base.rstrip("/") else "synth",
            {"text": phrase, "rate": rate},
        )
        assert clip.get("engine", "").startswith("silero"), clip.get("engine")
        pcm = base64.b64decode(clip["pcm_b64"], validate=True)
        samples = array("h")
        samples.frombytes(pcm)
        rms = math.sqrt(sum(x * x for x in samples) / max(len(samples), 1))
        assert len(samples) > 1600 and rms > 50, "TTS returned empty or silent audio"
        path = (
            folder
            / "audio"
            / f"{len(records) + 1:02d}-{call_id.rsplit('-', 1)[-1]}.wav"
        )
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(clip["sample_rate"])
            wav.writeframes(pcm)
        # The entire end-to-end path sees only PCM, not the expected phrase/transcript.
        state = post(
            "call.audio",
            call_id=call_id,
            pcm_b64=clip["pcm_b64"],
            rate=clip["sample_rate"],
        )
        assert "ignored" not in state, state
        records.append(
            {
                "source": phrase,
                "wav": str(path),
                "pcm_rms": round(rms, 2),
                "audio_s": len(samples) / clip["sample_rate"],
                "elapsed_s": round(time.perf_counter() - start, 3),
                "state": state["state"],
                "status": state["status"],
                "recognized_intent": state.get("vars", {}).get("last_intent"),
            }
        )
        return state

    call = prefix + "-book"
    post(
        "call.start",
        call_id=call,
        caller_phone="79165550000",
        flow="voice" if mode == "speech-only" else "inbound",
    )
    if mode == "speech-only":
        state = say(call, "Петрова Мария Сергеевна.")
        assert state["state"]=="identity.birth",state
        state = say(call, "Двадцать третье ноября тысяча девятьсот девяносто второго года.")
        assert state["state"] == "identity.readback" and not state["vars"].get("patient_id")
        state = say(call, "Да, данные верны.")
        if state['state']=='identity.readback':
            # A one-letter ASR discrepancy requires a second, database-backed
            # readback; it must not silently verify the approximate identity.
            assert not state['vars'].get('patient_id') and state['vars']['identity_readback_confirmed'], state
            state = say(call, "Да, верно.")
    else:
        for digit in "131003#231119921":
            state = post("call.dtmf", call_id=call, digit=digit)
    assert state["state"] == "inbound.book_start", state
    state = say(call, "Запишите меня к терапевту.")
    assert state["state"] == "inbound.book_offer", state["state"]
    state = say(call, "Да, подходит.")
    assert state["status"] == "booked", state
    assert state["vars"]["specialty"] == "терапевт"
    assert state["sms"] and "Мария" in state["sms"][0]["text"], state.get("errors")
    bid = state["vars"]["booking_id"]
    original = (state["vars"]["slot_date"], state["vars"]["slot_time"])

    call = prefix + "-confirm"
    post("call.start", call_id=call, flow="reminder", vars={"booking_id": bid})
    say(call, "Один ноль ноль три.")
    state = say(call, "Подтверждаю, я приду на приём.")
    assert state["status"] == "confirmed", state

    call = prefix + "-move"
    post("call.start", call_id=call, flow="reminder", vars={"booking_id": bid})
    say(call, "Один ноль ноль три.")
    state = say(call, "Перенесите мою запись.")
    assert state["state"] == "reschedule.rs_offer_day", state["state"]
    state = say(call, "Да, этот день подходит.")
    if state["state"] == "reschedule.rs_ask_part":
        state = say(call, "После обеда.")
    assert state["state"] == "reschedule.rs_offer_time", state["state"]
    state = say(call, "Да, подходит.")
    assert state["status"] == "rescheduled", state
    assert state["vars"]["booking_id"] == bid
    moved = (state["vars"]["slot_date"], state["vars"]["slot_time"])
    assert moved != original, (original, moved)

    call = prefix + "-cancel"
    post("call.start", call_id=call, flow="reminder", vars={"booking_id": bid})
    say(call, "Один ноль ноль три.")
    state = say(call, "Отмените мою запись.")
    assert state["state"] == "reminder.cancel_offer", state["state"]
    state = say(call, "Нет, отмените пожалуйста.")
    assert state["state"]=="reminder.cancel_confirm",state
    state = say(call, "Да, отмените.")
    assert state["status"] == "cancelled" and state["sms"], state

    report = {
        "mode": mode,
        "dtmf_inputs": 0 if mode == "speech-only" else len("131003#231119921"),
        "asr_inputs": len(records),
        "rate": rate,
        "booking_id": bid,
        "results": {
            "book": "passed",
            "confirm": "passed",
            "reschedule": "passed",
            "cancel": "passed",
            "sms_with_name": "passed",
        },
        "utterances": records,
    }
    return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--orchestrator", default="http://127.0.0.1:8080")
    p.add_argument("--tts-url", default="")
    p.add_argument("--rate", type=float, default=1.5)
    p.add_argument("--mode", choices=["speech-only", "combined"], default="speech-only")
    p.add_argument("--output", default="")
    ns = p.parse_args()
    output = Path(ns.output or f"debug/runs/{ns.mode}/commands.json")
    report = execute(
        ns.orchestrator, ns.tts_url or ns.orchestrator, ns.rate, ns.mode, output.parent
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
