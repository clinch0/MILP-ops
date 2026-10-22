"""Replay the original failed subsecond PCM; no regenerated audio or API calls."""
import base64
import hashlib
from pathlib import Path
import wave

from calls.asr.onnx_gateway import OnnxAsrModule


def test_original_short_confirmation_is_recognized_with_endpoint_context():
    fixture=Path(__file__).resolve().parents[1]/'fixtures/asr/short-identity-confirmation.wav'
    assert hashlib.sha256(fixture.read_bytes()).hexdigest()=='03ca4e38b6515731fd085cb3472f0bda9b5800f673baeea23acc592caa17833e'
    with wave.open(str(fixture)) as audio:
        rate=audio.getframerate();pcm=audio.readframes(audio.getnframes())
    assert .8 < len(pcm)/2/rate < 1.1
    status,result=OnnxAsrModule().handle('transcribe',{'pcm_b64':base64.b64encode(pcm).decode(),'rate':rate})
    assert status=='ok' and result['text'].strip(' .!').casefold()=='да, данные верны'
