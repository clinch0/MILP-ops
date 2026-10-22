from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from datetime import date

from calls.nlu.agent import TwoAgentNLU
from calls.nlu.embed import (
    EmbeddingIntentClassifier,
    HashEmbedder,
    SentenceTransformerEmbedder,
)
from calls.nlu.intent import HUMAN_ONLY, classify
from calls.nlu.normalize import interpret, parse_yes_no, tokenize
from calls.services.netutil import open_http, post_json

from app.safety.detector import SafetyLayer
from app.types import SafetyVerdict


class Nlu:
    def __init__(self, model=None):
        model = model or os.environ.get("NLU_EMBEDDER", "hash")
        self.semantic = None
        if model == "e5":
            from .semantic import SemanticInterpreter
            self.semantic = SemanticInterpreter()
        embed = (
            SentenceTransformerEmbedder(
                os.environ.get("NLU_MODEL", "deepvk/USER-bge-m3")
            )
            if model == "bge"
            else HashEmbedder()
        )
        self.agent = TwoAgentNLU(embed=EmbeddingIntentClassifier(embed).fit())
        self.safety = SafetyLayer()

    def handle(self, op, args):
        if op != "interpret":
            return "error", {"error": "unknown NLU operation"}
        text = str(args.get("text", ""))[:4096]
        today = date.fromisoformat(args["today"]) if args.get("today") else date.today()
        it = interpret(text, today)
        rules = classify(text, today)
        verified = self.agent.decide(text, today, have=args.get("have", ()))
        safety = self.safety.check(text)
        safety_intent = {
            SafetyVerdict.RED_FLAG_A: "emergency",
            SafetyVerdict.RED_FLAG_B: "emergency",
            SafetyVerdict.MEDICAL: "medical",
            SafetyVerdict.COMPLAINT: "complaint",
            SafetyVerdict.OPERATOR: "operator",
        }.get(safety.verdict)
        meaning = self.semantic.interpret(text) if self.semantic else None
        if meaning:
            ranked=dict(meaning['alternatives'])
            clinical=ranked.get('medical',0)>=.90 and ranked.get('medical',0)>=meaning['confidence']-.015
            if safety_intent=='medical' and not clinical:safety_intent=None
            if safety_intent=='operator' and meaning['intent'] not in {'operator','abuse'}:safety_intent=None
            if not safety_intent and clinical and not meaning.get('entity_only'):safety_intent='medical'
        intent = safety_intent or (
            rules.intent if rules.intent in HUMAN_ONLY else verified.intent
        )
        confidence = 1.0 if safety_intent else verified.confidence
        if meaning and not safety_intent:
            intent,confidence = meaning["intent"],meaning["confidence"]
        # The generic semantic model can rank this exact operational request as
        # unknown. Resolve only an unambiguous complete command; the dialogue
        # still verifies identity, selects a real slot and asks for consent.
        if not safety_intent and re.fullmatch(
            r"\s*(?:пожалуйста[,\s]+)?(?:(?:хочу|нужно|надо|можно)\s+)?"
            r"перен(?:ести|есите|есем|есём)\s+(?:мою\s+)?(?:запись|при[её]м)"
            r"(?:[.!?,\s]|пожалуйста)*", text.lower()
        ):
            intent,confidence = 'reschedule',1.0
        # Disagreement can request a person; no speculative write is executed by an embedding.
        if not meaning and verified.action == "operator" and intent not in HUMAN_ONLY:
            intent = "operator"
        if (
            not meaning and verified.action == "clarify"
            and verified.reasons
            and any("disagree" in x for x in verified.reasons)
        ):
            intent = "operator"
        # A real ASR run confused «перенесите» with «принесите». Do not
        # silently turn the ambiguous result into a new appointment.
        if re.search(r"\bпринес\w*\b", text.lower()) and re.search(
            r"\bзапис\w*\b", text.lower()
        ):
            intent = "operator"
        if not safety_intent and re.search(
            r"\bне\s+(?:(?:надо|нужно|хочу)\s+)?(?:подтвержд|подтверд|отмен|перенес|перенос|запис)",
            text.lower(),
        ):
            intent = "operator"
        if not safety_intent and re.search(
            r"(?:автоответчик|голосов[а-я]+\s+почт|оставьте\s+сообщение|"
            r"абонент\s+(?:временно\s+)?недоступен|помощник\s+т[ -]?банка|"
            r"я\s+(?:ии|виртуальный)\s+(?:помощник|ассистент))",
            text.lower(),
        ):
            intent = "voicemail"
        # «Да, отмените» is affirmative only at the cancellation confirmation.
        # Keep ordinary yes_no conservative in all other dialogue states.
        affirmative_action = parse_yes_no(
            " ".join(
                t
                for t in tokenize(text)
                if t not in {"отмена", "отменить", "отмените", "отменяю"}
            )
        )
        if any(t in {"не", "нет", "нельзя"} for t in tokenize(text)):
            affirmative_action = False
        if not safety_intent and re.fullmatch(
            r"\s*(?:спасибо(?: большое)?|всего доброго|до свидания)[.! ]*", text.lower()
        ):
            intent = "gratitude"
            confidence = 1.0
        flags=[]
        if intent=="abuse":intent="operator";flags.append("abusive_language")
        identity_field = args.get("identity_field")
        identity_context = identity_field in {"birth_date", "card_no", "snils", "phone"}
        date_field = None
        digits = it.digits
        if identity_context:
            from .identity import birth_date_input, normalize_birth_date, normalize_identifier
            if identity_field=="birth_date":date_field=birth_date_input(text,today=today)
            try:
                digits = (normalize_birth_date(text, today=today).strftime("%d%m%Y")
                          if identity_field == "birth_date" else normalize_identifier(text, identity_field))
            except ValueError:
                # Never reuse a partial number or guess a year for identification.
                digits = ""
        if meaning and identity_context and digits and not safety_intent:
            intent='unknown'
        if meaning and args.get('identity_field')=='full_name' and re.fullmatch(r"[А-Яа-яЁё'-]+(?:\s+[А-Яа-яЁё'-]+){2}",text.strip(' .!')) and confidence<.94 and not safety_intent:
            intent='unknown'
        semantic_yes=it.yes_no
        ent = rules.entities
        vars = {}
        if meaning:
            specialty=meaning['specialty']
            from .specialty_dictionary import SpecialtyDictionary
            items={x['id']:x for x in self.semantic.dictionary.read()['specialties']}
            if specialty['status'] in {'resolved','candidate'} and not identity_context and identity_field!='full_name':
                item=items[specialty['id']]
                key='specialty' if specialty['status']=='resolved' else 'specialty_candidate'
                vars[key]=item['label'].lower()
                if key=='specialty_candidate':vars['specialty_candidate_label']=item['label']
                if intent=='unknown' and (specialty['status']=='resolved' or meaning['alternatives'][0][0] in {'book','confirm','my_booking'}):
                    intent='book';confidence=.65
            if intent=='answer_yes':intent='unknown';semantic_yes=True
            if intent=='answer_no':intent='unknown';semantic_yes=False

        if not meaning and ent.specialty:
            vars.update(specialty=ent.specialty, needs_referral=ent.needs_referral)
        if not meaning and ent.doctor_surname:
            vars["doctor_surname"] = ent.doctor_surname
        # Covers a surname even if a specialty was also named.
        if not meaning and re.search(
            r"(?:врачу|доктору|терапевту|кардиологу|хирургу)\s+[А-ЯЁ][а-яё]+", text
        ):
            vars["doctor_surname"] = text.split()[-1]
        if re.search(r'\b(?:реб[её]н(?:ок|ка|ку)|детск\w*|сын(?:а|у)?|доч(?:ь|ери|ку))\b',text.lower()):
            vars['patient_group']='child'
        if ent.for_other is not None:
            vars["for_other"] = ent.for_other
        if it.date and not identity_context:
            vars["said_date"] = it.date.isoformat()
        if it.time and not identity_context:
            vars["said_time"] = it.time.strftime("%H:%M")
        if it.daypart and not identity_context:
            vars["said_daypart"] = it.daypart
        if not identity_context and identity_field != 'full_name':
            from .availability_constraints import parse as parse_availability
            constraints = parse_availability(text, today)
            if constraints['evidence'] or constraints['needs_clarification']:
                vars['availability_constraints'] = constraints
                if constraints['excluded_weekdays'] or constraints['excluded_dates']:
                    vars.pop('said_date', None)
                    if len(constraints['dates']) == 1: vars['said_date'] = constraints['dates'][0]
                if constraints['time_from'] is not None or constraints['time_to'] is not None:
                    vars.pop('said_time', None)
                    if (constraints['time_from'] == constraints['time_to']
                            and constraints['time_from_inclusive'] and constraints['time_to_inclusive']
                            and not constraints['needs_clarification']):
                        vars['said_time'] = constraints['time_from']
        if meaning and intent=='greeting' and any(k in vars for k in ('said_date','said_time','said_daypart')):intent='unknown'
        return "ok", {
            "intent": intent,
            "confidence": confidence,
            "normalized": it.normalized,
            "yes_no": semantic_yes,
            "semantic": meaning,
            "flags":flags,
            "source_scenario":{'home_visit':'С-06','results':'С-08','paid':'С-09','waitlist':'С-11','for_other':'С-12','attachment':'С-13'}.get(intent),
            "affirmative_action": affirmative_action,
            "digits": digits,
            "date_field": date_field,
            "vars": vars,
            "decision": verified.action,
            "classifier": rules.intent,
            "verifier": verified.embed_intent,
        }


