"""Behavioral acceptance for isolated settings, booking, recording and replay."""
import base64
import copy
from pathlib import Path
import pytest
from clinic.demo_call import DemoCalls
from clinic.sandbox_store import SandboxStore
from clinic.module_settings import ModuleSettings, DEFAULTS
from clinic.call_analysis import analyze
from clinic.provider_adapter import ConfiguredService
from clinic.scenario_extensions import request as extension_request
from test_demo_call import wav


@pytest.fixture
def app(tmp_path,semantic_nlu):
    settings=ModuleSettings(tmp_path/'modules.json')
    store=SandboxStore(tmp_path/'stand')
    heard=['К глазнику']
    def rpc(mod,op,args):
        if mod=='tts':return {'sample_rate':48000,'pcm_b64':base64.b64encode(b'\0\1'*100).decode()}
        if mod=='asr':return {'text':heard[0],'engine':'explicit test dependency'}
        if mod=='nlu':return semantic_nlu.handle(op,args)[1]
        raise AssertionError('No live CRM or external messages')
    demo=DemoCalls(rpc=rpc,store=store,settings=settings,tmp=tmp_path/'tmp')
    def req(op,**args):return demo.request('/api/demo-call/'+op,True,args,client='browser')
    return demo,store,settings,req,heard


def to_slots(req):
    key=req('start')['call_id']
    req('text',call_id=key,text='К глазнику меня запиши')
    req('text',call_id=key,text='один')
    req('text',call_id=key,text='Иванов Иван Иванович')
    result=req('text',call_id=key,text='12.04.1985')
    assert result['state']=='demo.identity_readback'
    assert not result['offers'] and not result['sandbox_booking']
    result=req('dtmf',call_id=key,digit='1')
    assert result['state']=='demo.slots',result
    return key,result


@pytest.mark.parametrize('phrase',['Первый вариант.','Вариант два.','Третий вариант!','1.'])
def test_spoken_slot_choice_still_requires_explicit_confirmation(app,phrase):
    _,store,_,req,_=app
    key,_=to_slots(req)
    result=req('text',call_id=key,text=phrase)
    assert result['state']=='demo.booking_confirm'
    assert not store.tables()['tables']['bookings']


@pytest.mark.parametrize('phrase',['Не первый вариант.','Первый или второй.'])
def test_uncertain_slot_choice_does_not_select_or_book(app,phrase):
    _,store,_,req,_=app
    key,_=to_slots(req)
    result=req('text',call_id=key,text=phrase)
    assert result['state']=='demo.slots'
    assert not store.tables()['tables']['bookings']


@pytest.mark.parametrize('phrase',['К глазнику','К глазнику меня запиши','К окулисту','К офтальмологу'])
def test_colloquial_specialty(app,phrase):
    _,_,_,req,_=app
    key=req('start')['call_id'];result=req('text',call_id=key,text=phrase)
    assert result['selection']['id']=='ophthalmology'


@pytest.mark.parametrize('phrase',['Здравствуйте','ЧТО?','ало','алло','И чё ты от меня хочешь?'])
def test_contact_and_repeat_preserve_current_question(app,phrase):
    demo,_,_,req,_=app
    key=req('start')['call_id']
    for _ in range(4):assert req('text',call_id=key,text=phrase)['state']=='demo.menu'
    assert demo.calls[key].attempts==0


def test_abuse_has_system_flag_even_when_client_tags_off(app):
    _,store,settings,req,_=app
    assert not settings.read()['client_tags']['enabled']
    key=req('start')['call_id'];r=req('text',call_id=key,text='да иди ты нахер')
    assert r['state']=='demo.operator' and 'abusive_language' in r['flags']
    assert 'Перевожу на оператора' in r['prompt']
    assert store.call(key)['flags']==r['flags']
    assert not store.tables()['tables']['client_tags']


def test_procedure_word_never_becomes_a_completed_appointment(app):
    _,store,_,req,_=app
    key=req('start')['call_id'];r=req('text',call_id=key,text='К хирургу на операцию лучезапястного сустава')
    assert r['state'] in {'demo.operator','demo.confirm'} and not r['sandbox_booking']
    assert not store.tables()['tables']['bookings']


