"""One actual outbound reminder to the team's synthetic test softphone only.

The shell fixture temporarily assigns phone-114 to synthetic patient 1002.
No real phone numbers, no mass campaign. Confirmation is local because DBI has
no Confirmed status. Cancels only the visit created by this test.
"""
import audioop
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time
from datetime import datetime,timedelta,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'runtime/src'),str(ROOT/'debug')]
from run_vm_sip_booking import post, API, CRM
from calls.dtmf.synth import dtmf_case
from clinic import outbound

FOLDER=ROOT/'debug/runs/sip-outbound-reminder'


def main(reschedule=False):
    global FOLDER
    if reschedule:FOLDER=ROOT/'debug/runs/sip-outbound-reschedule'
    FOLDER.mkdir(parents=True,exist_ok=True)
    booking=None
    result={'passed':False,'synthetic_only':True,'destination':'assigned team softphone','confirmation_scope':'local; DBI has no confirmed status'}
    spec=importlib.util.spec_from_file_location('organizer_sip','/organizer/check.py')
    sip=importlib.util.module_from_spec(spec);spec.loader.exec_module(sip)
    env=sip.load_env('/organizer-env/sip.env')
    env['SIP_VM_USER'],env['SIP_VM_PASSWORD']=env['SIP_PHONE_USER'],env['SIP_PHONE_PASSWORD']
    agent=sip.Agent(env,15060,15062)
    prior={r['call_id'] for r in post(CRM,'learning.calls',{'registrar':'114'})['calls']}
    shared={'id':None,'state':{},'since':time.monotonic(),'voice':time.monotonic()}
    stop=threading.Event()
    events=[]
    received=bytearray()
    def receive():
        packet,_=agent.rtp.recvfrom(2048)
        if len(packet)>12 and packet[1]&127 in (0,8):
            pcm=(audioop.alaw2lin if packet[1]&127==8 else audioop.ulaw2lin)(packet[12:],2)
            received.extend(pcm)
            if audioop.rms(pcm,2)>150:shared['voice']=time.monotonic()
    agent.handle_rtp=receive
    def watch():
        while not stop.wait(.3):
            try:
                if not shared['id']:
                    rows=[r for r in post(CRM,'learning.calls',{'registrar':'114'})['calls'] if r['call_id'] not in prior]
                    if len(rows)==1:shared['id']=rows[0]['call_id']
                if shared['id']:
                    state=post(API,'call.state',{'call_id':shared['id']})
                    if state['state']!=shared['state'].get('state'):
                        shared['since']=time.monotonic();events.append(state)
                        (FOLDER/'events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
                    shared['state']=state
            except (OSError,ValueError,KeyError):pass
    digits=b''.join(dtmf_case(d,100,codec='alaw',trail_ms=200)[0].tobytes() for d in '1002#')+bytes(16000)
    consent=post(API,'speech.synth',{'text':'Перенесите запись.' if reschedule else 'Да, подтверждаю.','rate':1.3})
    clips={'reminder.identity':digits,'reminder.start':base64.b64decode(consent['pcm_b64'])+bytes(16000)}
    if reschedule:
        for state,text in {'reschedule.rs_offer_day':'Да, подходит.','reschedule.rs_ask_part':'Утром.','reschedule.rs_offer_time':'Да, подходит.'}.items():
            clip=post(API,'speech.synth',{'text':text,'rate':1.3})
            clips[state]=base64.b64decode(clip['pcm_b64'])+bytes(16000)
    sent=set();pending=bytearray();seq=1;stamp=0
    watcher=threading.Thread(target=watch,daemon=True)
    dialer=None
    try:
        tomorrow=(datetime.now(timezone(timedelta(hours=4)))+timedelta(days=1)).date().isoformat()
        slot=post(CRM,'find_slots',{'specialty':'терапевт','said_date':tomorrow})
        assert slot['slot_date']==tomorrow
        booking=post(CRM,'book',{'patient_id':2,'slot_id':slot['slot_id'],'operation_key':'test-reminder-'+str(time.time_ns())})
        assert booking['stand_acknowledged'] is True
        assert agent.register()==200
        os.environ.update(OUTBOUND_ENABLED='1',ANTI_FRAUD_PUBLISHED='1',OUTBOUND_ALLOWED_PHONES=env['SIP_PHONE_USER'],OUTBOUND_ONCE='1',SIP_OUTBOUND_ENDPOINT='PJSIP/{phone}@gateway',CLINIC_PHONE=env['SIP_NUMBER'],ARI_URL='http://127.0.0.1:18088')
        watcher.start()
        dialer=threading.Thread(target=outbound.run,daemon=True);dialer.start()
        start=time.monotonic()
        while time.monotonic()-start<240:
            agent.poll(.001)
            state=shared['state'];name=state.get('state')
            if not pending and name in clips and name not in sent and shared['voice']>shared['since'] and time.monotonic()-shared['voice']>1:
                pending.extend(clips[name]);sent.add(name)
            frame=bytes(pending[:320]) if pending else bytes(320)
            del pending[:320]
            for call in list(agent.calls.values()):
                agent.rtp.sendto(struct.pack('!BBHII',0x80,8,seq&65535,stamp&0xffffffff,agent.ssrc)+audioop.lin2alaw(frame.ljust(320,b'\0'),2),call['remote'])
            seq+=1;stamp+=160
            if state.get('finished') and not agent.calls:break
            time.sleep(.019)
        result['call_id']=shared['id'];result['status']=shared['state'].get('status')
        assert result['status']==('rescheduled' if reschedule else 'confirmed'), result
        if reschedule:
            assert shared['state']['vars'].get('stand_acknowledged') is True
            result['confirmation_scope']='DBI acknowledged reschedule'
        result['passed']=True
    finally:
        stop.set()
        if watcher.is_alive():watcher.join(timeout=2)
        if shared['id']:
            result['archive']=post(CRM,'learning.call',{'registrar':'114','call_id':shared['id']})
        if booking:
            result['cleanup']=post(CRM,'cancel',{'booking_id':booking['booking_id'],'patient_id':2,'operation_key':'reminder-test-cleanup-'+str(booking['booking_id'])})
        agent.register(expires=0);agent.sip.close();agent.rtp.close()
        import wave
        with wave.open(str(FOLDER/'assistant.wav'),'wb') as output:
            output.setparams((1,2,8000,0,'NONE','not compressed'));output.writeframes(received)
        (FOLDER/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({k:result.get(k) for k in ['passed','status','confirmation_scope']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reschedule',action='store_true')
    main(parser.parse_args().reschedule)
