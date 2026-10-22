"""ASR gateway on top of ``onnx-asr`` (GigaAM v3 e2e CTC/RNN-T, T-one) – batch transcription of a finished utterance.

The orchestrator sends 8 kHz PCM16 (base64) or a text (stub mode).  Streaming partials come from the
Deeray runtime WebSocket when ``ASR_URL`` points at it; this adapter is the self-hosted MIT/Apache fallback.
"""

from __future__ import annotations

import base64
from collections import Counter
import json
from pathlib import Path
import tempfile
import time
import os
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class OnnxAsrModule:
    model_name: str = "gigaam-v3-e2e-rnnt"
    engine: str = "onnx-asr"
    _model: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        import onnx_asr  # type: ignore[import-not-found]
        import onnxruntime

        onnxruntime.disable_telemetry_events()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = int(os.environ.get("ASR_THREADS", "2"))
        options.inter_op_num_threads = 1
        self.provider = os.environ.get("ASR_PROVIDER", "CPUExecutionProvider")
        self.device_id = int(os.environ.get("ASR_CUDA_DEVICE_ID", "0"))
        self.cuda_verified = False
        self.execution_profiles: dict[str, dict] = {}
        self._verification_error: str | None = None
        cuda = self.provider == "CUDAExecutionProvider"
        if cuda:
            # The GPU wheel's optional NVIDIA packages need loading before ORT
            # creates sessions. Missing libraries still fail the checks below.
            onnxruntime.preload_dlls()
        if self.provider not in onnxruntime.get_available_providers():
            raise RuntimeError(f"ASR provider unavailable: {self.provider}")
        providers: list = [self.provider]
        if cuda:
            memory_mib = int(os.environ.get("ASR_GPU_MEM_LIMIT_MIB", "1024"))
            if memory_mib <= 0 or self.device_id < 0:
                raise ValueError("ASR CUDA memory must be positive and device ID nonnegative")
            providers = [(self.provider, {
                "device_id": self.device_id,
                # This bounds each session's arena, not CUDA context/library memory.
                "gpu_mem_limit": memory_mib * 1024 * 1024,
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
                "cudnn_conv_use_max_workspace": "0",
            })]
            profile_dir = Path(os.environ.get("ASR_PROFILE_DIR") or tempfile.mkdtemp(prefix="clinic-asr-cuda-"))
            profile_dir.mkdir(parents=True, exist_ok=True)
            options.enable_profiling = True
            options.profile_file_prefix = str(profile_dir / "ort")
        quantization = os.environ.get("ASR_QUANTIZATION", "none" if cuda else "int8")
        self._model = onnx_asr.load_model(
            self.model_name, path=os.environ.get("ASR_MODEL_PATH") or None,
            quantization=None if quantization == "none" else quantization,
            sess_options=options, providers=providers,
            # FFT/features stay on CPU; neural network sessions use the requested EP.
            preprocessor_config={"use_numpy_preprocessors": True, "max_concurrent_workers": 1},
            resampler_config={"providers": ["CPUExecutionProvider"], "sess_options": options},
        )
        self._sessions = {
            name.lstrip("_"): value
            for name, value in vars(self._model.asr).items()
            if isinstance(value, onnxruntime.InferenceSession)
        }
        if cuda and not self._sessions:
            raise RuntimeError("ASR CUDA model has no inspectable ONNX sessions")
        for name, session in self._sessions.items():
            actual = session.get_providers()
            if not actual or actual[0] != self.provider:
                raise RuntimeError(f"ASR {name} requested {self.provider}, initialized {actual}")
            # Do not recreate the session on CPU after an execution-provider error.
            session.disable_fallback()
        self._lock = threading.Lock()

    def _verify_cuda_execution(self) -> None:
        """Close first-utterance profiles and require actual GPU neural kernels.

        CPU shape/control operators are allowed. CPU Conv/MatMul/LSTM kernels
        are not: quantized weights may register a CUDA EP while doing model
        computation on CPU. The profile makes that failure visible.
        """
        failures = []
        for name, session in self._sessions.items():
            profile = session.end_profiling()
            profile_path = Path(profile)
            events = json.loads(profile_path.read_text())
            # ORT timestamps can coincide for sessions created in one second.
            profile_path = profile_path.with_name(f"{name}-{profile_path.name}")
            profile_path.write_text(json.dumps(events))
            profile = str(profile_path)
            counts: Counter[str] = Counter()
            gpu_compute = 0
            cpu_compute: Counter[str] = Counter()
            for event in events:
                args = event.get("args", {})
                provider, op = args.get("provider"), args.get("op_name", "")
                if not provider:
                    continue
                counts[f"{provider}:{op}"] += 1
                heavy = any(part in op.lower() for part in ("conv", "matmul", "gemm", "lstm", "attention"))
                if heavy and provider == "CUDAExecutionProvider":
                    gpu_compute += 1
                elif heavy:
                    cpu_compute[op] += 1
            self.execution_profiles[name] = {
                "file": profile, "providers": session.get_providers(),
                "cuda_compute_kernels": gpu_compute,
                "cpu_compute_kernels": dict(cpu_compute), "kernel_counts": dict(counts),
            }
            if not gpu_compute or cpu_compute:
                failures.append(f"{name}: CUDA compute kernels={gpu_compute}, CPU compute={dict(cpu_compute)}")
        if failures:
            self._verification_error = "ASR CUDA execution verification failed: " + "; ".join(failures)
            raise RuntimeError(self._verification_error)
        self.cuda_verified = True

    @staticmethod
    def _to_16k(pcm: bytes, rate: int) -> np.ndarray:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if rate == 16000 or len(x) == 0:
            return x
        n = int(len(x) * 16000 / rate)
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        if op != "transcribe":
            return "error", {"error": f"unknown op {op}"}
        if "pcm_b64" not in args:
            return "error", {"error": "real ASR requires audio"}
        pcm = base64.b64decode(args["pcm_b64"], validate=True)
        if len(pcm) % 2 or int(args.get("rate", 8000)) not in (8000, 16000, 24000, 48000):
            return "error", {"error": "invalid PCM16 audio/rate"}
        x = self._to_16k(pcm, int(args.get("rate", 8000)))
        # A finished short utterance may have no acoustic context around its
        # endpoints (e.g. a subsecond confirmation). The RNN-T then emits only
        # blanks. Pad the actual waveform, never replace it with expected text.
        # Keep the original duration for honest inference/latency accounting.
        audio_s = len(x) / 16000 or 1e-6
        if 0 < len(x) <= 32000:
            x = np.pad(x, (3200, 3200))
        t0 = time.perf_counter()
        with self._lock:
            if self._verification_error:
                raise RuntimeError(self._verification_error)
            text = str(self._model.recognize(x, sample_rate=16000))
            if self.provider == "CUDAExecutionProvider" and not self.cuda_verified:
                self._verify_cuda_execution()
        dt = time.perf_counter() - t0
        return "ok", {"text": text, "final": True, "engine": f"{self.engine}:{self.model_name}", "rtf": round(dt / audio_s, 3),
                      "provider": self.provider, "device_id": self.device_id if self.provider == "CUDAExecutionProvider" else None,
                      "cuda_verified": self.cuda_verified}
