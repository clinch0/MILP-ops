"""One-shot release gate: recognize every current recording before starting the voice service."""
import json
import time
from .admin_rpc import module_call, ModuleUnavailable


def audit():
    # Audit is a read/verify operation, never a clinical write. Only temporary
    # transport failures can be retried; bad pronunciations remain a hard gate.
    for attempt in range(3):
        try:
            return module_call('tts','bank.audit',{})
        except ModuleUnavailable:
            if attempt == 2:
                raise
            time.sleep(2)


def main():
    try:
        report=audit()
    except (ValueError,OSError) as error:
        print(json.dumps({'status':'failed','stage':'bank.audit',
            'error_type':type(error).__name__,
            'action':'Проверьте здоровье TTS/ASR и повторите docker compose up --force-recreate audio-check.'},ensure_ascii=False),flush=True)
        raise SystemExit(1) from None
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if report.get('status')!='passed':
        raise SystemExit('Аудиобанк не прошёл проверку. Откройте Аудиофразы → Проверить произношение')


if __name__=='__main__':main()