def test_full_booking_requires_identity_slot_and_final_consent(app):
    _,store,_,req,_=app
    key,r=to_slots(req)
    assert not store.tables()['tables']['bookings'] and len(r['offers'])==3
    assert req('text',call_id=key,text='85')['state']=='demo.slots'
    selected=req('text',call_id=key,text='два')
    assert selected['state']=='demo.booking_confirm'
    assert not store.tables()['tables']['bookings']
    done=req('text',call_id=key,text='да')
    assert done['state']=='demo.done' and done['sandbox_booking']['slot']['id']==r['offers'][1]['id']
    tables=store.tables()['tables']
    assert len(tables['bookings'])==1 and tables['queries']
    assert any('INSERT INTO bookings' in q['sql'] for q in tables['queries'])
    assert done['executed'] is False


def test_unknown_patient_never_gets_slot(app):
    _,store,_,req,_=app
    key=req('start')['call_id'];req('dtmf',call_id=key,digit='1');req('dtmf',call_id=key,digit='1')
    req('text',call_id=key,text='Иванов Иван Иванович')
    r=req('text',call_id=key,text='13.04.1985')
    assert r['state']=='demo.identity_readback' and not r['offers']
    # Confirming a nonexistent identity never gives access to appointments.
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.identity_readback' and not r['offers']
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.identity_policy'
    r=req('text',call_id=key,text='9999999999999999')
    assert r['state']=='demo.operator' and 'patient_not_found_after_oms' in r['flags']
    assert not store.tables()['tables']['bookings']


def test_referral_is_checked_in_table_despite_spoken_yes(app):
    _,store,_,req,_=app
    key=req('start')['call_id'];req('text',call_id=key,text='к кардиологу')
    req('text',call_id=key,text='да есть направление');req('text',call_id=key,text='один')
    req('text',call_id=key,text='Иванов Иван Иванович')
    r=req('text',call_id=key,text='12.04.1985')
    assert r['state']=='demo.identity_readback'
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.operator' and 'referral_not_verified' in r['flags']
    assert not store.tables()['tables']['bookings']


def test_slot_taken_after_offer_is_rejected(app):
    _,store,_,req,_=app
    key,r=to_slots(req);slot=r['offers'][0]['id']
    req('dtmf',call_id=key,digit='1')
    store.book('other-call',2,slot,'ophthalmology',False)
    result=req('dtmf',call_id=key,digit='1')
    assert result['state']=='demo.slots' and 'booking_rejected' in result['flags']
    assert len(store.tables()['tables']['bookings'])==1


@pytest.mark.parametrize('consent',[False,True])
def test_text_always_persists_audio_only_with_consent_and_tmp_cleans(app,consent):
    demo,store,_,req,heard=app
    key=req('start',consent_audio=consent)['call_id']
    heard[0]='К глазнику';req('audio',call_id=key,wav_b64=wav())
    req('end',call_id=key)
    reopened=SandboxStore(store.folder)
    document=reopened.call(key)
    assert any(t.get('source')=='real_asr' for t in document['transcript'])
    assert bool(list((store.audio/key).glob('*.wav')))==consent
    assert not list(demo.tmp.iterdir())


def test_asr_failure_cleans_transient_audio(app):
    demo,_,_,req,_=app
    key=req('start')['call_id']
    demo.rpc=lambda *a:(_ for _ in ()).throw(ValueError('ASR unavailable'))
    with pytest.raises(ValueError):req('audio',call_id=key,wav_b64=wav())
    assert not list(demo.tmp.iterdir())


def test_tts_failure_keeps_route_and_transcript(app):
    demo,store,_,req,_=app
    demo.rpc=lambda *a:(_ for _ in ()).throw(ValueError('TTS unavailable'))
    r=req('start')
    assert r['audio_error']=='TTS unavailable' and store.call(r['call_id'])['transcript']


def test_import_maps_real_turns_without_fabricating_speech_or_booking(app):
    demo,store,_,_,_=app
    r=analyze(demo,{'text':'Сотрудник: Добрый день\nПациент: Здравствуйте\nПациент: К глазнику\nСотрудник: Уточним данные'},'admin')
    assert r['analysis'] and r['trace'][-1]=='demo.confirm'
    assert len(r['transcript'])==4 and 'mapping' in r
    assert any(m['status']=='needs_review' for m in r['mapping'])
    assert not store.tables()['tables']['bookings']


def test_settings_roundtrip_isolation_and_no_secret_fields(app):
    _,_,settings,_,_=app
    values=settings.read();values['tts'].update(provider='openrouter',model='google/gemini-example-tts',voice='Kore')
    settings.save(values)
    assert settings.read()['asr']==DEFAULTS['asr']
    assert ModuleSettings(settings.path).read()['tts']['provider']=='openrouter'
    bad=copy.deepcopy(values);bad['tts']['api_key']='secret'
    with pytest.raises(ValueError):settings.save(bad)
    bad=copy.deepcopy(values);bad['asr']['model']='unlicensed'
    with pytest.raises(ValueError):settings.save(bad)


