import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def stack(tmp_path_factory):
    folder = tmp_path_factory.mktemp("stack")
    port = 28180
    env = {
        **os.environ,
        "CRM_NOW": "2026-10-15T11:00:00+04:00",
        "DEMO_DATA_DIR": str(folder),
        "API_TOKEN": "",
        "STAND_URL": "",
        "SMS_GATEWAY_URL": "",
    }
    log = (folder / "stack.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "tools/dev.py", "--no-build", "--base-port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(900):
            if proc.poll() is not None:
                pytest.fail((folder / "stack.log").read_text())
            try:
                with urllib.request.urlopen(base + "/readyz", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("stack did not become ready")

        def post(op, **args):
            req = urllib.request.Request(
                f"{base}/v1/{op}",
                data=json.dumps(args).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)["payload"]

        yield post, base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


def make_booking(post, cid):
    post("call.start", call_id=cid, caller_phone="79165550000")
    state = None
    for digit in "131003#23111992111":
        state = post("call.dtmf", call_id=cid, digit=digit)
    assert state["status"] == "booked", {"state": state["state"], "vars": state["vars"]}
    return state


def test_full_rust_graph_book_move_cancel_status_and_sms(stack):
    post, _ = stack
    state = make_booking(post, "book-http")
    bid = state["vars"]["booking_id"]
    assert state["sms"] and "Мария" in state["sms"][0]["text"]
    post("call.start", call_id="move-http", flow="reminder", vars={"booking_id": bid})
    for digit in "1003#":
        post("call.dtmf", call_id="move-http", digit=digit)
    post("call.dtmf", call_id="move-http", digit="2")
    post("call.dtmf", call_id="move-http", digit="1")
    state = post("call.state", call_id="move-http")
    if state["state"].endswith("rs_ask_part"):
        post("call.dtmf", call_id="move-http", digit="2")
    state = post("call.dtmf", call_id="move-http", digit="1")
    assert state["status"] == "rescheduled", {
        "state": state["state"],
        "vars": state["vars"],
    }
    assert state["vars"]["booking_id"] == bid
    post("call.start", call_id="cancel-http", flow="reminder", vars={"booking_id": bid})
    for digit in "1003#":
        post("call.dtmf", call_id="cancel-http", digit=digit)
    post("call.dtmf", call_id="cancel-http", digit="0")
    post("call.dtmf", call_id="cancel-http", digit="2")
    state = post("call.dtmf", call_id="cancel-http", digit="1")
    assert state["status"] == "cancelled"


def test_speech_after_timeout_starts_identity_without_booking(stack):
    post, _ = stack
    post("call.start", call_id="timeout-speech")
    post("call.timeout", call_id="timeout-speech")
    state = post(
        "call.speech", call_id="timeout-speech", text="хочу записаться к терапевту"
    )
    assert state["state"] == "identity.name" and not state["finished"]
    assert not state["sms"] and not state["vars"].get("booking_id")


def test_invalid_audio_at_speech_enabled_welcome_cannot_book(stack):
    post, _=stack
    start=post("call.start",call_id="audio-dtmf")
    assert start['listening'] is True
    result=post("call.audio",call_id="audio-dtmf",pcm_b64="not audio",rate=8000)
    assert result['status']=='transfer' and not result['sms']


def test_partial_medical_hands_off_but_partial_cancel_never_writes(stack):
    post, _ = stack
    post("call.start", call_id="partial")
    state = post("call.partial", call_id="partial", text="отмените запись")
    assert not state["finished"]
    state = post("call.partial", call_id="partial", text="у меня сильная боль в груди, не могу дышать")
    assert state["status"] == "transfer"


def test_concurrent_call_correlation(stack):
    post, _ = stack

    def run(i):
        cid = f"parallel-{i}"
        post("call.start", call_id=cid)
        post("call.dtmf", call_id=cid, digit="3")
        state = post("call.dtmf", call_id=cid, digit="1")
        assert state["vars"]["call_id"] == cid
        assert state["state"] == "inbound.info_say"
        state = post("call.dtmf", call_id=cid, digit="0")
        assert state["finished"]
        return cid

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(run, range(24)))) == 24


def test_operator_access_at_identity_step_and_ticket_panel(stack):
    post, base = stack
    post("call.start", call_id="identity-operator", caller_phone="79165550000")
    post("call.dtmf", call_id="identity-operator", digit="1")
    post("call.dtmf", call_id="identity-operator", digit="1")
    state = post("call.dtmf", call_id="identity-operator", digit="*")
    assert state["status"] == "transfer"
    dialogue = state["transfer"]["ticket"]["dialogue"]
    assert any(turn["role"] == "assistant" for turn in dialogue)
    assert dialogue[-1]["text"] == state["prompts"][-1]
    with urllib.request.urlopen(base + "/operator") as response:
        assert "identity-operator" in response.read().decode()


def test_wrong_identity_never_books(stack):
    post, _ = stack
    post("call.start", call_id="wrong-birth", caller_phone="79165550000")
    for digit in "131003#01012000":
        state = post("call.dtmf", call_id="wrong-birth", digit=digit)
    assert state["status"] == "transfer"
    assert not state["sms"] and not state["vars"].get("patient_id")


def test_active_calls_reject_duplicate_ids(stack):
    post, _ = stack
    post("call.start", call_id="duplicate")
    with pytest.raises(urllib.error.HTTPError) as error:
        post("call.start", call_id="duplicate")
    assert error.value.code == 400


