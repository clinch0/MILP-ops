"""Test calls run a real, inspectable booking protocol against an isolated stand."""
from __future__ import annotations
import array
import base64
from dataclasses import dataclass, field
from datetime import date, datetime
import io
import math
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import wave

from .admin_rpc import module_call
from .module_settings import configured
from .sandbox_store import SandboxStore,clinic_now
from .specialty_dictionary import SpecialtyDictionary
from .dialogue_plan import compile_plan
from .identity_form import IdentityForm, DialogueModel, phone_digits, foreign_language_request,near_phone_owner

CATALOG_SOURCE = 'docs/requirements/clinic-regulation.txt, раздел 2.3'
POLICY_VERSION = 'sandbox-booking-4'
TITLES = dict(identity_match_confirm='Подтверждаем данные другого пациента',identity_readback='Подтверждаем распознанные данные',identity_phone='Уточняем телефон',identity_policy_confirm='Подтверждаем данные по ОМС',identity_policy='Полис ОМС',identity_confirm='Проверяем уже названное ФИО',district='Уточняем участок',doctor='Выбираем врача',menu='Куда записаться?',specialist='Выбираем специалиста',referral='Уточняем направление',
 therapist_first='Сначала участковый врач',confirm='Проверяем выбранный вариант',identity_name='Фамилия, имя и отчество',
 identity_birth='Дата рождения',slots='Свободные талоны',booking_confirm='Подтверждение записи',
 done='Запись в тестовой таблице',operator='Помощь регистратора',emergency='Экстренное обращение',closed='Звонок завершён')
PROMPTS = {
 'menu':'Здравствуйте! Куда хотите записаться? К участковому врачу — один. К специалисту — два. На исследование или анализы — три. Можно ответить голосом.',
 'identity_policy':'Назовите номер полиса ОМС или его последние четыре цифры. Для помощи регистратора нажмите звёздочку.',
 'district':'Номер участка в картотеке отсутствует. Назовите свой участок. Если не знаете, переведу на регистратора: нажмите звёздочку.',
 'specialist':'Назовите специалиста. Для помощи регистратора нажмите ноль.',
 'referral':'Есть направление? Да — один. Нет — ноль. Регистратор — два.',
 'therapist_first':'Без направления сначала обратитесь к участковому врачу. Выбрать участкового врача — один. Назад — ноль. Регистратор — два.',
 'identity_name':'Назовите фамилию, имя и отчество пациента.',
 'identity_birth':'Назовите дату рождения пациента: день, месяц и полный год.',
 'operator':'Перевожу на оператора. В тестовом режиме передача отмечена в журнале звонка.',
 'emergency':'Если состояние резко ухудшилось, звоните сто три или сто двенадцать. Требуется помощь сотрудника.',
 'done':'Запись сохранена в тестовой таблице. В настоящей поликлинике запись не создавалась. Спасибо!',
}
TERMINAL = {'done','operator','emergency','closed'}
BRANCHES = {
 'menu':['specialist','referral','confirm','operator','emergency'], 'specialist':['referral','confirm','operator'],
 'referral':['confirm','therapist_first','operator'], 'therapist_first':['confirm','menu','operator'],
 'district':['doctor','slots','operator'],'doctor':['slots','district','operator'],'identity_policy':['district','doctor','slots','operator'],'identity_confirm':['identity_birth','identity_name','operator'],
 'identity_readback':['identity_name','identity_policy','operator'], 'identity_phone':['identity_policy','district','doctor','slots','operator'], 'identity_policy_confirm':['identity_name','district','doctor','slots','operator'],
 'identity_match_confirm':['identity_name','identity_birth','district','doctor','slots','operator'],
 'confirm':['identity_confirm','identity_name','menu','operator'], 'identity_name':['identity_birth','operator'],
 'identity_birth':['identity_readback','identity_phone','identity_name','identity_policy','district','doctor','slots','operator'], 'slots':['booking_confirm','operator'], 'booking_confirm':['done','slots','operator'],
}
MAX_SECONDS = 12
MAX_WAV_BYTES = MAX_SECONDS*48000*2+4096


