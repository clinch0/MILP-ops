"""Independent real PCM DTMF -> production detector/arbiter -> Rust commands; no ASR."""

import argparse
import json
import sys
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime/src")]
from calls.dtmf.synth import dtmf_case
from clinic.telephony import Bridge, Context
from run_speech_commands import request


def execute(base, output_dir="debug/runs/dtmf-only", *, operator_available=True):
    folder = Path(output_dir)
    (folder / "audio").mkdir(parents=True, exist_ok=True)
    bridge = Bridge(ari=object(), client=object())
    records = []
    for codec in ("lin", "alaw", "ulaw"):
        prefix = "dtmf-" + codec + "-" + uuid.uuid4().hex
        ctx = Context(prefix, "local-media-test")

        def post(op, **args):
            result = request(base, op, args)
            ctx.listening = bool(result.get("listening"))
            return result

        def tone(call, digit, codec=codec, ctx=ctx):
            pcm, _, _ = dtmf_case(digit, 100, codec=codec, trail_ms=800)
            with wave.open(
                str(
                    folder
                    / "audio"
                    / f"{codec}-{digit.replace('*', 'star').replace('#', 'hash')}.wav"
                ),
                "wb",
            ) as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(pcm.tobytes())
            for i in range(0, len(pcm), 160):
                bridge.audio(ctx, pcm[i : i + 160].tobytes())
            detected = []
            state = None
            while not ctx.events.empty():
                op, args = ctx.events.get_nowait()
                assert op == "call.dtmf", (
                    "ASR must never be called in the tone-only test"
                )
                detected.append(args["digit"])
                if call:
                    state = post(op, call_id=call, **args)
            assert detected == [digit], (codec, digit, detected)
            records.append(
                {
                    "codec": codec,
                    "generated": digit,
                    "detected": detected,
                    "state": state.get("state") if state else None,
                }
            )
            return state

        # Also arm ASR while testing all pairs; a tone must never become speech.
        ctx.listening = True
        for digit in "123A456B789C*0#D":
            tone(None, digit)
        call = prefix + "-book"
        post("call.start", call_id=call, caller_phone="79165550000")
        for digit in "131003#23111992111":
            state = tone(call, digit)
        assert state["status"] == "booked", state
        bid = state["vars"]["booking_id"]
        original = (state["vars"]["slot_date"], state["vars"]["slot_time"])
        for action in ("confirm", "move", "cancel"):
            call = prefix + "-" + action
            post("call.start", call_id=call, flow="reminder", vars={"booking_id": bid})
            for digit in "1003#":
                tone(call, digit)
            if action == "confirm":
                state = tone(call, "1")
                assert state["status"] == "confirmed", state
            elif action == "move":
                tone(call, "2")
                state = tone(call, "1")
                if state["state"] == "reschedule.rs_ask_part":
                    tone(call, "2")
                state = tone(call, "1")
                assert state["status"] == "rescheduled", state
                assert (
                    state["vars"]["slot_date"],
                    state["vars"]["slot_time"],
                ) != original
            else:
                tone(call, "0")
                tone(call, "2")
                state = tone(call, "1")
                assert state["status"] == "cancelled", state
        # Star must interrupt identity, and OOB/inband duplicate must not repeat input.
        call = prefix + "-operator"
        post("call.start", call_id=call)
        state = tone(call, "*")
        if operator_available:
            assert state["status"] == "transfer", state
        else:
            assert state["state"] == "callback.ask_consent", state
            assert state["vars"]["operator_available"] is False, state
            assert state["transfer"] is None and not state["finished"] and not state["errors"], state
    return {
        "mode": "dtmf-only",
        "asr_inputs": 0,
        "operator_available_expected": operator_available,
        "codecs": ["lin", "alaw", "ulaw"],
        "results": {
            k: "passed"
            for k in (
                "all_16_keys",
                "book",
                "confirm",
                "reschedule",
                "cancel",
                "operator",
            )
        },
        "tones": records,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orchestrator", default="http://127.0.0.1:8080")
    parser.add_argument("--output", default="debug/runs/dtmf-only/commands.json")
    parser.add_argument("--operator-unavailable", action="store_true",
                        help="Require the exact off-hours callback route instead of immediate transfer")
    ns = parser.parse_args()
    report = execute(ns.orchestrator, Path(ns.output).parent, operator_available=not ns.operator_unavailable)
    Path(ns.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["results"], ensure_ascii=False))


if __name__ == "__main__":
    main()
