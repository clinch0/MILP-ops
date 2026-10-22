"""Startup transport recovery must never turn failed speech verification green."""
import pytest
from clinic import audio_check
from clinic.admin_rpc import ModuleUnavailable


def test_transient_transport_recovers_with_bounded_retries(monkeypatch):
    attempts=[]
    def rpc(*_):
        attempts.append(1)
        if len(attempts)<3:raise ModuleUnavailable('offline')
        return {'status':'passed'}
    monkeypatch.setattr(audio_check,'module_call',rpc)
    monkeypatch.setattr(audio_check.time,'sleep',lambda _:None)
    assert audio_check.audit()['status']=='passed' and len(attempts)==3


def test_bad_recording_is_not_retried_or_allowed(monkeypatch):
    attempts=[]
    def rpc(*_):
        attempts.append(1)
        return {'status':'needs_review'}
    monkeypatch.setattr(audio_check,'module_call',rpc)
    with pytest.raises(SystemExit):audio_check.main()
    assert len(attempts)==1


def test_persistent_outage_is_clear_and_bounded(monkeypatch,capsys):
    attempts=[]
    def rpc(*_):
        attempts.append(1)
        raise ModuleUnavailable('do not print a private endpoint')
    monkeypatch.setattr(audio_check,'module_call',rpc)
    monkeypatch.setattr(audio_check.time,'sleep',lambda _:None)
    with pytest.raises(SystemExit) as error:audio_check.main()
    assert error.value.code==1 and len(attempts)==3
    output=capsys.readouterr().out
    assert 'ModuleUnavailable' in output and 'private endpoint' not in output
