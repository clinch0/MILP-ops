"""Fixed module destinations; browser cannot select a URL or an arbitrary operation."""
import json
import os
import urllib.error
import urllib.request
from calls.services.netutil import open_http


class ModuleUnavailable(ValueError):
    """Transport failure, distinct from a reachable module rejecting an operation."""


def module_call(module, operation, args):
    if module not in {'crm', 'tts', 'asr', 'nlu'}:
        raise ValueError('Unsupported administration module')
    url = os.environ.get(module.upper()+'_URL', 'http://127.0.0.1:'+{'crm':'8081','nlu':'8082','tts':'8085','asr':'8086'}[module])
    request = urllib.request.Request(url.rstrip('/')+'/v1/'+operation,
        data=json.dumps(args,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
    try:
        with open_http(request, timeout=120 if operation in {'bank.refresh','bank.audit','bank.doctors.refresh'} else 15) as response:
            result=json.loads(response.read(4*1024*1024))
    except urllib.error.HTTPError as error:
        # A module can be reachable but reject pronunciation or input. Preserve
        # its structured explanation instead of reporting a connection outage.
        try:
            result=json.loads(error.read(4*1024*1024))
        except (OSError,ValueError):
            raise ValueError('Модуль '+module+' недоступен; проверьте запуск контейнера') from None
    except OSError:
        raise ModuleUnavailable('Модуль '+module+' недоступен; проверьте запуск контейнера') from None
    if result.get('status') not in {'ok','empty'}:
        raise ValueError(result.get('payload',{}).get('error') or result.get('status','Ошибка модуля'))
    return result.get('payload',{})