def test_generated_audible_phrases_are_recognized_and_execute_commands(stack):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "speech_acceptance", ROOT / "debug/run_speech_commands.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, base = stack
    report = module.execute(base, "http://127.0.0.1:28185")
    assert set(report["results"].values()) == {"passed"}
    assert all(r["pcm_rms"] > 50 for r in report["utterances"])
    (ROOT / "debug/runs/speech-only/commands.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )


def test_real_dtmf_tones_execute_without_asr(stack):
    sys.path.insert(0, str(ROOT / "debug"))
    from run_dtmf_commands import execute

    _, base = stack
    report = execute(base)
    assert report["asr_inputs"] == 0
    (ROOT / "debug/runs/dtmf-only/commands.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )


def test_answering_machine_and_wrong_person_cannot_change_booking(stack):
    sys.path.insert(0, str(ROOT / "debug"))
    from run_unverified_recipient import execute

    _, base = stack
    assert set(execute(base, "http://127.0.0.1:28185")["results"].values()) == {
        "passed"
    }


def test_combined_tones_and_real_speech(stack):
    sys.path.insert(0, str(ROOT / "debug"))
    from run_speech_commands import execute

    _, base = stack
    report = execute(base, "http://127.0.0.1:28185", mode="combined")
    assert report["dtmf_inputs"] > 0 and report["asr_inputs"] > 0
    (ROOT / "debug/runs/combined/commands.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )


def test_assisted_confirmed_booking_and_post_confirmation_finish(stack):
    post, _ = stack
    state = post("call.start", call_id="assisted-book", flow="assisted", caller_phone="79165550000")
    for digit in "31003#231119921": state = post("call.dtmf", call_id="assisted-book", digit=digit)
    assert state["state"] == "assisted.ask_request"
    state = post("call.dtmf", call_id="assisted-book", digit="1")
    assert state["state"] == "assisted.offer"
    assert state["vars"]["agent_completed"] is False and not state["sms"]
    assert state["vars"]["agent_attempt"] == 1
    state = post("call.dtmf", call_id="assisted-book", digit="1")
    assert state["state"] == "assisted.success" and state["vars"]["agent_completed"] is True
    assert state["vars"]["booking_id"] and state["sms"] and not state["errors"]
    state = post("call.speech", call_id="assisted-book", text="Спасибо")
    assert state["status"] == "booked" and state["finished"]


def test_assisted_three_declines_handoff_exact_machine_view_and_dialogue(stack):
    post, _ = stack
    post("call.start", call_id="assisted-decline", flow="assisted", caller_phone="79165550000")
    for digit in "31003#231119921": post("call.dtmf", call_id="assisted-decline", digit=digit)
    state = post("call.dtmf", call_id="assisted-decline", digit="1")
    seen = []
    for attempt in range(1, 4):
        assert state["state"] == "assisted.offer" and state["vars"]["agent_attempt"] == attempt
        assert state["vars"]["slot_id"] not in seen
        seen.append(state["vars"]["slot_id"])
        state = post("call.dtmf", call_id="assisted-decline", digit="0")
    assert state["status"] == "transfer" and not state["sms"]
    ticket = state["transfer"]["ticket"]
    view = ticket["machine_view"]
    assert view["offers_made"] == 3 and view["selected_slot_id"] == seen[-1]
    assert view["slots"] == state["vars"]["agent_seen_slots"]
    assert view["query"] == state["vars"]["agent_sql"]
    assert any(turn["role"] == "assistant" for turn in ticket["dialogue"])
    assert not state["errors"]


def test_assisted_unknown_caller_gets_first_visit_question(stack):
    post, _ = stack
    state = post("call.start", call_id="assisted-unknown", flow="assisted", caller_phone="79990009999")
    assert state["state"] == "identity.name"
    assert "Назовите фамилию" in state["prompts"][-1]
    assert not state["vars"].get("patient_id") and not state["sms"]


def test_assisted_optional_feedback_after_explicit_details_confirmation(stack):
    post, _ = stack
    call = "assisted-feedback"
    post("call.start", call_id=call, flow="assisted", caller_phone="79165550000", registrar_number="100")
    for digit in "31003#231119921": post("call.dtmf", call_id=call, digit=digit)
    post("call.dtmf", call_id=call, digit="1")
    state = post("call.dtmf", call_id=call, digit="1")
    assert state["state"] == "assisted.success"
    state = post("call.speech", call_id=call, text="Да, данные верны")
    assert state["state"] == "feedback.ask"
    assert "Что, по вашему мнению" in state["prompts"][-1]
    state = post("call.speech", call_id=call, text="Пожалуйста, говорите немного медленнее")
    assert state["finished"] and state["status"] == "booked" and state["vars"]["feedback_id"] > 0, {k:state.get(k) for k in ("state","input_field","vars","prompts")}
    assert state["vars"]["night_processing"] == "queued" and not state["errors"]


def test_spoken_historical_birth_date_is_contextual_identity_not_appointment_date(stack):
    post, _ = stack
    call = "spoken-birth"
    post("call.start", call_id=call, caller_phone="79165550000")
    post("call.dtmf", call_id=call, digit="1")
    for digit in "31003#":state=post("call.dtmf",call_id=call,digit=digit)
    assert state["state"] == "identity.birth" and state["listening"] is True
    state = post("call.speech", call_id=call, text="двадцать третьего ноября тысяча девятьсот девяносто второго года")
    assert state["state"] == "identity.readback" and not state["vars"].get("patient_id")
    state = post("call.dtmf", call_id=call, digit="1")
    assert state["state"] == "inbound.book_start" and state["vars"]["patient_id"] == 3
    assert "said_date" not in state["vars"] and not state["sms"] and not state["finished"]