class CrmProxy:
    def __init__(self, url=None):
        self.url = url or os.environ.get("CRM_URL", "http://127.0.0.1:8081")

    def call(self, op, args):
        result = post_json(f"{self.url}/v1/{op}", args, timeout=90)
        return result["status"], result["payload"]


class Operator(CrmProxy):
    def handle(self, op, args):
        return self.call(
            "operator_transfer" if op == "transfer" else "operator_list", args
        )


class Sms(CrmProxy):
    """Durable delivery outbox; an offline gateway never masquerades as delivered."""

    def __init__(self, url=None):
        super().__init__(url)
        self.gateway = os.environ.get("SMS_GATEWAY_URL", "")
        self.token = os.environ.get("SMS_GATEWAY_TOKEN", "")
        self.demo = os.environ.get("APP_MODE", "demo") == "demo"
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def handle(self, op, args):
        if op != "send":
            return "error", {"error": "unknown SMS operation"}
        status, result = self.call("sms_enqueue", args)
        if status == "ok":
            self.flush()
            result["delivery"] = (
                "simulated" if self.demo and not self.gateway else "queued"
            )
        return status, result

    def flush(self):
        from .module_settings import configured
        if not configured().read()['sms']['enabled']:
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            status, result = self.call("sms_pending", {})
            if status != "ok":
                return
            for m in result["messages"]:
                if not configured().read()['sms']['enabled']:
                    return
                if not self.gateway:
                    if self.demo:
                        self.call(
                            "sms_ack",
                            {
                                "id": m["id"],
                                "state": "simulated",
                                "provider_id": f"demo-{m['id']}",
                            },
                        )
                    continue
                request = urllib.request.Request(
                    self.gateway,
                    data=json.dumps({"to": m["phone"], "text": m["text"]}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.token}",
                        "Idempotency-Key": m["key"],
                    },
                    method="POST",
                )
                try:
                    with open_http(request, timeout=5) as response:
                        data = json.load(response)
                    self.call(
                        "sms_ack",
                        {
                            "id": m["id"],
                            "state": "accepted",
                            "provider_id": str(data["id"]),
                        },
                    )
                except (OSError, ValueError, KeyError):
                    # Keep pending, retry with the same provider idempotency key.
                    continue
        finally:
            self.lock.release()

    def run(self):
        while not self.stop.wait(10):
            try:
                self.flush()
            except (OSError, ValueError, KeyError):
                pass


