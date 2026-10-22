"""Actual Silero/CPU ASR acceptance of the medical boundary menu in PCM and G.711. No API keys. Run in the integration-test image."""
import sys,base64,json,wave,pathlib,audioop,argparse
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'runtime/src')]
from calls.tts.silero import SileroTTS,TtsModule
from calls.asr.onnx_gateway import OnnxAsrModule
from calls.tts.quality import compare
from calls.scenario.dsl import load_flows
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--callback-failure',action='store_true')
args=parser.parse_args()
flow_name,state_name=('callback','failed') if args.callback_failure else ('inbound','medical_boundary')
flow=load_flows(ROOT/'scenarios')[flow_name]
text=next(statement.text for statement in flow.states[state_name].statements if statement.kind=='say')
synth=SileroTTS(ROOT/'modules/tts/models/v5_cis_base.pt',speaker='ru_ekaterina',device='cpu',stress='dict')
pcm,_=TtsModule(synth).render(text,1.5)
asr=OnnxAsrModule()
folder=ROOT/'debug/runs'/('callback-failure-audio' if args.callback_failure else 'medical-boundary-audio');folder.mkdir(parents=True,exist_ok=True)
results=[]
for name,data in [('pcm',pcm),('alaw',audioop.alaw2lin(audioop.lin2alaw(pcm,2),2)),('ulaw',audioop.ulaw2lin(audioop.lin2ulaw(pcm,2),2))]:
 status,result=asr.handle('transcribe',{'pcm_b64':base64.b64encode(data).decode(),'rate':8000})
 assert status=='ok'
 assessment=compare(text,result['text'])
 results.append({'codec':name,'asr':result['text'],**assessment})
 with wave.open(str(folder/(name+'.wav')),'wb') as w:w.setparams((1,2,8000,0,'NONE','not compressed'));w.writeframes(data)
(folder/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=False),flush=True)
assert all(row['passed'] for row in results)
