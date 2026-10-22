"""Cumulative literal identity fields through the phone service and registrar client."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from calls.crm.registry import MockRegistry
from clinic.identity_form import IdentityForm
from clinic.identity_service import IdentityService
from clinic.demo_call import DemoCalls
from clinic.module_settings import ModuleSettings
from clinic.sandbox_store import SandboxStore

TODAY=date(2026,10,22)
CORPUS=Path(__file__).resolve().parents[1]/'fixtures/speech_regression'
SPOKEN=next(item['text'] for item in json.loads((CORPUS/'manifest.json').read_text())['items'] if item['id']=='missing_patronymic-2')
ASR='Иванов Иван родился 12 апреля 1985 года.'


@pytest.fixture
def service(monkeypatch):
    import clinic.identity_service as module
    monkeypatch.setattr(module,'configured',lambda:SimpleNamespace(read=lambda:{'dialogue':{'enabled':False}}))
    registry=MockRegistry()
    value=IdentityService(registry,today=lambda:TODAY)
    yield value
    registry._conn.close()


def patient(service, identifier=1, last='Иванов', first='Иван', middle='Петрович', phone=None, oms=None):
    service.reg._x('INSERT INTO patients(id,last_name,first_name,middle_name,birth_date,phone,oms) VALUES(?,?,?,?,?,?,?)',
                   (identifier,last,first,middle,'1985-04-12',phone,oms))


def test_card_route_requires_birth_and_database_readback(service):
    patient(service)
    service.reg._x('UPDATE patients SET card_no=? WHERE id=1',('1003',))
    service.start('card')
    assert service.use_card('card')[0]=='card'
    assert service.answer('card','1003')[0]=='birth'
    stage,result=service.answer('card','12.04.1985')
    assert stage=='match_confirm' and 'patient_id' not in result
    stage,result=service.answer('card','да')
    assert stage=='ok' and result['patient_id']==1


def test_card_route_never_verifies_wrong_birth_or_locked_patient(service):
    patient(service)
    service.reg._x('UPDATE patients SET card_no=? WHERE id=1',('1003',))
    for key,birth,locked in [('wrong-birth','13.04.1985',None),('wrong-person','12.04.1985',2)]:
        service.start(key,locked_patient_id=locked)
        service.use_card(key);service.answer(key,'1003')
        stage,result=service.answer(key,birth)
        if stage=='match_confirm':stage,result=service.answer(key,'да')
        assert stage=='operator' and 'patient_id' not in result


def test_declined_card_identity_clears_card_route_before_new_name(service):
    patient(service)
    service.reg._x('UPDATE patients SET card_no=? WHERE id=1',('1003',))
    service.start('correction');service.use_card('correction')
    service.answer('correction','1003');service.answer('correction','12.04.1985')
    assert service.answer('correction','нет')[0]=='name'
    service.answer('correction','Иванов Иван Петрович')
    assert service.answer('correction','12.04.1985')[0]=='readback'
    assert service.answer('correction','да')[0]=='ok'


@pytest.mark.parametrize('utterance',[SPOKEN,ASR,'Иванов Иван 12.04.1985','Иванов Иван двенадцатого апреля тысяча девятьсот восемьдесят пятого года'])
def test_partial_names_and_complete_dob_accumulate_without_model(service,utterance):
    patient(service)
    def no_model(*args):
        raise AssertionError('Straightforward names and DOB do not need an LLM')
    service.model.extract=no_model
    service.start('caller')
    stage,result=service.answer('caller',utterance)
    assert stage=='name' and result['identity_missing']==['middle_name']
    assert result['identity_parts']=={'last_name':'Иванов','first_name':'Иван'}
    assert result['identity_birth_date']=='1985-04-12' and 'patient_id' not in result
    assert 'отчеств' in result['identity_question'] and 'рожден' not in result['identity_question']
    stage,result=service.answer('caller','Отчество Петрович.')
    assert stage=='readback' and result['identity_missing']==[]
    assert result['identity_birth_date']=='1985-04-12'
    assert 'Иванов Иван Петрович' in result['identity_question']
    assert 'patient_id' not in result
    stage,result=service.answer('caller','Да, всё верно.')
    assert stage=='ok' and result['patient_id']==1
    assert result['patient_birth_date']=='1985-04-12'


def test_explicit_absent_middle_name_is_exact_not_wildcard(service):
    patient(service,middle='')
    patient(service,identifier=2,middle='Петрович')
    service.start('none')
    service.answer('none',SPOKEN)
    stage,result=service.answer('none','Нет отчества')
    assert stage=='readback' and not result['identity_missing']
    assert 'отчества нет' in result['identity_question']
    assert service.answer('none','1')[1]['patient_id']==1


def test_invalid_or_partial_birth_never_guesses_year_or_loses_known_names(service):
    service.start('invalid')
    stage,result=service.answer('invalid','Иванов Иван, родился 12.04.85')
    assert stage=='name' and result['identity_parts']=={'last_name':'Иванов','first_name':'Иван'}
    assert result['identity_birth_date']==''
    assert service.answer('invalid','Отчество Петрович')[0]=='birth'
    for value in ['31.02.1985','12 апреля восемьдесят пятого года','1985','Завтра']:
        stage,result=service.answer('invalid',value)
        assert stage=='birth' and 'patient_id' not in result


def test_yo_spelling_is_preserved_and_equivalent_duplicate_is_not_autopicked(service):
    patient(service,last='Семёнов',phone='79001234567',oms='1234567890123456')
    patient(service,identifier=2,last='Семенов',phone='79001234567',oms='9999567890123456')
    service.start('yo',caller_phone='79001234567')
    stage,result=service.answer('yo','Семёнов Иван Петрович, родился 12.04.1985')
    assert stage=='readback' and result['identity_parts']['last_name']=='Семёнов'
    assert 'Семёнов' in result['identity_question']
    stage,result=service.answer('yo','1')
    assert stage=='policy' and 'patient_id' not in result
    # The shared phone or spelling accent cannot select either identical cohort member.
    assert service.answer('yo','3456')[0]=='operator'


def test_single_database_yo_difference_requires_visible_confirmation(service):
    patient(service,last='Семёнов')
    service.start('yo')
    service.answer('yo','Семенов Иван Петрович 12.04.1985')
    stage,result=service.answer('yo','1')
    assert stage=='match_confirm' and 'patient_id' not in result
    assert 'Семёнов' in result['identity_question']
    assert service.sessions['yo']['form'].parts['last_name']=='Семенов'
    assert service.answer('yo','0')[0]=='name'


def test_another_phone_owner_and_fuzzy_name_need_explicit_candidate_confirmation(service):
    patient(service,phone='79001234567')
    patient(service,identifier=2,last='Петров',phone='79007654321')
    service.start('other',caller_phone='79007654321')
    service.answer('other','Иванов Иван Петрович 12.04.1985')
    stage,result=service.answer('other','1')
    assert stage=='match_confirm' and 'patient_id' not in result
    assert service.answer('other','0')[0]=='name'
    service.start('typo',caller_phone='79001234567')
    service.answer('typo','Ивонов Иван Петрович 12.04.1985')
    stage,result=service.answer('typo','1')
    assert stage=='match_confirm' and 'Иванов' in result['identity_question']
    assert 'patient_id' not in result


def test_locked_identity_cannot_be_switched_by_confirmation(service):
    patient(service)
    service.start('locked',locked_patient_id=2)
    service.answer('locked','Иванов Иван Петрович 12.04.1985')
    stage,result=service.answer('locked','1')
    assert stage=='operator' and result['reason']=='identity_conflict'
    assert 'patient_id' not in result


def test_slow_extraction_does_not_serialize_twenty_call_sessions(service):
    # Each caller may use a slow optional model; the global session map lock
    # must not cover it. A barrier detects accidental global serialization.
    count=20
    barrier=threading.Barrier(count)
    def slow_extract(text,requested):
        barrier.wait(timeout=5)
        return {},True
    service.model.extract=slow_extract
    for index in range(count):service.start(str(index))
    started=time.monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        results=list(pool.map(lambda index:service.answer(str(index),'непонятные данные пациента'),range(count)))
    assert time.monotonic()-started<8
    assert all(stage=='name' for stage,_ in results)


def test_registrar_actual_text_route_keeps_dob_until_readback_and_database(tmp_path):
    store=SandboxStore(tmp_path/'sandbox')
    store.db.execute("UPDATE patients SET full_name='Иванов Иван Петрович' WHERE id=1")
    store.db.commit()
    settings=ModuleSettings(tmp_path/'modules.json')
    values=settings.read();values['tts']['enabled']=False;settings.save(values)
    def rpc(module,op,args):
        assert module=='nlu' and op=='interpret', 'No live MIS/LLM/TTS dependency'
        return {'semantic':{'specialty':{'status':'unknown'}},'intent':'unknown','yes_no':None}
    demo=DemoCalls(rpc=rpc,store=store,settings=settings)
    def request(op,**data):return demo.request('/api/demo-call/'+op,True,data,client='registrar-test')
    key=request('start')['call_id']
    request('dtmf',call_id=key,digit='1');request('dtmf',call_id=key,digit='1')
    assert demo.calls[key].state=='identity_name'
    result=request('text',call_id=key,text=ASR)
    assert result['state']=='demo.identity_name'
    assert result['identity_form']['missing_fields']==['middle_name']
    assert result['identity_form']['birth_date']=='1985-04-12'
    result=request('text',call_id=key,text='Отчество Петрович.')
    assert result['state']=='demo.identity_readback' and demo.calls[key].patient is None
    assert demo.calls[key].identity.birth_date=='1985-04-12'
    result=request('dtmf',call_id=key,digit='1')
    assert demo.calls[key].patient['id']==1
    assert result['state']!='demo.identity_birth'
    assert not store.tables()['tables']['bookings']
    store.db.close()


def test_untrusted_extractor_cannot_replace_literal_yo_spelling():
    from clinic.identity_form import literal_parts
    assert literal_parts({'last_name':'Семенов'},'Семёнов')=={}
    assert literal_parts({'last_name':'Семёнов'},'Семенов')=={}
    assert literal_parts({'last_name':'Семёнов'},'Семёнов')=={'last_name':'Семёнов'}


def test_spelled_correction_keeps_dob_but_requires_new_readback(service):
    patient(service,last='Семёнов')
    service.start('spelling')
    service.answer('spelling','Семенов Иван Петрович 12.04.1985')
    stage,result=service.answer('spelling','С е м ё н о в')
    assert stage=='readback' and result['identity_birth_date']=='1985-04-12'
    assert result['identity_parts']['last_name']=='Семёнов'
    assert not result['identity_readback_confirmed'] and 'patient_id' not in result
    assert service.answer('spelling','1')[0]=='ok'


def test_import_analysis_collects_combined_identity_without_database_lookup(tmp_path):
    from clinic.demo_call import Call
    store=SandboxStore(tmp_path/'analysis')
    settings=ModuleSettings(tmp_path/'modules.json')
    demo=DemoCalls(store=store,settings=settings)
    call=Call('observer',0,key='imported-only',analysis=True,modules=settings.read())
    call.identity.accept_name('Иванов Иван Петрович 12.04.1985',today=TODAY)
    call.selection={'id':'therapist','label':'Терапевт','referral_required':False}
    store.identity_candidates=lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('Archive analysis must not identify patients'))
    result=demo._identity_lookup(call)
    assert call.state=='slots' and call.patient is None
    assert 'identity_and_referral_not_verified' in call.flags
    store.db.close()


def test_database_candidate_readback_never_truncates_compound_names(service):
    service.start('compound')
    stage,result=service.confirm_candidate('compound',{'id':1,'full_name':'Ван Дер Сар Иван Петрович','birth_date':'1985-04-12'})
    assert stage=='match_confirm' and 'Ван Дер Сар Иван Петрович' in result['identity_question']
    assert 'patient_id' not in result


def test_phone_crm_dispatch_accumulates_fields_before_lookup(tmp_path, monkeypatch):
    from clinic.crm import ClinicCrm
    monkeypatch.setenv('CRM_NOW', '2026-10-22T12:00:00+04:00')
    registry = MockRegistry(str(tmp_path / 'phone.sqlite'))
    crm = ClinicCrm(registry)
    registry._x('INSERT INTO patients(id,last_name,first_name,middle_name,birth_date) VALUES(?,?,?,?,?)',
                (1, 'Иванов', 'Иван', 'Петрович', '1985-04-12'))
    try:
        assert crm.handle('identity_start', {'call_id': 'phone'})[0] == 'name'
        status, reply = crm.handle('identity_answer', {'call_id': 'phone', 'text': ASR})
        assert status == 'name' and reply['identity_missing'] == ['middle_name']
        assert reply['identity_birth_date'] == '1985-04-12'
        status, reply = crm.handle('identity_answer', {'call_id': 'phone', 'text': 'Петрович'})
        assert status == 'readback' and 'patient_id' not in reply
        status, reply = crm.handle('identity_answer', {'call_id': 'phone', 'text': 'Да'})
        assert status == 'ok' and reply['patient_id'] == 1
        assert not registry._q('SELECT * FROM bookings')
    finally:
        registry._conn.close()


@pytest.mark.parametrize('flag', ['CI', 'GITHUB_ACTIONS', 'ACT', 'CLINIC_ACCEPTANCE_TEST'])
def test_ci_never_calls_optional_generative_identity_model(monkeypatch, flag):
    from clinic.identity_form import DialogueModel
    monkeypatch.setenv(flag, 'true')
    settings = SimpleNamespace(read=lambda: {'dialogue': {'enabled': True, 'provider': 'local', 'model': 'test'}})
    monkeypatch.setattr('clinic.identity_form.open_http', lambda *_args, **_kwargs: pytest.fail('CI called a generative API'))
    assert DialogueModel(settings).complete({'task': 'extract'}, {}) is None
