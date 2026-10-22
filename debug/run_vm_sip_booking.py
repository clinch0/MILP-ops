"""Real SIP speech -> production ASR/dialogue -> DBI, synthetic patient only.

State polling observes progress; all patient answers enter through G.711 RTP.
No call.start/call.speech/call.dtmf injection. Cancels only the new test visit.
Requires the organizer check.py and phone credentials mounted read-only.
"""
import audioop
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
import urllib.request
import wave

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / 'debug/runs/sip-booking-whisper'
API = 'http://127.0.0.1:8080'
CRM = os.environ['CRM_URL']


def post(base, op, args):
    headers = {'Content-Type':'application/json'}
    if base == API:
        headers['Authorization'] = 'Bearer ' + os.environ['API_TOKEN']
    request = urllib.request.Request(base+'/v1/'+op, data=json.dumps(args).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=100) as response:
        doc = json.load(response)
    if doc.get('status') != 'ok':
        raise RuntimeError({'operation':op, 'response':doc})
    return doc['payload']


def save(name, pcm):
    with wave.open(str(FOLDER/name), 'wb') as output:
        output.setparams((1,2,8000,0,'NONE','not compressed'))
        output.writeframes(pcm)


def main(operator=False):
    global FOLDER
    if operator: FOLDER = ROOT / 'debug/runs/sip-answered-transfer'
    FOLDER.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('organizer_sip', '/organizer/check.py')
    sip = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sip)
    env = sip.load_env('/organizer-env/sip.env')
    env['SIP_VM_USER'], env['SIP_VM_PASSWORD'] = env['SIP_PHONE_USER'], env['SIP_PHONE_PASSWORD']
    prior = {r['call_id'] for r in post(CRM,'learning.calls',{'registrar':'114'})['calls']}
    phrases = {
        'inbound.welcome':'Запишите меня к терапевту.',
        'identity.name':'Иванов Иван Иванович.',
        'identity.birth':'Первое января тысяча девятьсот девяностого года.',
        'inbound.book_start':'К терапевту.',
        'inbound.book_offer':'Да, подходит.',
    }
    if operator: phrases['inbound.book_offer'] = 'Соедините меня с регистратором.'
    clips = {}
    for state, text in phrases.items():
        clip = post(API,'speech.synth',{'text':text,'rate':1.3})
        clips[state] = base64.b64decode(clip['pcm_b64'])
        save(state+'.wav', clips[state])
    observed = {'state':{}, 'call_id':None, 'last_voice':time.monotonic(), 'state_since':time.monotonic()}
    events, received, sent = [], bytearray(), bytearray()
    stop = threading.Event()

    def watch():
        while not stop.wait(.3):
            try:
                if not observed['call_id']:
                    new = [r for r in post(CRM,'learning.calls',{'registrar':'114'})['calls'] if r['call_id'] not in prior]
                    if len(new) == 1: observed['call_id'] = new[0]['call_id']
                    elif len(new) > 1: raise RuntimeError('Concurrent call: acceptance cannot attribute safely')
                if observed['call_id']:
                    state = post(API,'call.state',{'call_id':observed['call_id']})
                    if state.get('state') != observed['state'].get('state'):
                        observed['state_since'] = time.monotonic()
                        events.append({'elapsed':time.monotonic()-began,'snapshot':state})
                        (FOLDER/'events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
                    observed['state'] = state
            except Exception as error:
                observed['error'] = type(error).__name__

    def frames(payload, seconds):
        convert = audioop.lin2alaw if payload == 8 else audioop.lin2ulaw
        answered = set()
        pending = bytearray()
        last_send = 0
        for _ in range(int(seconds/.02)):
            state = observed['state']
            name = state.get('state')
            if not pending and name in clips and name not in answered and state.get('listening') and observed['last_voice'] > observed['state_since'] and time.monotonic()-observed['last_voice'] > 1 and time.monotonic()-last_send > 2:
                pending.extend(clips[name]+bytes(16000))
                answered.add(name)
                last_send = time.monotonic()
            frame = bytes(pending[:320]) if pending else bytes(320)
            del pending[:320]
            frame = frame.ljust(320,b'\0')
            sent.extend(frame)
            yield convert(frame,2)
            if state.get('finished') and time.monotonic()-observed['last_voice'] > (15 if operator else 2):
                break

    sip.tone_frames = frames
    agent = sip.Agent(env,15060,15062)
    class Tap:
        def __init__(self, original): self.original = original
        def __getattr__(self, name): return getattr(self.original,name)
        def fileno(self): return self.original.fileno()
        def recvfrom(self, size):
            packet, source = self.original.recvfrom(size)
            if len(packet)>12 and packet[1]&127 in (0,8):
                pcm = (audioop.alaw2lin if packet[1]&127==8 else audioop.ulaw2lin)(packet[12:],2)
                received.extend(pcm)
                if audioop.rms(pcm,2)>150: observed['last_voice'] = time.monotonic()
            return packet,source
    agent.rtp = Tap(agent.rtp)
    began = time.monotonic()
    worker = threading.Thread(target=watch,daemon=True)
    result = {'passed':False, 'scope':'actual organizer SIP/G711 -> Whisper -> Rust -> real DBI; synthetic patient 1002'}
    try:
        assert agent.register() == 200
        worker.start()
        result['sip'] = agent.call(env['SIP_NUMBER'],tone_seconds=170)
        state = observed['state']
        result['call_id'], result['final_state'] = observed['call_id'], state
        assert result['sip']['status'] == 200 and result['sip']['rtp_in']>100, result
        if operator:
            assert state.get('status') == 'transfer', state
            transfer = post(CRM,'stand.operator_status',{'call_id':observed['call_id']})
            result['operator'] = transfer
            assert transfer['sync_state'] == 'synced' and transfer['remote_id'], transfer
            result['scope'] = 'actual SIP answered test softphone + bridge + DBI escalation; not a staffed-human acceptance'
        else:
            assert state.get('status') == 'booked' and state['vars']['booking_status'] == 'booked', state
        result['passed'] = True
    finally:
        stop.set()
        worker.join(timeout=2)
        agent.register(expires=0)
        agent.sip.close()
        agent.rtp.close()
        state = observed['state']
        if state.get('vars',{}).get('booking_id'):
            result['cleanup'] = post(CRM,'cancel',{'booking_id':state['vars']['booking_id'],'patient_id':state['vars']['patient_id'],'operation_key':'sip-acceptance-'+observed['call_id']})
            assert result['cleanup']['stand_acknowledged'] is True
        if observed['call_id']:
            result['archive'] = post(CRM,'learning.call',{'registrar':'114','call_id':observed['call_id']})
        result['duration_seconds'] = time.monotonic()-began
        save('assistant.wav',received)
        save('caller.wav',sent)
        (FOLDER/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({'passed':result['passed'],'call_id':observed['call_id'],'duration_seconds':result['duration_seconds']},ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator',action='store_true')
    main(parser.parse_args().operator)
