"""SIP/ARI + binary AudioSocket UUID, independent media/control workers per call.

Reuses calls-arena's tested in-band detector and OOB arbiter. AudioSocket PCM
is endpointed locally; only utterances reach ASR after the graph arms listening.
"""

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from queue import Full, Queue

import numpy as np
from calls.dtmf.arbiter import DtmfArbiter
from calls.dtmf.events import DigitEnd, DigitStart
from calls.dtmf.inband import InbandDtmfDetector
from calls.services.netutil import open_http
from calls.telephony.ari_bridge import Ari, WsClient


def recv_exact(conn, n):
    chunks = bytearray()
    while len(chunks) < n:
        data = conn.recv(n - len(chunks))
        if not data:
            raise EOFError
        chunks.extend(data)
    return bytes(chunks)


class Client:
    def __init__(self):
        self.url = os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8080")
        self.token = os.environ.get("API_TOKEN", "")

    def call(self, op, **args):
        req = urllib.request.Request(
            f"{self.url}/v1/{op}",
            data=json.dumps(args).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        with open_http(req, timeout=190) as response:
            return json.load(response)["payload"]


@dataclass
class Context:
    call_id: str
    channel: str
    media: str = ""
    operator: str = ""
    operator_connected: threading.Event = field(default_factory=threading.Event)
    events: Queue = field(default_factory=lambda: Queue(maxsize=64))
    stop: threading.Event = field(default_factory=threading.Event)
    generation: int = 0
    listening: bool = False
    playing: bool = False
    conn: object = None
    arbiter: DtmfArbiter = field(
        default_factory=lambda: DtmfArbiter(oob_negotiated=True, inband_fallback=True)
    )
    detector: InbandDtmfDetector = field(default_factory=InbandDtmfDetector)
    samples: int = 0
    utterance: bytearray = field(default_factory=bytearray)
    last_utterance: bytes = b""
    silence_samples: int = 0
    voiced: bool = False


class Bridge:
    def __init__(self, ari=None, client=None):
        self.ari = ari or Ari(
            os.environ.get("ARI_URL", "http://127.0.0.1:8088"),
            os.environ.get("ARI_USER", "calls"),
            os.environ["ARI_PASSWORD"],
        )
        self.client = client or Client()
        self.calls = {}
        self.by_channel = {}
        self.lock = threading.Lock()

    def enqueue(self, ctx, op, **args):
        if op == "call.dtmf":
            ctx.generation += 1
        try:
            ctx.events.put_nowait((op, args))
        except Full:
            ctx.stop.set()  # Never run an unbounded buffer for a stalled call.

    def controls(self, ctx):
        while not ctx.stop.is_set():
            from queue import Empty

            try:
                op, args = ctx.events.get(timeout=0.5)
            except Empty:
                continue
            try:
                result = self.client.call(op, call_id=ctx.call_id, **args)
                ctx.listening = bool(result.get("listening"))
            except (OSError, ValueError):
                ctx.stop.set()

    def playback(self, ctx):
        while not ctx.stop.is_set():
            # ARI may finish call.start before the TCP UUID handshake arrives.
            # Do not consume (and lose) the first spoken prompt in that interval.
            if ctx.conn is None:
                ctx.stop.wait(0.02)
                continue
            try:
                prompt = self.client.call("call.next_prompt", call_id=ctx.call_id)
                listening = prompt.get("listening", ctx.listening)
                if listening and not ctx.listening and ctx.last_utterance:
                    self.enqueue(
                        ctx,
                        "call.audio",
                        pcm_b64=base64.b64encode(ctx.last_utterance).decode(),
                        rate=8000,
                    )
                    ctx.last_utterance = b""
                ctx.listening = listening
                pcm = base64.b64decode(prompt.get("pcm_b64", ""))
                if pcm and ctx.conn:
                    print(json.dumps({'event':'audio_prompt','call_id':ctx.call_id,'pcm_bytes':len(pcm)}), flush=True)
                    epoch = ctx.generation
                    start = time.monotonic()
                    ctx.playing = True
                    try:
                        for i in range(0, len(pcm), 320):
                            if ctx.stop.is_set() or epoch != ctx.generation:
                                break
                            frame = pcm[i : i + 320]
                            ctx.conn.sendall(
                                bytes([0x10]) + struct.pack("!H", len(frame)) + frame
                            )
                            ctx.stop.wait(
                                max(0, start + (i // 320 + 1) * 0.02 - time.monotonic())
                            )
                    finally:
                        ctx.playing = False
                    if epoch == ctx.generation:
                        self.client.call(
                            "call.prompt_done",
                            call_id=ctx.call_id,
                            prompt_id=prompt.get("prompt_id"),
                        )
                elif prompt.get("finished"):
                    if prompt.get("status") == "transfer":
                        self.transfer(ctx)
                    else:
                        self.ari.hangup(ctx.channel)
                    break
            except (OSError, ValueError) as exc:
                print(json.dumps({'event':'playback_failed','call_id':ctx.call_id,'error_type':type(exc).__name__}), flush=True)
                break
            ctx.stop.wait(0.05)

    def transfer(self, ctx):
        endpoint = os.environ.get("OPERATOR_SIP_ENDPOINT", "")
        if not endpoint:
            self.ari._req("POST", f"/channels/{ctx.channel}/continue",
                          context="clinic-operator", extension="s", priority=1)
            return
        ctx.operator = str(uuid.uuid4())
        try:
            self.ari._req("POST", f"/channels/{ctx.channel}/ring")
            self.ari._req("POST", "/channels", endpoint=endpoint, app="calls",
                          appArgs=f"operator,{ctx.call_id}", timeout=30,
                          channelId=ctx.operator)
        except (OSError, ValueError):
            self.transfer_failed(ctx)
            return
        print(json.dumps({'event':'operator_dialing','call_id':ctx.call_id}), flush=True)
        def deadline():
            if not ctx.operator_connected.wait(32) and not ctx.stop.is_set():
                self.transfer_failed(ctx)
        threading.Thread(target=deadline, daemon=True).start()

    def transfer_failed(self, ctx):
        print(json.dumps({'event':'operator_unanswered','call_id':ctx.call_id}), flush=True)
        try:
            self.ari._req("DELETE", f"/channels/{ctx.channel}/ring")
            clip = self.client.call('speech.synth', text='Сотрудник сейчас не отвечает. Ваше обращение сохранено для регистратора. Если нужна срочная помощь, позвоните сто двенадцать.', rate=1.3)
            pcm = base64.b64decode(clip['pcm_b64'])
            if ctx.conn:
                for offset in range(0,len(pcm),320):
                    if ctx.stop.is_set(): break
                    frame=pcm[offset:offset+320]
                    ctx.conn.sendall(bytes([0x10])+struct.pack('!H',len(frame))+frame)
                    ctx.stop.wait(.02)
        except (OSError, ValueError, KeyError):
            pass
        finally:
            if ctx.operator: self.ari.hangup(ctx.operator)
            self.ari.hangup(ctx.channel)

    def connect_operator(self, channel, call_id):
        ctx = self.calls.get(call_id)
        if not ctx or ctx.stop.is_set() or channel.get('id') != ctx.operator:
            self.ari.hangup(channel['id'])
            return
        self.ari._req("DELETE", f"/channels/{ctx.channel}/ring")
        self.ari.bridge(ctx.channel, ctx.operator)
        self.by_channel[ctx.operator] = call_id
        ctx.operator_connected.set()
        print(json.dumps({'event':'operator_connected','call_id':call_id}), flush=True)
        if ctx.media: self.ari.hangup(ctx.media)
        # The external card means an answered, bridged transfer, never an offer.
        try:
            result = self.client.call('stand.operator_connected', target_call_id=call_id)
            print(json.dumps({'event':'operator_escalation','call_id':call_id,'status':result.get('status')}), flush=True)
        except (OSError, ValueError):
            print(json.dumps({'event':'operator_escalation_unconfirmed','call_id':call_id}), flush=True)

    def audio(self, ctx, pcm):
        if len(pcm) % 2:
            return
        # AudioSocket may combine many 20-ms packets. Keep detector/VAD ordering
        # causal even for a large read containing both speech and a keypad tone.
        for offset in range(0, len(pcm), 320):
            self._audio_frame(ctx, pcm[offset : offset + 320])

    def _audio_frame(self, ctx, pcm):
        frame = np.frombuffer(pcm, dtype="<i2")
        ctx.samples += len(frame)
        tone = ctx.detector.active_digit is not None
        for ev in ctx.detector.push(frame):
            if isinstance(ev, DigitStart):
                # A confirmed key interrupts an unfinished spoken answer. Clear
                # its leading detection window as well; otherwise pure tones
                # later become a second, spurious ASR answer in the next state.
                ctx.utterance.clear()
                ctx.last_utterance = b""
                ctx.voiced = False
                ctx.silence_samples = 0
                tone = True
            if isinstance(ev, DigitEnd):
                tone = True
                for digit in ctx.arbiter.on_inband(
                    ev.digit, ev.t_start, ev.t_end, detector_version=ev.detector_version
                ):
                    if not digit.shadow:
                        self.enqueue(ctx, "call.dtmf", digit=digit.digit)
        for digit in ctx.arbiter.tick(ctx.samples):
            self.enqueue(ctx, "call.dtmf", digit=digit.digit)
        # 600 ms energy endpoint with a hard 10-second utterance cap. No inference on 20 ms frames.
        if ctx.playing or tone or ctx.detector.active_digit is not None:
            return
        rms = (
            float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) if len(frame) else 0
        )
        voiced = rms > float(os.environ.get("VAD_RMS", "350"))
        if voiced:
            ctx.voiced = True
            ctx.silence_samples = 0
        elif ctx.voiced:
            ctx.silence_samples += len(frame)
        if ctx.voiced:
            ctx.utterance.extend(pcm)
        if ctx.voiced and (ctx.silence_samples >= 4800 or len(ctx.utterance) >= 160000):
            utterance = bytes(ctx.utterance)
            ctx.utterance.clear()
            ctx.voiced = False
            ctx.silence_samples = 0
            if ctx.listening:
                self.enqueue(
                    ctx,
                    "call.audio",
                    pcm_b64=base64.b64encode(utterance).decode(),
                    rate=8000,
                )
            else:
                ctx.last_utterance = utterance

    def media_connection(self, conn):
        ctx = None
        try:
            conn.settimeout(120)
            while True:
                kind, length = struct.unpack("!BH", recv_exact(conn, 3))
                payload = recv_exact(conn, length)
                if kind == 0x01:
                    if length != 16:
                        raise ValueError("AudioSocket UUID must be 16 binary bytes")
                    call_id = str(uuid.UUID(bytes=payload))
                    ctx = self.calls.get(call_id)
                    if ctx is None:
                        raise ValueError("unknown call UUID")
                    ctx.conn = conn
                    print(json.dumps({'event':'media_connected','call_id':call_id}), flush=True)
                elif kind == 0x10 and ctx:
                    self.audio(ctx, payload)
                elif kind == 0x03 and ctx:
                    self.enqueue(ctx, "call.dtmf", digit=payload.decode("ascii"))
                elif kind in (0, 0xFF):
                    break
        except (OSError, EOFError, ValueError):
            pass
        finally:
            conn.close()
            if ctx:
                ctx.stop.set()

    def start_call(self, channel, args):
        call_id = str(uuid.uuid4())
        ctx = Context(call_id, channel["id"])
        self.calls[call_id] = ctx
        self.by_channel[channel["id"]] = call_id
        try:
            flow = args[0] if args else "inbound"
            phone = (
                args[1]
                if flow == "reminder" and len(args) > 1
                else channel.get("caller", {}).get("number", "")
            )
            vars = (
                {"booking_id": int(args[2])}
                if flow == "reminder" and len(args) > 2
                else {}
            )
            self.ari.answer(ctx.channel)
            host = os.environ.get("AUDIOSOCKET_ADVERTISE", "127.0.0.1:9092")
            media = self.ari.external_media("calls", host, call_id)
            ctx.media = media["id"]
            self.ari.bridge(ctx.channel, ctx.media)
            self.client.call(
                "call.start", call_id=call_id, flow=flow, caller_phone=phone, registrar_number=(os.environ.get('REGISTRAR_NUMBER','114') if flow == 'reminder' else str(channel.get("dialplan",{}).get("exten","100"))), vars=vars
            )
            threading.Thread(target=self.controls, args=(ctx,), daemon=True).start()
            threading.Thread(target=self.playback, args=(ctx,), daemon=True).start()
        except (OSError, ValueError, KeyError):
            ctx.stop.set()
            self.ari.hangup(ctx.channel)

    def event(self, ev):
        channel = ev.get("channel", {})
        kind = ev.get("type")
        if kind == "StasisStart":
            args = ev.get('args', [])
            if len(args) == 2 and args[0] == 'operator':
                threading.Thread(target=self.connect_operator, args=(channel,args[1]), daemon=True).start()
                return
            name = channel.get("name", "")
            if name.startswith(("AudioSocket/", "UnicastRTP/")):
                return
            threading.Thread(
                target=self.start_call, args=(channel, ev.get("args", [])), daemon=True
            ).start()
        elif kind == "ChannelDtmfReceived":
            ctx = self.calls.get(self.by_channel.get(channel.get("id")))
            if ctx:
                end = ctx.samples
                start = max(0, end - int(float(ev.get("duration_ms", 80)) * 8))
                for digit in ctx.arbiter.on_oob(ev["digit"], start, end):
                    self.enqueue(ctx, "call.dtmf", digit=digit.digit)
        elif kind == "StasisEnd":
            call_id = self.by_channel.pop(channel.get("id"), None)
            ctx = self.calls.pop(call_id, None)
            if ctx:
                ctx.stop.set()
                for peer in (ctx.channel, ctx.operator):
                    if peer and peer != channel.get('id'):
                        self.by_channel.pop(peer, None)
                        self.ari.hangup(peer)
                if ctx.media:
                    self.ari.hangup(ctx.media)
                threading.Thread(
                    target=lambda: self.client.call("call.hangup", call_id=call_id),
                    daemon=True,
                ).start()

    def run(self):
        host, port = os.environ.get("AUDIOSOCKET_BIND", "127.0.0.1:9092").rsplit(":", 1)
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, int(port)))
        server.listen(64)

        def accept():
            while True:
                conn, _ = server.accept()
                threading.Thread(
                    target=self.media_connection, args=(conn,), daemon=True
                ).start()

        threading.Thread(target=accept, daemon=True).start()
        parsed = urllib.parse.urlsplit(self.ari.url)
        while True:
            try:
                creds = urllib.parse.quote(
                    f"{self.ari.user}:{self.ari.password}", safe=""
                )
                ws = WsClient(
                    f"ws://{parsed.hostname}:{parsed.port or 8088}/ari/events?app=calls&api_key={creds}&subscribeAll=false"
                )
                while True:
                    message = ws.recv_text()
                    if message is None:
                        break
                    self.event(json.loads(message))
            except (OSError, ValueError):
                time.sleep(2)


if __name__ == "__main__":
    Bridge().run()