@dataclass
class Call:
    owner: str
    expires: float
    state: str = 'menu'
    selection: dict | None = None
    transcript: list = field(default_factory=list)
    trace: list = field(default_factory=lambda:['menu'])
    attempts: int = 0
    consent: bool = False
    flags: list = field(default_factory=list)
    identity: IdentityForm = field(default_factory=IdentityForm)
    caller_phone: str = ''
    candidates: list = field(default_factory=list)
    policy_candidate: dict | None = None
    patient_name: str = ''
    name_offered: bool = False
    identity_misses: int = 0
    doctor: dict | None = None
    doctors: list = field(default_factory=list)
    district: str | None = None
    patient: dict | None = None
    offers: list = field(default_factory=list)
    slot: dict | None = None
    booking: dict | None = None
    queries: list = field(default_factory=list)
    key: str = ''
    analysis: bool = False
    registrar: str = '100'
    started: float = field(default_factory=time.monotonic)
    modules: dict = field(default_factory=dict)
    extensions: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class DemoCalls:
    def __init__(self,rpc=None,clock=None,limit=32,ttl=900,store=None,settings=None,tmp=None):
        self.rpc=rpc or module_call
        self.dictionary=SpecialtyDictionary()
        self.clock=clock or time.monotonic
        self.limit,self.ttl=limit,ttl
        self.calls={};self.lock=threading.Lock();self.voice_cache={}
        self._temporary = tempfile.TemporaryDirectory(prefix='clinic-unit-') if store is None else None
        self.store=store or SandboxStore(self._temporary.name)
        self.settings=settings or configured()
        self.dialogue_model=DialogueModel(self.settings)
        self.tmp=Path(tmp or tempfile.gettempdir())/'clinic-call-tmp'
        self.tmp.mkdir(parents=True,exist_ok=True)

    def request(self,route,post,data,query=None,client=''):
        prefix='/api/demo-call/'
        if not route.startswith(prefix):raise ValueError('Неизвестное действие пробного звонка')
        op=route[len(prefix):]
        if op=='catalog' and not post:
            return {'source':CATALOG_SOURCE,'items':[{'id':'therapist','label':'Участковый терапевт'},
                {'id':'specialist','label':'Врач-специалист','details':self.dictionary.read()['specialties']}]}
        if not post or op not in {'start','audio','text','dtmf','end','inspect'}:raise ValueError('Неизвестное действие пробного звонка')
        if op=='start':
            if type(data.get('consent_audio',False)) is not bool:raise ValueError('Укажите согласие на аудиозапись')
            with self.lock:
                now=self.clock()
                expired=[k for k,v in self.calls.items() if v.expires<=now]
                for k in expired:self.calls.pop(k)
                if len(self.calls)>=self.limit or sum(c.owner==client for c in self.calls.values())>=4:raise ValueError('Слишком много пробных звонков. Завершите предыдущий.')
                key=secrets.token_urlsafe(32)
                call=Call(client,now+self.ttl,caller_phone=phone_digits(data.get('caller_phone')),registrar=str(data.get('_line') or '100'),consent=data.get('consent_audio',False),key=key,modules=self.settings.read())
                if call.modules['scenario_extensions']['enabled']:
                    from .scenario_extensions import snapshot
                    call.extensions=snapshot(self.store,data.get('_line','100'))
                self.calls[key]=call
            try:
                with call.lock:return self._result(key,call,PROMPTS['menu'])
            except Exception:
                with self.lock:self.calls.pop(key,None)
                raise
        key=str(data.get('call_id',''))
        with self.lock:
            call=self.calls.get(key)
            if call is None or call.expires<=self.clock() or call.owner!=client:raise ValueError('Пробный звонок завершён. Начните новый.')
        if not call.lock.acquire(blocking=False):raise ValueError('Дождитесь ответа помощника')
        try:
            if op=='inspect':return self.document(call)
            if op=='end':
                self._persist(call)
                with self.lock:self.calls.pop(key,None)
                return {'call_id':key,'state':'demo.closed','title':TITLES['closed'],'sandbox':True,'executed':False,'archived':True}
            if call.state in TERMINAL:raise ValueError('Пробный звонок завершён. Начните новый.')
            if len(call.transcript)>=120:raise ValueError('Достигнут предел пробного разговора')
            digit=None
            if op=='audio':
                if not self.enabled(call,'asr'):raise ValueError('Распознавание речи отключено')
                pcm,rate=self._decode(data.get('wav_b64'))
                # Transient audio is always removed, including provider failures.
                with tempfile.NamedTemporaryFile(dir=self.tmp,suffix='.wav') as temp:
                    from .provider_adapter import wav_bytes
                    temp.write(wav_bytes(pcm,rate));temp.flush()
                    recognized=self.rpc('asr','transcribe',{'pcm_b64':base64.b64encode(pcm).decode(),'rate':rate})
                    text=str(recognized.get('text','')).strip()[:1000]
                    if not text:raise ValueError('Речь не распознана. Повторите или используйте клавиши.')
                    if call.consent:
                        folder=self.store.audio/key;folder.mkdir(exist_ok=True)
                        (folder/(str(len(call.transcript))+'.patient.wav')).write_bytes(Path(temp.name).read_bytes())
                source={'role':'patient','text':text,'source':'real_asr','engine':recognized.get('engine',''),
                        'provider':recognized.get('provider',''),'cuda_verified':recognized.get('cuda_verified',False)}
            elif op=='text':
                text=str(data.get('text','')).strip()
                if not 0<len(text)<=1000:raise ValueError('Введите фразу до 1000 символов')
                source={'role':'patient','text':text,'source':'typed_test'}
            else:
                digit=str(data.get('digit',''))
                if digit not in set('0123456789*#'):raise ValueError('Неизвестная клавиша')
                text='';source={'role':'dtmf','text':digit,'source':'dtmf'}
            source['state']='demo.'+call.state
            source['step_index']=len(call.trace)-1
            call.transcript.append(source)
            source_step=len(call.trace)-1
            prompt=self._advance(call,text,digit)
            seen={q['id'] for q in call.queries}
            call.queries.extend({**q,'step_index':source_step,'state':source['state']} for q in self.store.queries(key) if q['id'] not in seen)
            call.expires=self.clock()+self.ttl
            result=self._result(key,call,prompt)
            result['recognized']=source if op=='audio' else None
            return result
        finally:call.lock.release()
    @staticmethod
    def _decode(encoded, allow_silence=False):
        if not isinstance(encoded,str) or len(encoded) > (MAX_WAV_BYTES+2)//3*4:
            raise ValueError('Запишите не больше двенадцати секунд речи')
        try:
            raw = base64.b64decode(encoded,validate=True)
            if len(raw)>MAX_WAV_BYTES:raise ValueError('Слишком большая запись')
            with wave.open(io.BytesIO(raw),'rb') as wav:
                channels,width,rate,frames = wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes()
                if channels!=1 or width!=2 or wav.getcomptype()!='NONE' or rate not in {8000,16000,24000,48000}:
                    raise ValueError('Нужен моно WAV PCM16 с частотой 8, 16, 24 или 48 кГц')
                if not rate*(.001 if allow_silence else .15) <= frames <= rate*MAX_SECONDS:
                    raise ValueError('Запишите от одной секунды до двенадцати секунд речи')
                pcm = wav.readframes(frames)
                if len(pcm)!=frames*2:raise ValueError('Запись обрезана')
            values=array.array('h');values.frombytes(pcm)
            import sys
            if sys.byteorder!='little':values.byteswap()
            if not allow_silence and math.sqrt(sum(v*v for v in values)/len(values))<60:
                raise ValueError('Не слышно речи. Проверьте микрофон или используйте клавиши.')
            return pcm,rate
        except (wave.Error,EOFError,TypeError,ValueError) as exc:
            if isinstance(exc,ValueError) and str(exc).startswith(('Нужен','Запишите','Запись','Не слышно','Слишком')):raise
            raise ValueError('Не удалось прочитать WAV. Запишите фразу ещё раз.') from None

    @staticmethod
    def _move(call,state):
        call.state=state;call.trace.append(state);call.attempts=0
        if state=='menu':
            call.selection=None;call.offers=[];call.slot=None

    def enabled(self,call,module):
        return call.modules.get(module,{}).get('enabled',False) and self.settings.read().get(module,{}).get('enabled',False)

    def _choose(self,call,item):
        doc=self.dictionary.read()
        call.plan=compile_plan(doc['revision'],item['id'],item.get('referral_required'))
        call.dictionary_revision=doc['revision']
        call.selection=item
        call.patient=None;call.slot=None;call.offers=[];call.doctor=None;call.doctors=[];call.district=None
        if item.get('referral_required') is None:return self._handoff(call,'specialty_policy_not_configured')
        self._move(call,'referral' if any(n.name=='referral_lookup' for n in call.plan) else 'confirm')
        return self._prompt(call)

    def _back(self,call):
        if call.booking:return self._handoff(call,'change_after_completed_operation')
        previous={'identity_birth':'identity_name','doctor':'identity_birth','district':'identity_birth','identity_policy':'identity_name','identity_confirm':'confirm','slots':'doctor' if call.doctors else 'identity_birth','booking_confirm':'slots','identity_name':'confirm','confirm':'specialist','referral':'specialist','therapist_first':'referral'}
        target=previous.get(call.state,'menu')
        if call.state=='confirm' and call.selection and call.selection['id'] in {'therapist','pediatrician'}:target='menu'
        if target in {'identity_name','identity_birth'}:
            call.patient=None;call.slot=None;call.offers=[];call.doctor=None;call.doctors=[];call.district=None;call.candidates=[];call.policy_candidate=None
            if target=='identity_name':call.identity.reset();call.patient_name=''
            else:call.identity.birth_date=''
        self._move(call,target)
        return self._prompt(call)

    def _prompt(self,call):
        if call.state=='identity_name':return call.identity.question(self.dialogue_model if self.enabled(call,'dialogue') else None)
        if call.state=='identity_readback':return call.identity.readback()
        if call.state=='identity_match_confirm':
            candidate=call.policy_candidate
            from .identity_form import identity_readback
            return 'В картотеке найдены данные. '+identity_readback(candidate['full_name'],candidate['birth_date'])
        if call.state=='identity_phone':return 'По ФИО и дате рождения найдено несколько совпадений. Назовите номер телефона пациента, указанный в картотеке.'
        if call.state=='identity_policy_confirm':
            from calls.tts.verbalize import date_to_words
            p=call.policy_candidate
            return 'По полису найдены данные: '+p['full_name']+', дата рождения '+date_to_words(date.fromisoformat(p['birth_date']))+'. Это пациент, которого нужно записать? Да — один. Нет — ноль.'
        if call.state=='identity_confirm':return 'Вы уже назвали: '+call.patient_name+'. Это ФИО пациента? Да — один. Исправить — ноль.'
        if call.state=='doctor':return 'Выберите врача: '+'. '.join(f'{i+1}: {d["full_name"]}' for i,d in enumerate(call.doctors))+'. Назовите номер варианта или ФИО врача.'
        if call.state=='confirm':
            label=call.selection['label'].lower()
            words=label.split()
            if call.selection.get('id')=='diagnostic' or label in {'исследования','исследование или лабораторные анализы'}:target='на исследование или анализы'
            elif words[-1].endswith(('лог','евт','атр','ург')):
                words[-1]+='у'
                words=[w[:-2]+'ому' if w.endswith(('ый','ий')) else w for w in words]
                target='к '+' '.join(words)
            else:target='к врачу по специальности «'+label+'»'
            return 'Вы хотите записаться '+target+'? Да — один. Нет — ноль, переведу на оператора.'
        if call.analysis and call.state in {'slots','booking_confirm'}:
            return 'В разговоре обсуждается выбор времени.' if call.state=='slots' else 'В разговоре требуется подтверждение выбранного времени.'
        if call.state=='slots':
            return 'Доступные тестовые талоны. '+'. '.join(f'{i+1}: {s["doctor"]}, {self._spoken_slot(s)}' for i,s in enumerate(call.offers))+'. Назовите номер варианта. Для регистратора нажмите звёздочку.'
        if call.state=='booking_confirm':return 'Подтвердите запись: '+call.selection['label']+', '+call.slot['doctor']+', '+self._spoken_slot(call.slot)+'. Подтвердить — один. Выбрать другое время — ноль.'
        return PROMPTS[call.state]

    @staticmethod
    def _spoken_slot(slot):
        from calls.tts.verbalize import date_to_words,time_to_words
        starts=datetime.fromisoformat(slot['starts_at'])
        return date_to_words(starts.date())+', в '+time_to_words(starts.time())

    def _handoff(self,call,reason):
        call.flags.append(reason)
        self._move(call,'operator')
        return PROMPTS['operator']

    def _identity_lookup(self,call,phone_checked=False):
        call.patient=None
        if call.analysis:
            call.flags.append('identity_and_referral_not_verified')
            self._move(call,'slots');return self._prompt(call)
        if not call.identity.confirmed_readback:
            self._move(call,'identity_readback')
            return call.identity.readback()
        call.candidates=self.store.identity_candidates(call.key,call.identity.parts,birth=call.identity.birth_date)
        if len(call.candidates)==1:
            if call.caller_phone:
                owners=self.store.identity_candidates(call.key,{},phone=call.caller_phone)
                if owners and call.candidates[0]['id'] not in {p['id'] for p in owners}:
                    call.policy_candidate=call.candidates[0]
                    self._move(call,'identity_match_confirm');return self._prompt(call)
            if call.candidates[0]['full_name'].casefold()!=call.identity.full_name.casefold():
                call.policy_candidate=call.candidates[0]
                self._move(call,'identity_match_confirm');return self._prompt(call)
            call.patient=call.candidates[0];return self._availability(call)
        if not call.candidates:
            if call.caller_phone:
                owners=self.store.identity_candidates(call.key,{},birth=call.identity.birth_date,phone=call.caller_phone)
                if len(owners)==1 and near_phone_owner(call.identity.parts,owners[0]):
                    call.policy_candidate=owners[0];call.flags.append('asr_name_candidate_requires_confirmation')
                    self._move(call,'identity_match_confirm');return self._prompt(call)
            call.identity_misses+=1
            if call.identity_misses>=2:return self._handoff(call,'patient_not_found_after_retry')
            self._move(call,'identity_readback')
            return 'Карточку по этим данным найти не удалось. '+self._prompt(call)
        if call.caller_phone:
            narrowed=self.store.identity_candidates(call.key,call.identity.parts,birth=call.identity.birth_date,phone=call.caller_phone)
            if len(narrowed)==1:
                call.policy_candidate=narrowed[0]
                self._move(call,'identity_match_confirm');return self._prompt(call)
            if narrowed:call.candidates=narrowed
            self._move(call,'identity_policy');return self._prompt(call)
        self._move(call,'identity_policy' if phone_checked else 'identity_phone')
        return self._prompt(call)

    def _availability(self,call):
        if not self.enabled(call,'crm'):return self._handoff(call,'crm_disabled')
        if call.selection['referral_required'] and not self.store.has_referral(call.key,call.patient['id'],call.selection['id']):return self._handoff(call,'referral_not_verified')
        district=call.district or (None if self.store.source_path else call.patient.get('district'))
        if call.selection['id'] in {'therapist','pediatrician'} and not district:
            self._move(call,'district');return self._prompt(call)
        call.doctors=self.store.doctors(call.key,call.selection['id'],district if call.selection['id'] in {'therapist','pediatrician'} else None)
        if len(call.doctors)>1:
            self._move(call,'doctor');return self._prompt(call)
        if len(call.doctors)==1:call.doctor=call.doctors[0]
        elif call.selection['id'] in {'therapist','pediatrician'}:
            return ('В исходной выгрузке нет связи этого участка с врачом. Уточнить её поможет регистратор. ' if self.store.source_path else '')+self._handoff(call,'district_doctor_not_found')
        elif self.store.source_path:return self._handoff(call,'specialty_doctor_not_found_in_source')
        return self._offer_slots(call)

    def _offer_slots(self,call):
        preference=self.store.saved_preference(call.patient['id']) if self.enabled(call,'preferences') else None
        call.offers=self.store.slots(call.key,call.selection['id'],preference,call.doctor['full_name'] if call.doctor else None)
        if not call.offers:return self._handoff(call,'no_slots')
        self._move(call,'slots');return self._prompt(call)

    def _advance(self,call,text,digit):
        low=re.sub(r'[^а-яё0-9\s]',' ',text.lower()).replace('ё','е').strip()
        low=re.sub(r'\s+',' ',low)
        if text and foreign_language_request(text):return self._handoff(call,'language_unsupported')
        correction=re.match(r'^\s*нет[, .!]*\s*(?:я\s+|меня зовут\s+)(.+)',text,re.I)
        if correction and call.state.startswith('identity_'):
            call.identity.reset();call.patient=None;call.patient_name='';call.slot=None;call.offers=[];call.candidates=[];call.policy_candidate=None
            call.identity.accept_name(correction[1],self.dialogue_model if self.enabled(call,'dialogue') else None)
            call.patient_name=call.identity.full_name
            self._move(call,'identity_name' if call.identity.missing else 'identity_birth')
            return self._prompt(call)
        meaning=None
        yes_no=None
        if text:
            if not self.enabled(call,'nlu'):return self._handoff(call,'nlu_disabled')
            context={'text':text,'today':clinic_now().date().isoformat(),'state':call.state,'identity_field':'birth_date' if call.state=='identity_birth' else 'full_name' if call.state=='identity_name' else None}
            try:result=self.rpc('nlu','interpret',context)
            except ValueError:return self._handoff(call,'nlu_unavailable')
            meaning=result.get('semantic')
            if not meaning:raise ValueError('Модуль понимания речи не вернул семантический результат')
            call.transcript[-1]['interpretation']=result
            intent=result.get('intent','unknown');yes_no=result.get('yes_no')
            if intent=='record_opt_out':
                import shutil
                call.consent=False;shutil.rmtree(self.store.audio/call.key,ignore_errors=True)
                return 'Хорошо, аудиозапись разговора отключена. '+self._prompt(call)
            if intent=='self_identity':return 'Я виртуальный помощник поликлиники. '+self._prompt(call)
            if intent in {'home_visit','results','paid','waitlist','attachment'}:return self._handoff(call,'scenario_integration_not_enabled:'+str(result.get('source_scenario')))
            if intent=='emergency':call.flags.append('emergency');self._move(call,'emergency');return PROMPTS['emergency']
            if intent in {'operator','medical','complaint'}:return self._handoff(call,'abusive_language' if 'abusive_language' in result.get('flags',[]) else 'human_only_'+intent)
            if intent=='identity_question' and call.state.startswith('identity_'):
                return 'Назовите данные пациента, которого нужно записать. '+self._prompt(call)
            if intent=='already_said':
                return ('Сохранил названное ФИО: '+call.patient_name+'. ' if call.patient_name else 'Не удалось надёжно выделить ФИО пациента из предыдущей фразы. ')+self._prompt(call)
            if call.state in {'menu','specialist'}:
                # Capture structured data without treating it as verified identity.
                match=re.search(r"(?<![А-Яа-яЁё])([А-ЯЁ][а-яё'-]+)\s+([А-ЯЁ][а-яё'-]+)\s+([А-ЯЁ][а-яё'-]+)(?![А-Яа-яЁё])",text)
                if match and any(w.casefold().endswith(('вич','вна','ична','оглы')) for w in match.groups()):
                    words=list(match.groups())
                    if words[1].casefold().endswith(('вич','вна','ична','оглы')):words=[words[2],words[0],words[1]]
                    call.patient_name=' '.join(words);call.name_offered=True
            if intent in {'greeting','repeat'}:return self._prompt(call)
            if intent=='back':
                return self._back(call)
            if intent in {'cancel','reschedule','my_booking'}:return self._handoff(call,'existing_booking_request')
            specialty=meaning.get('specialty',{})
            if specialty.get('status')=='ambiguous' and call.state in {'menu','specialist'}:
                return 'Вы назвали несколько специальностей. Уточните, к какому врачу нужна запись.'
            if specialty.get('status')=='candidate' and call.state in {'menu','specialist'}:
                item=next((x for x in self.dictionary.read()['specialties'] if x['id']==specialty['id']),None)
                if item:
                    call.candidate=item
                    return 'Вы имеете в виду '+item['label']+'? Подтвердите или назовите иначе.'
            if getattr(call,'candidate',None) and yes_no is True:
                item=call.candidate;call.candidate=None
                return self._choose(call,item)
            if getattr(call,'candidate',None) and yes_no is False:call.candidate=None;return self._prompt(call)
            if specialty.get('status')=='resolved' and call.state in {'menu','specialist'}:
                item=self.dictionary.policy(specialty['id'])
                if result.get('vars',{}).get('patient_group')=='child':item={**item,'label':'Детский '+item['label'].lower(),'patient_group':'child'}
                return self._choose(call,item)
        if getattr(call,'candidate',None) and digit=='1':
            item=call.candidate;call.candidate=None;return self._choose(call,item)
        if digit=='*':return self._handoff(call,'requested_operator')
        if digit=='#':return self._back(call)
        if self.enabled(call,'scenario_extensions'):
            for version in sorted(call.extensions,key=lambda v:v['scope']=='global'):
                for event in version['events']:
                    if event['state']==call.state and event['phrase'].casefold().strip()==text.casefold().strip():
                        return event['response']+' '+self._prompt(call)
        if digit is None:
            digit={'один':'1','одна':'1','первый':'1','первое':'1','два':'2','две':'2','второй':'2','три':'3','третий':'3','ноль':'0'}.get(low)
            if low in set('0123456789'):digit=low
            if digit is None and call.state in {'slots','doctor'}:
                choice=re.fullmatch(r'(?:вариант\s+)?(первый|второй|третий|один|два|три|[123])(?:\s+вариант)?[.!?]*',low)
                if choice:
                    digit={'первый':'1','второй':'2','третий':'3','один':'1','два':'2','три':'3'}.get(choice[1],choice[1])
        if call.state=='specialist' and digit=='0':return self._handoff(call,'requested_operator')
        if call.state=='identity_confirm':
            if digit=='1' or yes_no is True:
                call.identity.accept_name(call.patient_name)
                self._move(call,'identity_birth' if not call.identity.missing else 'identity_name');return self._prompt(call)
            if digit=='0' or yes_no is False:call.patient_name='';call.name_offered=False;call.identity.reset();self._move(call,'identity_name');return self._prompt(call)
        elif call.state=='identity_name':
            if call.identity.accept_name(text,self.dialogue_model if self.enabled(call,'dialogue') else None,today=clinic_now().date()):
                call.patient_name=call.identity.full_name
                if not call.analysis and self.enabled(call,'crm') and call.identity.parts:
                    call.candidates=self.store.identity_candidates(call.key,call.identity.parts)
                # Even one preliminary candidate never completes missing fields.
                if not call.identity.missing and not call.identity.pending and call.identity.birth_date:
                    return self._identity_lookup(call)
                self._move(call,'identity_name' if call.identity.missing or call.identity.pending else 'identity_birth')
                return self._prompt(call)
        elif call.state=='identity_birth':
            from .identity import birth_date_input
            parsed=birth_date_input(text,today=clinic_now().date())
            call.transcript[-1]['date_field']=parsed
            if parsed['status']!='valid':return parsed['message']
            call.identity.birth_date=parsed['value']
            if call.analysis:
                call.flags.append('identity_and_referral_not_verified')
                self._move(call,'slots');return self._prompt(call)
            if not self.enabled(call,'crm'):return self._handoff(call,'crm_disabled')
            if call.identity.missing:
                self._move(call,'identity_name');return self._prompt(call)
            return self._identity_lookup(call)
        elif call.state=='identity_match_confirm':
            if digit=='1' or yes_no is True:
                call.patient=call.policy_candidate;return self._availability(call)
            if digit=='0' or yes_no is False:
                call.identity.reset();call.patient=None;call.patient_name=''
                self._move(call,'identity_name');return self._prompt(call)
        elif call.state=='identity_readback':
            if digit=='1' or yes_no is True:
                if not call.identity.confirmed_readback:
                    call.identity.confirmed_readback=True
                    return self._identity_lookup(call)
                self._move(call,'identity_policy');return self._prompt(call)
            if digit=='0' or yes_no is False:
                call.identity.reset();call.patient_name='';call.patient=None
                self._move(call,'identity_name');return self._prompt(call)
            # An actual correction is data, not an unsuccessful yes/no answer.
            old=call.identity.requested;call.identity.requested='last_name'
            if call.identity.accept_name(text,self.dialogue_model if self.enabled(call,'dialogue') else None,today=clinic_now().date()):
                call.patient_name=call.identity.full_name
                if not call.identity.missing and call.identity.birth_date:return self._identity_lookup(call)
                self._move(call,'identity_birth' if not call.identity.missing else 'identity_name');return self._prompt(call)
            call.identity.requested=old
        elif call.state=='identity_phone':
            from .identity import normalize_identifier
            try:number=normalize_identifier(text,'phone')
            except ValueError:number=''
            if number:
                call.caller_phone=phone_digits(number)
                return self._identity_lookup(call,phone_checked=True)
        elif call.state=='identity_policy':
            from .identity import DIGIT_WORDS
            policy=''.join(DIGIT_WORDS.get(w,w) for w in re.sub(r'[.,-]',' ',text.casefold()).split())
            if re.fullmatch(r'[0-9]{4,16}',policy):
                if not self.enabled(call,'crm'):return self._handoff(call,'crm_disabled')
                if not call.identity.birth_date or call.identity.missing:return self._handoff(call,'identity_incomplete')
                matches=self.store.identity_candidates(call.key,call.identity.parts if call.candidates else {},
                    birth=call.identity.birth_date,policy=policy,cohort=[p['id'] for p in call.candidates] if call.candidates else None)
                if len(matches)==1:
                    if call.candidates:
                        call.patient=matches[0];return self._availability(call)
                    call.policy_candidate=matches[0]
                    self._move(call,'identity_policy_confirm');return self._prompt(call)
                return self._handoff(call,'ambiguous_identity_after_oms' if matches else 'patient_not_found_after_oms')
        elif call.state=='identity_policy_confirm':
            if digit=='1' or yes_no is True:
                call.patient=call.policy_candidate
                return self._availability(call)
            if digit=='0' or yes_no is False:return self._handoff(call,'identity_oms_not_confirmed')
        elif call.state=='district':
            if digit and digit.isdigit():number=digit
            else:
                from calls.nlu.normalize import numberize,Num
                values=numberize(text.lower().strip(' .!').split())
                number=str(values[0].value) if len(values)==1 and isinstance(values[0],Num) else None
            if number and int(number)>0:call.district=number;return self._availability(call)
        elif call.state=='doctor':
            selected=call.doctors[int(digit)-1] if digit and digit.isdigit() and 1<=int(digit)<=len(call.doctors) else next((d for d in call.doctors if d['full_name'].casefold().replace('ё','е')==low),None)
            if selected:call.doctor=selected;return self._offer_slots(call)
        elif call.state=='slots':
            if call.analysis and (digit in {'1','2','3'} or yes_no is True or re.search(r'\b\d{1,2}[:.]\d{2}\b|утр|вечер|завтра',low)):
                self._move(call,'booking_confirm');return self._prompt(call)
            if digit and digit.isdigit() and 1<=int(digit)<=len(call.offers):
                call.slot=call.offers[int(digit)-1];self._move(call,'booking_confirm');return self._prompt(call)
            if not call.analysis and self.enabled(call,'preferences') and call.patient and re.search(r'предпочитаю|мне удобно',low):
                preference=next((value for word,value in [('утр','morning'),('после обеда','afternoon'),('днем','afternoon'),('вечер','evening')] if word in low),None)
                if preference:
                    self.store.preference(call.patient['id'],preference)
                    call.offers=self.store.slots(call.key,call.selection['id'],preference)
                    return 'Предпочтение сохранено. '+(self._prompt(call) if call.offers else self._handoff(call,'no_slots_for_preference'))
        elif call.state=='booking_confirm':
            if digit=='0' or yes_no is False:self._move(call,'slots');return self._prompt(call)
            if digit=='1' or yes_no is True:
                if call.analysis:
                    call.flags.append('booking_status_not_verified')
                    self._move(call,'done');return 'В разговоре прозвучало подтверждение. Результат в МИС не проверен.'
                if not self.enabled(call,'crm'):return self._handoff(call,'crm_disabled')
                policy=self.dictionary.policy(call.selection['id'])
                if not policy or policy.get('referral_required') is None:return self._handoff(call,'specialty_policy_not_configured')
                required=policy['referral_required'] or call.selection['referral_required']
                try:call.booking=self.store.book(call.key,call.patient['id'],call.slot['id'],call.selection['id'],required)
                except ValueError as exc:
                    call.flags.append('booking_rejected');self._move(call,'slots');call.offers=self.store.slots(call.key,call.selection['id'])
                    return str(exc)+' '+(self._prompt(call) if call.offers else self._handoff(call,'no_slots'))
                self._move(call,'done');return PROMPTS['done']
        elif call.state in {'referral','confirm','therapist_first'}:
            if digit=='2':return self._handoff(call,'requested_operator')
            if digit=='1' or (digit is None and yes_no is True):
                if call.state=='confirm':self._move(call,'identity_confirm' if call.name_offered else 'identity_name');return self._prompt(call)
                if call.state=='therapist_first':
                    child=bool(call.selection and call.selection.get('patient_group')=='child')
                    return self._choose(call,{'id':'pediatrician' if child else 'therapist','label':'Участковый педиатр' if child else 'Участковый терапевт','referral_required':False})
                self._move(call,'confirm');return self._prompt(call)
            if digit=='0' or (digit is None and yes_no is False):
                if call.state=='confirm':return self._handoff(call,'specialty_not_confirmed')
                self._move(call,'therapist_first' if call.state=='referral' else 'menu');return self._prompt(call)
        else:
            if call.state=='menu' and digit in {'1','3'}:
                item=self.dictionary.policy('therapist' if digit=='1' else 'diagnostic')
                return self._choose(call,item)
            if call.state=='menu' and digit=='2':
                self._move(call,'specialist');return PROMPTS['specialist']
        call.attempts+=1
        if call.attempts>=3:return self._handoff(call,'unrecognized_repeatedly')
        return 'Не удалось понять ответ. '+self._prompt(call)

    def document(self,call):
        titles={**TITLES,**({'done':'Подтверждение со слов участников'} if call.analysis else {})}
        return {'call_id':call.key,'registrar':call.registrar,'duration_s':max(0,time.monotonic()-call.started),'sandbox':True,'executed':False,'sandbox_booking':call.booking,'state':'demo.'+call.state,
            'title':titles[call.state],'selection':call.selection,'prompt':call.transcript[-1]['text'] if call.transcript else '',
            'transcript':list(call.transcript),'trace':['demo.'+s for s in call.trace],
            'steps':[{'id':'demo.'+s,'title':titles[s]} for s in call.trace],
            'graph':{'version':POLICY_VERSION,'titles':titles,'branches':BRANCHES},'flags':list(call.flags),
            'consent_audio':call.consent,'finished':call.state in TERMINAL,'catalog_source':CATALOG_SOURCE,
            'source_data':self.store.source_info(),'queries':list(call.queries),'doctor':call.doctor,'district':call.district,
            'offers':list(call.offers),'patient':call.patient,'analysis':call.analysis,'modules':call.modules,
            'extensions':call.extensions,'dictionary_revision':getattr(call,'dictionary_revision',None),'compiled_plan':[vars(n) for n in getattr(call,'plan',())],
            'identity_form':{'parts':dict(call.identity.parts),'birth_date':call.identity.birth_date,'missing_fields':call.identity.missing+([] if call.identity.birth_date else ['birth_date']),'requested_field':call.identity.requested,'candidate_count':len(call.candidates),'caller_phone_available':bool(call.caller_phone)},
            'audio_files':[p.name for p in sorted((self.store.audio/call.key).glob('*.wav'))] if call.consent else [],
            'proposed_action':'Создана запись только в тестовой SQLite' if call.booking else 'Запись ещё не создана'}

    def _persist(self,call):
        document=self.document(call)
        self.store.save_call(call.key,call.owner,document)
        if self.enabled(call,'client_tags') and call.patient and call.flags:
            self.store.tag(call.patient['id'],call.flags)

    def _result(self,key,call,prompt):
        call.transcript.append({'role':'assistant','text':prompt,'state':'demo.'+call.state,'step_index':len(call.trace)-1,'source':'scenario'})
        # Save the actual dialogue even if synthesis fails.
        self._persist(call)
        result=self.document(call)
        if self.enabled(call,'tts'):
            try:
                voice=self.rpc('tts','preview',{'text':prompt,'rate':1.0})
                if voice.get('sample_rate')!=48000 or not voice.get('pcm_b64'):raise ValueError('Модуль не вернул речь в качестве сорок восемь килогерц')
                result['audio']=voice
                if call.consent:
                    from .provider_adapter import wav_bytes
                    try:
                        pcm=base64.b64decode(voice['pcm_b64'],validate=True)
                        folder=self.store.audio/key;folder.mkdir(exist_ok=True)
                        (folder/(str(len(call.transcript))+'.assistant.wav')).write_bytes(wav_bytes(pcm,48000))
                    except ValueError: pass
                    self._persist(call)
                    result['audio_files']=self.document(call)['audio_files']
            except ValueError as exc:result['audio_error']=str(exc)
        return result


def speech_phrases():
    demo=DemoCalls();phrases=set(PROMPTS.values())
    for label in ['Участковый терапевт','Участковый педиатр','Исследование или лабораторные анализы']+[v['label'] for v in SpecialtyDictionary().read()['specialties']]:
        phrases.add(demo._prompt(Call('',0,state='confirm',selection={'label':label})))
    return sorted(phrases)
