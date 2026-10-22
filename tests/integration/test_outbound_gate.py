import pytest
from clinic import outbound


class Crm:
    def __init__(self, destination='phone-114'): self.calls=[]; self.destination=destination
    def call(self, op, args):
        self.calls.append((op,args))
        if op=='dial_due': return 'ok',{'items':[{'booking_id':1,'phone':'phone-114'}]}
        if op=='dial_reserve': return 'ok',{'attempt_id':3,'phone':self.destination}
        return 'ok',{}


class Ari:
    def __init__(self): self.calls=[]
    def _req(self, method,path,**args):
        self.calls.append((method,path,args))
        return {'id':'test-channel'} if method=='POST' else {'state':'Up'}


@pytest.fixture
def setup(monkeypatch):
    crm,ari=Crm(),Ari()
    monkeypatch.setattr(outbound,'CrmProxy',lambda:crm)
    monkeypatch.setattr(outbound,'Ari',lambda *args:ari)
    for k,v in {'OUTBOUND_ENABLED':'1','ANTI_FRAUD_PUBLISHED':'1','OUTBOUND_ONCE':'1','OUTBOUND_ALLOWED_PHONES':'phone-114','ARI_PASSWORD':'test-only','CLINIC_PHONE':'114','SIP_OUTBOUND_ENDPOINT':'PJSIP/{phone}@gateway'}.items():
        monkeypatch.setenv(k,v)
    return crm,ari


def test_answer_is_not_patient_confirmation(setup):
    crm,ari=setup
    outbound.run()
    assert ari.calls[0][2]['endpoint']=='PJSIP/phone-114@gateway'
    assert ari.calls[0][2]['appArgs']=='reminder,phone-114,1'
    assert crm.calls[-1]==('dial_result',{'attempt_id':3,'outcome':'answered'})
    assert not any(op=='confirm' for op,_ in crm.calls)


def test_empty_allowlist_blocks_job(setup,monkeypatch):
    monkeypatch.setenv('OUTBOUND_ALLOWED_PHONES','')
    with pytest.raises(ValueError):outbound.run()
    assert not setup[1].calls


def test_destination_outside_allowlist_never_reserves(setup,monkeypatch):
    monkeypatch.setenv('OUTBOUND_ALLOWED_PHONES','phone-999')
    outbound.run()
    assert [op for op,_ in setup[0].calls]==['dial_due']
    assert not setup[1].calls


def test_changed_reserved_destination_does_not_dial(setup):
    crm,ari=setup
    crm.destination='someone-else'
    outbound.run()
    assert not ari.calls
    assert crm.calls[-1][1]['outcome']=='failed'


def test_disabled_never_queries_contacts(setup,monkeypatch):
    monkeypatch.setenv('OUTBOUND_ENABLED','0')
    outbound.run()
    assert not setup[0].calls and not setup[1].calls
