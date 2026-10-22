from datetime import date, datetime
import json

from calls.crm.registry import MockRegistry, seed_demo
from clinic.crm import ClinicCrm
from clinic.telephony import Bridge, Context


class Stand:
    def __init__(self, lose_reply=False): self.calls=[]; self.lose_reply=lose_reply
    def request(self, method, path, body):
        self.calls.append((method,path,body))
        if path=='/clients': return {'id':17}
        if self.lose_reply: raise OSError('acknowledgement lost')
        return {'id':23, **body}


def clinic(tmp_path, patient_id=3, lose_reply=False):
    reg=MockRegistry(str(tmp_path/'clinic.sqlite'),datetime(2026,10,21,12))
    seed_demo(reg,date(2026,10,22))
    stand=Stand(lose_reply)
    crm=ClinicCrm(reg,stand)
    status,_=crm.handle('operator_transfer',{'call_id':'test-transfer','reason':'requested','vars':{'patient_id':patient_id}})
    assert status=='ok'
    assert not stand.calls, 'Offering a transfer must not record a completed transfer'
    return crm,stand


def test_answered_transfer_uses_remote_client_and_is_idempotent(tmp_path):
    crm,stand=clinic(tmp_path)
    assert crm.handle('stand.operator_connected',{'target_call_id':'test-transfer','call_id':''})[1]['stand_acknowledged'] is True
    assert stand.calls[-1][1]=='/escalations'
    assert stand.calls[-1][2]['client_id']==17
    crm.handle('stand.operator_connected',{'call_id':'test-transfer'})
    assert len([x for x in stand.calls if x[1]=='/escalations'])==1


def test_lost_reply_is_quarantined_without_second_post(tmp_path):
    crm,stand=clinic(tmp_path,lose_reply=True)
    assert crm.handle('stand.operator_connected',{'call_id':'test-transfer'})[1]['state']=='uncertain'
    crm.handle('stand.operator_connected',{'call_id':'test-transfer'})
    assert len([x for x in stand.calls if x[1]=='/escalations'])==1


def test_unknown_identity_stays_local(tmp_path):
    crm,stand=clinic(tmp_path,patient_id=None)
    assert crm.handle('stand.operator_connected',{'call_id':'test-transfer'})[1]['state']=='local_only'
    assert not stand.calls


class Ari:
    def __init__(self): self.events=[]
    def _req(self,*args,**kwargs): self.events.append(('request',args,kwargs)); return {}
    def bridge(self,*channels): self.events.append(('bridge',channels))
    def hangup(self,channel): self.events.append(('hangup',channel))


class Client:
    def __init__(self): self.calls=[]
    def call(self,op,**args): self.calls.append((op,args)); return {'status':'ok'}


def test_only_expected_answered_operator_records_escalation():
    ari,client=Ari(),Client()
    bridge=Bridge(ari,client)
    ctx=Context('call-1','caller',media='media',operator='expected-operator')
    bridge.calls[ctx.call_id]=ctx
    bridge.connect_operator({'id':'wrong-channel'},ctx.call_id)
    assert not client.calls
    bridge.connect_operator({'id':'expected-operator'},ctx.call_id)
    assert ('bridge',('caller','expected-operator')) in ari.events
    assert ctx.operator_connected.is_set()
    assert client.calls[0][0]=='stand.operator_connected'
