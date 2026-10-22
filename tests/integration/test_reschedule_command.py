import pytest


@pytest.mark.parametrize('text',['Перенесите запись.','Хочу перенести прием.','Пожалуйста, перенесите мою запись.'])
def test_clear_request_is_not_lost_to_unknown(semantic_nlu,text):
    status,data=semantic_nlu.handle('interpret',{'text':text,'state':'reminder.start','today':'2026-10-21'})
    assert status=='ok'
    assert data['intent']=='reschedule'


@pytest.mark.parametrize('text',['Не переносите запись.','Не хочу перенести прием.','Принесите запись.','Перенесите запись и отмените другую.'])
def test_negated_or_ambiguous_phrase_does_not_match_exact_command(semantic_nlu,text):
    _,data=semantic_nlu.handle('interpret',{'text':text,'state':'reminder.start','today':'2026-10-21'})
    assert not (data['intent']=='reschedule' and data['confidence']==1.0)
