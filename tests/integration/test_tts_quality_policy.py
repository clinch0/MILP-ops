"""Meaning-changing recognition errors must fail even when aggregate WER is low."""
import json
from types import SimpleNamespace

import pytest
from calls.tts.quality import compare, QUALITY_POLICY_VERSION
from calls.tts.stress import DictAccentor, NoStress, accentor_fingerprint, load_accentor
from clinic.audio_bank import PromptBank


@pytest.mark.parametrize(('expected', 'actual', 'guard'), [
    ('Перед исследованием нельзя есть до исследования, пожалуйста уточните порядок подготовки у врача и возьмите с собой паспорт и полис.',
     'Перед исследованием нельзя есть после исследования, пожалуйста уточните порядок подготовки у врача и возьмите с собой паспорт и полис.', 'polarity_match'),
    ('Никогда не отменяйте назначенные врачом препараты самостоятельно, перед исследованием согласуйте изменения с лечащим врачом и уточните правила подготовки.',
     'Не отменяйте назначенные врачом препараты самостоятельно, перед исследованием согласуйте изменения с лечащим врачом и уточните правила подготовки.', 'polarity_match'),
    ('Воду разрешается пить только после исследования, уточните подробности подготовки у врача и возьмите с собой паспорт и полис.',
     'Воду разрешается пить после исследования, уточните подробности подготовки у врача и возьмите с собой паспорт и полис.', 'polarity_match'),

    ('Вы не записаны на приём. Пожалуйста, дождитесь подтверждения от регистратора и не приезжайте раньше назначенного времени.',
     'Вы записаны на приём. Пожалуйста, дождитесь подтверждения от регистратора и не приезжайте раньше назначенного времени.', 'polarity_match'),
    ('Перед исследованием нельзя есть и пить после полуночи. Если принимаете лекарства, уточните правила у врача.',
     'Перед исследованием можно есть и пить после полуночи. Если принимаете лекарства, уточните правила у врача.', 'polarity_match'),
    ('Не подтверждайте запись, отменяйте запись только после согласия пациента и проверки данных в базе.',
     'Подтверждайте запись, не отменяйте запись только после согласия пациента и проверки данных в базе.', 'polarity_match'),
    ('Ваш приём назначен на шестнадцатое октября в кабинете номер двести один, возьмите паспорт и полис.',
     'Ваш приём назначен на семнадцатое октября в кабинете номер двести один, возьмите паспорт и полис.', 'numbers_match'),
    ('Ваш приём назначен на шестнадцатое октября в кабинете номер двести один, возьмите паспорт и полис.',
     'Ваш приём назначен на шестнадцатое ноября в кабинете номер двести один, возьмите паспорт и полис.', 'numbers_match'),
    ('Ваша запись подтверждена, возьмите с собой паспорт и полис, подробности поступят вам в сообщении.',
     'Ваша запись отменена, возьмите с собой паспорт и полис, подробности поступят вам в сообщении.', 'status_match'),
])
def test_low_wer_does_not_hide_material_error(expected, actual, guard):
    result = compare(expected, actual)
    assert result['wer'] <= .15
    assert not result[guard]
    assert not result['passed']
    assert result['quality_policy_version'] == QUALITY_POLICY_VERSION


def test_numeric_and_spoken_dates_and_harmless_grammar_are_equivalent():
    assert compare('Вы записаны 16.10.2026 в 14:30.',
                   'Вы записаны шестнадцатого октября две тысячи двадцать шестого года в четырнадцать тридцать.')['passed']
    assert compare('На этот номер записано несколько пациентов. Введите дату рождения пациента: восемь цифр.',
                   'На этот номер записаны несколько пациентов. Введите дату рождения пациента: 8 цифр.')['passed']


def test_clock_range_format_does_not_hide_changed_hours_minutes_or_polarity():
    expected='Обратный звонок можно выбрать с десяти до шестнадцати часов. Один — да. Два — нет.'
    assert compare(expected,'Обратный звонок можно выбрать с 10:00 до 16:00. Один — да. Два — нет.')['passed']
    for clock in ['с 10:00 до 15:00','с 10:00 до 16:30','с 16:00 до 10:00']:
        assert not compare(expected,'Обратный звонок можно выбрать '+clock+'. Один — да. Два — нет.')['passed']
    assert not compare(expected,expected.replace('до шестнадцати','после шестнадцати'))['passed']


def test_real_dictionary_fallback_and_clitics_determine_fingerprint():
    plain = DictAccentor(lexicon={'замок': 'з+амок'})
    changed = DictAccentor(lexicon={'замок': 'зам+ок'})
    assert accentor_fingerprint(plain) == accentor_fingerprint(DictAccentor(lexicon={'замок': 'з+амок'}))
    assert accentor_fingerprint(plain) != accentor_fingerprint(changed)
    assert accentor_fingerprint(plain) != accentor_fingerprint(DictAccentor(lexicon=plain.lexicon, clitics=frozenset()))
    assert accentor_fingerprint(plain) != accentor_fingerprint(DictAccentor(lexicon=plain.lexicon, fallback=lambda word: word))
    assert accentor_fingerprint(plain) != accentor_fingerprint(NoStress())


def test_changed_accentor_never_reuses_old_recording(tmp_path):
    # A protocol unit test, not an acoustic acceptance fixture.
    model = tmp_path / 'unit-model.bin'
    model.write_bytes(b'explicit unit-test model identity')
    first = DictAccentor(lexicon={'замок': 'з+амок'})
    second = DictAccentor(lexicon={'замок': 'зам+ок'})
    def synth(accentor):
        return SimpleNamespace(model_path=model, name='unit', speaker='test',
                               pipeline_version=accentor_fingerprint(accentor))
    class Module:
        def handle(self, op, args):
            assert op == 'synth'
            return 'ok', {'engine': 'unit dependency'}
    folder = tmp_path / 'bank'
    old = PromptBank(folder, synth(first), Module())
    old_key = old.key('Замок', 1.5)
    (folder / 'manifest.json').write_text(json.dumps({old_key: {'file': old_key + '.wav'}}))
    current = PromptBank(folder, synth(second), Module())
    assert current.key('Замок', 1.5) != old_key
    status, result = current.render('Замок', 1.5)
    assert status == 'ok' and result['audio_source'] == 'dynamic_tts'
    assert current.hits == 0


def test_dict_accentor_fingerprint_is_fully_local(monkeypatch):
    import socket
    import urllib.request
    import calls.tts.stress as stress
    def forbidden(*args, **kwargs):
        raise AssertionError('dictionary mode must not load/download a fallback')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(stress.SileroStressAccentor, '__init__', forbidden)
    accentor = load_accentor('dict')
    assert isinstance(accentor, DictAccentor)
    assert len(accentor_fingerprint(accentor)) == 64