@pytest.mark.parametrize('module', ['tts','asr','nlu','agent','crm','sms','operator'])
def test_each_module_can_be_fully_disabled_before_its_factory(app,module):
    _,_,settings,_,_=app
    values=settings.read();values[module]['enabled']=False;settings.save(values)
    service=ConfiguredService(module,lambda item:pytest.fail('Disabled module initialized'),settings)
    with pytest.raises(ValueError,match='отключён'):service.handle('anything',{})


def test_remote_adapter_uses_selected_model_not_local(app,monkeypatch):
    _,_,settings,_,_=app
    values=settings.read();values['asr'].update(provider='openrouter',model='chosen/audio');settings.save(values)
    calls=[]
    def remote(item,path,payload):
        calls.append((item,path,payload));return b'{"text":"hello"}','application/json'
    monkeypatch.setattr('clinic.provider_adapter.remote',remote)
    service=ConfiguredService('asr',lambda item:pytest.fail('Local fallback forbidden'),settings)
    assert service.handle('transcribe',{'pcm_b64':base64.b64encode(b'\0\1'*100).decode(),'rate':16000})[1]['text']=='hello'
    assert calls[0][1]=='audio/transcriptions' and calls[0][2]['model']=='chosen/audio'


def test_extensions_disabled_and_registrar_cannot_compile_global(app):
    demo,store,settings,req,_=app
    admin={'role':'admin','username':'admin'};user={'role':'registrar','username':'user','registrar':'100'}
    with pytest.raises(ValueError,match='отключён'):extension_request(store,settings,admin,'compile',{})
    values=settings.read();values['scenario_extensions']['enabled']=True;settings.save(values)
    extension_request(store,settings,user,'add',{'scope':'global','phrase':'где вход','response':'Вход справа','state':'menu'})
    result=extension_request(store,settings,user,'compile',{'scope':'global'})
    assert result['scope']=='line:100'
    key=req('start')['call_id'];r=req('text',call_id=key,text='где вход')
    assert 'Вход справа' in r['prompt'] and r['state']=='demo.menu'
    values['scenario_extensions']['enabled']=False;settings.save(values)
    key=req('start')['call_id'];assert 'Вход справа' not in req('text',call_id=key,text='где вход')['prompt']


def test_live_disable_prevents_existing_call_booking(app):
    _,store,settings,req,_=app
    key,_=to_slots(req);req('dtmf',call_id=key,digit='1')
    values=settings.read();values['crm']['enabled']=False;settings.save(values)
    assert req('dtmf',call_id=key,digit='1')['state']=='demo.operator'
    assert not store.tables()['tables']['bookings']


def test_provider_model_catalog_filters_by_actual_modality(monkeypatch):
    import io,json
    from clinic.module_settings import model_catalog
    models=[{'id':'voice','architecture':{'input_modalities':['text'],'output_modalities':['speech']}},
            {'id':'listener','architecture':{'input_modalities':['audio'],'output_modalities':['text']}},
            {'id':'reader','architecture':{'input_modalities':['text'],'output_modalities':['text']}}]
    monkeypatch.setattr('calls.services.netutil.open_http',lambda *a,**kw:io.BytesIO(json.dumps({'data':models}).encode()))
    assert [m['id'] for m in model_catalog('tts')['models']]==['voice']
    assert [m['id'] for m in model_catalog('asr')['models']]==['listener']
    assert [m['id'] for m in model_catalog('nlu')['models']]==['reader']


def test_optional_preferences_are_reused_and_disabled_without_deleting_history(app):
    _,store,settings,req,_=app
    values=settings.read();values['preferences']['enabled']=True;settings.save(values)
    key,_=to_slots(req)
    result=req('text',call_id=key,text='Предпочитаю вечером')
    assert store.saved_preference(1)=='evening'
    assert all(s['starts_at'][11:13]=='18' for s in result['offers'])
    req('end',call_id=key)
    key,result=to_slots(req)
    assert all(s['starts_at'][11:13]=='18' for s in result['offers'])
    req('end',call_id=key)
    values['preferences']['enabled']=False;settings.save(values)
    _,result=to_slots(req)
    assert result['offers'][0]['starts_at'][11:13]=='10'
    assert store.saved_preference(1)=='evening'


