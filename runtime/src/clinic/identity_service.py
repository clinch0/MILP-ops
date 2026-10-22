"""Telephony adapter for the required-field identity form and local SQL mirror."""
import re
import threading
import time

from .identity_form import (IdentityForm,DialogueModel,NAME_FIELDS,QUESTIONS,
                           candidate_query,folded,phone_digits,foreign_language_request,near_phone_owner)
from .identity import birth_date_input,normalize_identifier
from .module_settings import configured


class IdentityService:
    def __init__(self,registry,today):
        self.reg,self.today=registry,today
        self.sessions={};self.lock=threading.RLock()
        self.model=DialogueModel(configured())
        with self.reg._lock,self.reg._conn:
            columns={r['name'] for r in self.reg._conn.execute('PRAGMA table_info(patients)')}
            if 'oms' not in columns:self.reg._conn.execute('ALTER TABLE patients ADD COLUMN oms TEXT')
            self.reg._conn.create_function('clinic_name',1,folded,deterministic=True)
            self.reg._conn.create_function('clinic_phone',1,phone_digits,deterministic=True)
            self.reg._conn.execute('''CREATE VIEW IF NOT EXISTS identity_people AS
SELECT id,trim(COALESCE(last_name,'')||' '||COALESCE(first_name,'')||' '||COALESCE(middle_name,'')) AS full_name,
last_name,first_name,middle_name,birth_date,phone,oms,NULL AS district FROM patients''')

    def query(self,call_id,sql,params):
        rows=[dict(r) for r in self.reg._q(sql,params)]
        self.sessions[call_id]['queries'].append({'sql':sql,'params':list(params),'result':rows})
        return rows

    def candidates(self,key,parts,**kwargs):
        return candidate_query(self.query,key,parts,**kwargs)

    def start(self,call_id,caller_phone='',locked_patient_id=None,**_):
        with self.lock:
            now=time.monotonic()
            self.sessions={k:v for k,v in self.sessions.items() if now-v['updated']<3600}
            if len(self.sessions)>=1000:return 'error',{'error':'identity_capacity'}
            self.sessions[call_id]={'lock':threading.RLock(),'form':IdentityForm(),'stage':'name','phone':phone_digits(caller_phone),
                'locked':locked_patient_id,'tries':0,'invalids':0,'candidates':[],'pending':None,'policy':'','policy_first':False,'queries':[],'updated':now}
            return self.reply(call_id,'name',QUESTIONS['full_name']+' Для полиса ОМС нажмите два. Для номера карты нажмите три.')

    def use_card(self,call_id,**_):
        with self.lock:
            if call_id not in self.sessions:return 'error',{'error':'identity_not_started'}
            self.sessions[call_id]['card_first']=True
            self.sessions[call_id]['policy_first']=False
            return self.reply(call_id,'card','Введите номер карты и нажмите решётку. Затем уточню дату рождения.')

    def use_policy(self,call_id,**_):
        with self.lock:
            if call_id not in self.sessions:return 'error',{'error':'identity_not_started'}
            self.sessions[call_id]['policy_first']=True
            self.sessions[call_id]['card_first']=False
            return self.reply(call_id,'policy','Назовите номер полиса ОМС полностью: шестнадцать цифр. Затем уточню дату рождения.')

    def reply(self,key,stage,question='',**extras):
        session=self.sessions[key];session['stage']=stage
        form=session['form']
        return stage,{'identity_question':question,'identity_requested':form.requested,
            'identity_missing':form.missing+([] if form.birth_date else ['birth_date']),
            'identity_parts':dict(form.parts),'identity_birth_date':form.birth_date,
            'identity_readback_confirmed':form.confirmed_readback,
            'identity_queries':list(session['queries']),**extras}

    def verified(self,key,row):
        session=self.sessions[key]
        if session['locked'] and int(session['locked'])!=row['id']:
            return self.reply(key,'operator',reason='identity_conflict')
        if not session['form'].birth_date:return self.reply(key,'operator',reason='birth_date_required')
        return self.reply(key,'ok',patient_id=row['id'],patient_full_name=row['full_name'],
            patient_name=' '.join(row['full_name'].split()[1:]),patient_birth_date=row['birth_date'])

    def confirm_candidate(self,key,row):
        """An approximate spelling or another phone owner is never auto-verified."""
        self.sessions[key]['pending']=row
        from .identity_form import identity_readback
        return self.reply(key,'match_confirm','В картотеке найдены данные. '+identity_readback(row['full_name'],row['birth_date']))

    def lookup(self,key):
        session=self.sessions[key];form=session['form']
        if form.missing:return self.reply(key,'name',form.question(self.model))
        if not form.birth_date:return self.reply(key,'birth',QUESTIONS['birth_date'])
        if not form.confirmed_readback:return self.reply(key,'readback',form.readback())
        rows=self.candidates(key,form.parts,birth=form.birth_date)
        session['candidates']=rows
        if len(rows)==1:
            if session['phone']:
                owners=self.candidates(key,{},phone=session['phone'])
                if owners and rows[0]['id'] not in {r['id'] for r in owners}:
                    session['pending']=rows[0]
                    return self.confirm_candidate(key, rows[0])
            if rows[0]['full_name'].casefold() != form.full_name.casefold():
                return self.confirm_candidate(key, rows[0])
            return self.verified(key,rows[0])
        if not rows:
            if session['phone']:
                owners=self.candidates(key,{},birth=form.birth_date,phone=session['phone'])
                if len(owners)==1 and near_phone_owner(form.parts,owners[0]):
                    return self.confirm_candidate(key, owners[0])
            session['tries']+=1
            if session['tries']>=2:return self.reply(key,'operator',reason='patient_not_found_after_retry')
            return self.reply(key,'readback','Карточку по этим данным найти не удалось. '+form.readback())
        if session['phone']:
            narrowed=self.candidates(key,form.parts,birth=form.birth_date,phone=session['phone'])
            if len(narrowed)==1:return self.confirm_candidate(key,narrowed[0])
            if narrowed:session['candidates']=narrowed
            return self.reply(key,'policy',QUESTIONS['oms'])
        return self.reply(key,'phone',QUESTIONS['phone'])

    def answer(self,call_id,text='',**_):
        with self.lock:
            session=self.sessions.get(call_id)
        if session is None:return 'error',{'error':'identity_not_started'}
        with session['lock']:
            session=self.sessions[call_id];session['updated']=time.monotonic()
            form=session['form'];stage=session['stage'];text=str(text or '')[:1000]
            if foreign_language_request(text):return self.reply(call_id,'operator',reason='language_unsupported')
            correction=re.match(r'^\s*нет[, .!]*\s*(?:я\s+|меня зовут\s+)(.+)',text,re.I)
            if correction:
                session['card_first']=False
                form.reset();session['pending']=None;session['candidates']=[];session['policy']='';session['policy_first']=False;stage='name';text=correction[1]
            low=folded(text).strip(' .!')
            if stage in {'match_confirm','readback','policy_confirm'}:
                from calls.nlu.normalize import parse_yes_no
                yes=parse_yes_no(low)
                if low=='1':yes=True
                if low=='0':yes=False
                if yes is True:
                    if stage in {'match_confirm','policy_confirm'}:return self.verified(call_id,session['pending'])
                    if not form.confirmed_readback:
                        form.confirmed_readback=True
                        return self.lookup(call_id)
                    return self.reply(call_id,'policy',QUESTIONS['oms'])
                if yes is False:
                    session['card_first']=False
                    form.reset();session['policy']='';session['policy_first']=False
                    return self.reply(call_id,'name',form.question(self.model))
                if stage=='readback':form.requested='last_name';stage='name'
            if stage=='name':
                if not form.accept_name(text,self.model,today=self.today()):
                    session['invalids']+=1
                    if session['invalids']>=3:return self.reply(call_id,'operator',reason='identity_name_unclear')
                    return self.reply(call_id,'name',form.question(self.model))
                session['invalids']=0
                if form.parts:self.candidates(call_id,form.parts)
                if form.missing or form.pending:return self.reply(call_id,'name',form.question(self.model))
                return self.lookup(call_id) if form.birth_date else self.reply(call_id,'birth',QUESTIONS['birth_date'])
            if stage=='birth':
                parsed=birth_date_input(text,today=self.today())
                if parsed['status']!='valid':return self.reply(call_id,'birth',parsed['message'])
                form.birth_date=parsed['value']
                if session.get('card_first'):
                    rows=[dict(r) for r in self.reg._q('SELECT id FROM patients WHERE card_no=? AND birth_date=?',(session['card'],form.birth_date))]
                    if len(rows)!=1:return self.reply(call_id,'operator',reason='identity_card_not_unique' if rows else 'patient_not_found_after_card')
                    row=self.candidates(call_id,{},birth=form.birth_date,cohort=[rows[0]['id']])[0]
                    session['pending']=row
                    return self.confirm_candidate(call_id,row)
                if session['policy_first']:
                    return self.policy_lookup(call_id,session['policy'])
                return self.lookup(call_id)
            if stage=='card':
                try:session['card']=normalize_identifier(text,'card_no')
                except ValueError:return self.reply(call_id,'card','Введите номер карты и нажмите решётку.')
                return self.reply(call_id,'birth',QUESTIONS['birth_date'])
            if stage=='phone':
                try:session['phone']=phone_digits(normalize_identifier(text,'phone'))
                except ValueError:return self.reply(call_id,'phone',QUESTIONS['phone'])
                # Query within the exact FIO + DOB cohort, never phone alone.
                return self.lookup(call_id)
            if stage=='policy':
                try:policy=normalize_identifier(text,'oms')
                except ValueError:return self.reply(call_id,'policy',QUESTIONS['oms'])
                if session['policy_first'] and not form.birth_date:
                    if len(policy)!=16:return self.reply(call_id,'policy','Назовите полис ОМС полностью: шестнадцать цифр.')
                    session['policy']=policy
                    return self.reply(call_id,'birth',QUESTIONS['birth_date'])
                return self.policy_lookup(call_id,policy)
            return self.reply(call_id,stage,form.readback() if stage in {'readback','match_confirm'} else QUESTIONS.get(stage,QUESTIONS['full_name']))

    def policy_lookup(self,key,policy):
        session=self.sessions[key];form=session['form']
        if not form.birth_date:return self.reply(key,'operator',reason='birth_date_required')
        rows=self.candidates(key,form.parts if session['candidates'] else {},birth=form.birth_date,policy=policy,
            cohort=[r['id'] for r in session['candidates']] if session['candidates'] else None)
        if len(rows)!=1:return self.reply(key,'operator',reason='identity_oms_not_unique' if rows else 'patient_not_found_after_oms')
        if session['candidates']:return self.verified(key,rows[0])
        session['pending']=rows[0]
        from calls.tts.verbalize import date_to_words
        from datetime import date
        return self.reply(key,'policy_confirm','По полису найдены данные: '+rows[0]['full_name']+', дата рождения '+date_to_words(date.fromisoformat(rows[0]['birth_date']), with_year=True, with_weekday=False)+'. Это пациент, которого нужно записать? Да — один. Нет — ноль.')
