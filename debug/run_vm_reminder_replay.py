"""Real PCM/ASR reminder reschedule acceptance; no SIP origination or dial-budget bypass.

Creates and cancels only its own synthetic DBI visit. Run on the Docker network
with the same environment/mounts as run_vm_live.py. Raw evidence stays private.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from run_vm_live import post, Bridge, Context


def main():
    folder = Path(__file__).resolve().parent / 'runs/reminder-reschedule-replay'
    folder.mkdir(parents=True, exist_ok=True)
    api, crm = 'http://orchestrator:8080', 'http://crm:8080'
    call_id = 'reminder-replay-' + uuid.uuid4().hex[:12]
    bridge, context = Bridge(ari=object(), client=object()), Context(call_id, 'acceptance')
    result = {'passed': False, 'scope': 'real PCM / production VAD / organizer Whisper / NLU / Rust / DBI; no SIP call'}
    booking = None
    events = []
    try:
        tomorrow = (datetime.now(timezone(timedelta(hours=4))) + timedelta(days=1)).date().isoformat()
        slot = post(crm, 'find_slots', {'specialty': 'терапевт', 'said_date': tomorrow})
        booking = post(crm, 'book', {'patient_id': 2, 'slot_id': slot['slot_id'], 'operation_key': call_id})
        assert booking['stand_acknowledged'] is True
        state = post(api, 'call.start', {'call_id': call_id, 'flow': 'reminder', 'registrar_number': '114', 'vars': {'booking_id': booking['booking_id']}})
        for text in ['Один ноль ноль два.', 'Перенесите запись.', 'Да, подходит.', 'Утром.', 'Да, подходит.']:
            if state.get('finished'):
                break
            # A single available day part skips its question.
            if text == 'Утром.' and state['state'] == 'reschedule.rs_offer_time':
                continue
            clip = post('http://tts:8080', 'synth', {'text': text, 'rate': 1.3})
            context.listening = bool(state['listening'])
            assert context.listening
            bridge.audio(context, base64.b64decode(clip['pcm_b64']) + bytes(16000))
            operation, args = context.events.get_nowait()
            assert operation == 'call.audio' and context.events.empty()
            state = post(api, operation, {'call_id': call_id, **args})
            events.append(state)
            print(json.dumps({'state': state['state'], 'status': state['status']}, ensure_ascii=False), flush=True)
        assert state['status'] == 'rescheduled', state
        assert state['vars']['stand_acknowledged'] is True
        result['passed'] = True
    finally:
        post(api, 'call.hangup', {'call_id': call_id})
        if booking:
            result['cleanup'] = post(crm, 'cancel', {'booking_id': booking['booking_id'], 'patient_id': 2, 'operation_key': call_id + '-cleanup'})
        (folder / 'events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2))
        (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({'passed': result['passed'], 'cancel_acknowledged': result.get('cleanup', {}).get('stand_acknowledged')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