@pytest.mark.parametrize('consent',[False,True])
def test_long_import_chunks_have_owner_order_consent_and_cleanup(app,consent):
    from clinic.call_analysis import Uploads
    demo,store,_,_,_=app;demo.uploads=Uploads(demo)
    key=demo.uploads.request({'op':'start'},'admin')['upload_id']
    with pytest.raises(ValueError):demo.uploads.request({'op':'chunk','upload_id':key,'index':0,'wav_b64':wav()},'other')
    with pytest.raises(ValueError):demo.uploads.request({'op':'chunk','upload_id':key,'index':1,'wav_b64':wav()},'admin')
    demo.uploads.request({'op':'chunk','upload_id':key,'index':0,'wav_b64':wav()},'admin')
    demo.uploads.request({'op':'chunk','upload_id':key,'index':1,'wav_b64':wav(silent=True)},'admin')
    result=analyze(demo,{'text':'Пациент: К глазнику','upload_id':key,'consent_audio':consent},'admin')
    assert bool(result['audio_files'])==consent
    assert not (demo.uploads.root/key).exists() and key not in demo.uploads.items
    assert not store.tables()['tables']['bookings']


def test_remote_tts_preserves_pcm_contract_and_does_not_load_local_model(app,monkeypatch):
    import io,math,struct,wave
    from clinic.provider_adapter import speech
    output=io.BytesIO()
    with wave.open(output,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(b''.join(struct.pack('<h',int(5000*math.sin(i*.1))) for i in range(24000)))
    requests=[]
    def remote(item,path,payload):
        requests.append((item,path,payload));return output.getvalue(),'audio/wav'
    monkeypatch.setattr('clinic.provider_adapter.remote',remote)
    item={**DEFAULTS['tts'],'provider':'openrouter','model':'chosen/tts','voice':'voice-x'}
    status,result=speech('tts','preview',{'text':'Фраза без обязательного ударения','rate':1},item)
    assert status=='ok' and result['sample_rate']==48000
    assert len(base64.b64decode(result['pcm_b64']))>90000
    assert requests[0][1]=='audio/speech' and requests[0][2]['voice']=='voice-x'


def test_remote_nlu_cannot_replace_local_emergency_handling(app,monkeypatch):
    from types import SimpleNamespace
    _,_,settings,_,_=app
    values=settings.read();values['nlu'].update(provider='openrouter',model='chosen/nlu');settings.save(values)
    local=SimpleNamespace(handle=lambda op,args:('ok',{'intent':'emergency','vars':{'specialty':'терапевт'}}))
    monkeypatch.setattr('clinic.provider_adapter.remote',lambda *a:pytest.fail('Emergency must not call a cloud classifier'))
    service=ConfiguredService('nlu',lambda item:local,settings)
    assert service.handle('interpret',{'text':'Не могу дышать'})[1]['intent']=='emergency'


def test_remote_nlu_changes_only_valid_intent(app,monkeypatch):
    import json
    from types import SimpleNamespace
    _,_,settings,_,_=app
    values=settings.read();values['nlu'].update(provider='openrouter',model='chosen/nlu');settings.save(values)
    local=SimpleNamespace(handle=lambda op,args:('ok',{'intent':'unknown','vars':{'specialty':'терапевт'}}))
    raw=json.dumps({'choices':[{'message':{'content':'{"intent":"info_schedule"}'}}]}).encode()
    monkeypatch.setattr('clinic.provider_adapter.remote',lambda *a:(raw,'application/json'))
    r=ConfiguredService('nlu',lambda item:local,settings).handle('interpret',{'text':'Когда приём?'})[1]
    assert r['intent']=='info_schedule' and r['vars']=={'specialty':'терапевт'}


def test_slot_agent_uses_its_own_config_without_touching_environment(monkeypatch):
    from clinic.agent import SlotAgent
    monkeypatch.setenv('UNIT_PROVIDER_KEY','explicit-contract-key')
    monkeypatch.setenv('AGENT_MODE','deterministic')
    item={**DEFAULTS['agent'],'provider':'openrouter','model':'chosen/agent','key_env':'UNIT_PROVIDER_KEY'}
    agent=SlotAgent(settings=item)
    assert agent.mode=='openrouter' and agent.model=='chosen/agent'
    assert agent.url=='https://openrouter.ai/api/v1/chat/completions'
    import os
    assert os.environ['AGENT_MODE']=='deterministic'


def test_disabling_sms_stops_background_outbox_processing(app,monkeypatch):
    from clinic.modules import Sms
    _,_,settings,_,_=app
    values=settings.read();values['sms']['enabled']=False;settings.save(values)
    monkeypatch.setenv('MODULE_SETTINGS_FILE',str(settings.path))
    service=Sms();service.call=lambda *a:pytest.fail('Disabled outbox must not read or send')
    service.flush()


def test_imported_full_conversation_maps_without_accessing_patient_or_slot_tables(app,monkeypatch):
    demo,store,_,_,_=app
    for name in ('lookup_patient','has_referral','slots','book','preference'):
        monkeypatch.setattr(store,name,lambda *a:pytest.fail('Replay must not touch operational tables'))
    text='\n'.join('Пациент: '+t for t in ['К глазнику','да','Сидоров Пётр Алексеевич','11.02.1970','один','да'])
    result=analyze(demo,{'text':text},'admin')
    assert result['state']=='demo.done' and result['sandbox_booking'] is None
    assert result['title']=='Подтверждение со слов участников'
    assert 'booking_status_not_verified' in result['flags']


def test_continue_and_identity_question_do_not_reselect_specialty(app):
    _,_,_,req,_=app
    key=req('start')['call_id'];req('text',call_id=key,text='Офтальмолог')
    r=req('text',call_id=key,text='Продолжить.')
    assert r['state']=='demo.identity_name'
    r=req('text',call_id=key,text='Имя-отчество офтальмолога?')
    assert r['state']=='demo.identity_name' and 'пациента' in r['prompt']
    assert r['selection']['id']=='ophthalmology'


def test_birth_queries_and_no_century_guess_or_age_cutoff(app):
    _,store,_,req,_=app
    with store.db:
        store.db.execute("DELETE FROM patients WHERE id=4")
        store.db.execute("INSERT INTO patients(id,full_name,birth_date) VALUES(99,'Иванов Сергей Владимирович','1915-10-05')")
    key=req('start')['call_id'];req('text',call_id=key,text='К глазнику');req('text',call_id=key,text='Продолжить')
    req('text',call_id=key,text='Иванов Сергей Владимирович')
    for phrase in ['Ноль три, ноль пять девяносто шесть.','Двенадцатое 09.81','5 сентября восьмого года.']:
        r=req('text',call_id=key,text=phrase)
        assert r['state']=='demo.identity_birth' and 'год рождения полностью' in r['prompt'] and not r['offers'] and not r['sandbox_booking']
    r=req('text',call_id=key,text='5 октября 1915')
    assert r['state']=='demo.identity_readback' and not r['offers']
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.slots' and r['patient']['id']==99
    query=next(q for q in reversed(r['queries']) if 'FROM identity_people' in q['sql'] and '1915-10-05' in q['params'])
    assert query['result'][0]['id']==99
    assert query['state']=='demo.identity_readback' and store.call(key)['queries']==r['queries']


def test_initial_name_is_retained_but_requires_confirmation(app):
    _,_,_,req,_=app
    key=req('start')['call_id']
    req('text',call_id=key,text='К участковому терапевту Евгений Петрович Замязин 059364')
    r=req('text',call_id=key,text='Продолжить')
    assert r['state']=='demo.identity_confirm' and 'Замязин Евгений Петрович' in r['prompt']
    assert req('text',call_id=key,text='Да')['state']=='demo.identity_birth'


def test_multiple_doctors_and_unknown_district_must_be_resolved(app):
    _,store,_,req,_=app
    with store.db:
        store.db.execute('UPDATE patients SET district=NULL WHERE id=1')
        store.db.execute("INSERT INTO doctors VALUES(9,'Другой тестовый врач','therapist','14')")
    key=req('start')['call_id'];req('dtmf',call_id=key,digit='1');req('text',call_id=key,text='Продолжить')
    req('text',call_id=key,text='Иванов Иван Иванович')
    r=req('text',call_id=key,text='12.04.1985')
    assert r['state']=='demo.identity_readback'
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.district' and not r['offers']
    r=req('text',call_id=key,text='четырнадцать')
    assert r['state']=='demo.doctor' and 'Другой тестовый врач' in r['prompt'] and not r['offers']
    r=req('text',call_id=key,text='1')
    assert r['state']=='demo.slots' and all(s['doctor']==r['doctor']['full_name'] for s in r['offers'])


@pytest.mark.parametrize('reply',['нет','Нет, не хочу','0'])
def test_specialty_confirmation_denial_transfers_to_operator(app,reply):
    _,store,_,req,_=app
    key=req('start')['call_id']
    r=req('text',call_id=key,text='Офтальмолог')
    assert r['prompt']=='Вы хотите записаться к офтальмологу? Да — один. Нет — ноль, переведу на оператора.'
    r=req('text',call_id=key,text=reply)
    assert r['state']=='demo.operator' and 'specialty_not_confirmed' in r['flags']
    assert not store.tables()['tables']['bookings']
