"""Select the hackathon's prescribed ASR without changing local GPU defaults.

Run inside scenario-editor (the writable owner of MODULE_SETTINGS_FILE).
Only synthetic/consented test audio may use the organizer gateway; this is not
a claim that the gateway is approved for real patient data in production.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime/src'))
from clinic.module_settings import ModuleSettings


def configure(path, base_url, agent_model=None):
    settings = ModuleSettings(path)
    values = settings.read()
    values['asr'].update(enabled=True, provider='compatible',
        model='openai/whisper-large-v3', base_url=base_url,
        key_env='OPENAI_API_KEY')
    if agent_model:
        values['agent'].update(enabled=True,provider='compatible',model=agent_model,
            base_url=base_url,key_env='OPENAI_API_KEY')
    settings.save(values)
    return {'module':'asr', 'provider':'compatible', 'model':values['asr']['model']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings-file', default=os.environ.get('MODULE_SETTINGS_FILE', '/data/modules.json'))
    parser.add_argument('--base-url', default=os.environ.get('OPENAI_BASE_URL', 'https://hub.gigaschool.ru/api/v1/llm/v1'))
    parser.add_argument('--agent-model',help='Explicitly persist the existing live slot-agent model, including after restart')
    args = parser.parse_args()
    print(configure(args.settings_file, args.base_url, args.agent_model))
