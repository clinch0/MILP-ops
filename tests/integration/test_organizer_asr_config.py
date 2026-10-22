import importlib.util
from pathlib import Path

import pytest
from clinic.module_settings import ModuleSettings

spec = importlib.util.spec_from_file_location('configure_organizer_asr', Path(__file__).resolve().parents[2] / 'tools/configure_organizer_asr.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_selects_required_model_preserving_other_modules(tmp_path):
    path = tmp_path / 'modules.json'
    settings = ModuleSettings(path)
    before = settings.read()
    before['tts']['voice'] = 'ru_irina'
    settings.save(before)
    module.configure(path, 'https://hub.gigaschool.ru/api/v1/llm/v1')
    after = settings.read()
    assert after['asr']['model'] == 'openai/whisper-large-v3'
    assert after['asr']['key_env'] == 'OPENAI_API_KEY'
    assert {k:v for k,v in after.items() if k != 'asr'} == {k:v for k,v in before.items() if k != 'asr'}


def test_rejects_insecure_remote_without_replacing_config(tmp_path):
    path = tmp_path / 'modules.json'
    settings = ModuleSettings(path)
    settings.save(settings.read())
    before = path.read_bytes()
    with pytest.raises(ValueError):
        module.configure(path, 'http://external.example/v1')
    assert path.read_bytes() == before


def test_live_agent_survives_fresh_module_config(tmp_path):
    path=tmp_path/'modules.json'
    module.configure(path,'https://hub.gigaschool.ru/api/v1/llm/v1','GigaChat/GigaChat-3-Pro')
    agent=ModuleSettings(path).read()['agent']
    assert agent['provider']=='compatible'
    assert agent['model']=='GigaChat/GigaChat-3-Pro'
    assert agent['key_env']=='OPENAI_API_KEY'
