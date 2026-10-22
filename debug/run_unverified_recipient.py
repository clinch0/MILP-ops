"""Spoken answering-machine and wrong-person replies must not change an appointment."""

import argparse
import base64
import json
import uuid
import wave
from pathlib import Path

from run_speech_commands import request


def execute(base, tts_url, folder="debug/runs/answering-machine"):
    folder = Path(folder)
    (folder / "audio").mkdir(parents=True, exist_ok=True)
    prefix = "recipient-" + uuid.uuid4().hex
    request(base, "call.start", {"call_id": prefix, "caller_phone": "79165550000"})
    for digit in "131003#23111992111":
        state = request(base, "call.dtmf", {"call_id": prefix, "digit": digit})
    assert state["status"] == "booked", state
    bid = state["vars"]["booking_id"]
    records = []
    # Explicit fixture pronunciation is required for words outside the clinic lexicon.
    for i, phrase in enumerate(
        [
            "Здравствуйте. Я вирту+альный помощник Т Б+анка. Ост+авьте сообщ+ение после сигн+ала.",
            "Да, подтверждаю.",
            "Да, отмените.",
            "Нет, вы ош+иблись н+омером.",
        ]
    ):
        call = f"{prefix}-{i}"
        state = request(
            base,
            "call.start",
            {"call_id": call, "flow": "reminder", "vars": {"booking_id": bid}},
        )
        assert all(
            "Мария" not in p
            and "кабинет" not in p.lower()
            and "записан" not in p.lower()
            for p in state["prompts"]
        ), state["prompts"]
        clip = request(
            tts_url,
            "speech.synth" if tts_url.rstrip("/") == base.rstrip("/") else "synth",
            {"text": phrase, "rate": 1.5},
        )
        assert clip["engine"].startswith("silero")
        path = folder / "audio" / f"{i + 1:02d}-unverified.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(clip["sample_rate"])
            wav.writeframes(base64.b64decode(clip["pcm_b64"]))
        state = request(
            base,
            "call.audio",
            {"call_id": call, "pcm_b64": clip["pcm_b64"], "rate": clip["sample_rate"]},
        )
        assert (
            state["status"] not in ("confirmed", "cancelled", "rescheduled")
            and not state["sms"]
        ), state
        assert all(
            "Мария" not in p and "кабинет" not in p.lower() for p in state["prompts"]
        ), state["prompts"]
        records.append(
            {
                "source": phrase.replace("+", ""),
                "state": state["state"],
                "status": state["status"],
                "intent": state["vars"].get("last_intent"),
                "wav": str(path),
            }
        )
    # A real authenticated patient can still cancel the untouched appointment.
    call = prefix + "-cleanup"
    request(
        base,
        "call.start",
        {"call_id": call, "flow": "reminder", "vars": {"booking_id": bid}},
    )
    for digit in "1003#021":
        state = request(base, "call.dtmf", {"call_id": call, "digit": digit})
    assert state["status"] == "cancelled", state
    report = {
        "mode": "real-audio-unverified-recipient",
        "results": {
            "no_details_before_identity": "passed",
            "no_unverified_mutation": "passed",
            "no_unverified_sms": "passed",
            "authenticated_control": "passed",
        },
        "utterances": records,
    }
    (folder / "commands.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--orchestrator", default="http://127.0.0.1:28180")
    p.add_argument("--tts-url", default="http://127.0.0.1:28185")
    ns = p.parse_args()
    print(
        json.dumps(execute(ns.orchestrator, ns.tts_url), ensure_ascii=False, indent=2)
    )