class Speech:
    def __init__(self, kind, settings=None):
        self.kind = kind
        if kind == "tts":
            from calls.tts.silero import SileroTTS, TtsModule

            path = os.environ.get("TTS_MODEL", "modules/tts/models/v5_cis_base.pt")
            synth = SileroTTS(
                path,
                (settings or {}).get('voice') or os.environ.get("TTS_SPEAKER") or None,
                device=os.environ.get("TTS_DEVICE", "cpu"),
                stress=os.environ.get("TTS_STRESS", "context"),
            )
            self.module = TtsModule(synth, cache_size=64)
            from .audio_bank import PromptBank

            self.bank = PromptBank(
                os.environ.get("PROMPT_DIR", "data/audio/prompts"), synth, self.module
            )
            self.bank.warm(os.environ.get("SCENARIO_DIR", "scenarios"))
        else:
            from calls.asr.onnx_gateway import OnnxAsrModule

            model = os.environ.get("ASR_MODEL", "gigaam-v3-e2e-rnnt")
            self.module = OnnxAsrModule(model)

    def handle(self, op, args):
        if self.kind == "tts":
            if op == "preview":
                from calls.tts.silero import to_pcm16
                rate = float(args.get('rate',1.5))
                if not .5 <= rate <= 2.5:raise ValueError('Недопустимая скорость речи')
                audio=self.module.synth.synth_quality(str(args['text']),rate)
                pcm=to_pcm16(audio)
                return 'ok',{'pcm':pcm,'sample_rate':48000,'format':'pcm16','duration_ms':len(audio)*1000//48000,'engine':self.module.synth.name}
            if op == "bank.doctors.refresh":
                return "ok", self.bank.refresh_doctors(args["doctors"], float(args.get("rate", 1.5)))
            if op == "bank.audit":
                from .admin_rpc import module_call
                return 'ok',self.bank.audit(os.environ.get('SCENARIO_DIR','scenarios'),lambda pcm:module_call('asr','transcribe',{'pcm_b64':__import__('base64').b64encode(pcm).decode(),'rate':8000})['text'])
            if op == "bank.refresh":
                self.bank.warm(os.environ.get("SCENARIO_DIR", "scenarios"))
                return "ok", {"prompts": len(self.bank.manifest), "generated": self.bank.generated}
            if op == "bank.replace":
                import base64
                return "ok", self.bank.replace_recording(str(args['key']), base64.b64decode(args['wav_b64'], validate=True))
            if op in ("say", "synth"):
                return self.bank.render(str(args["text"]), float(args.get("rate", 1.5)))
            return self.module.handle(op, args)
        return self.module.handle(op, args)
