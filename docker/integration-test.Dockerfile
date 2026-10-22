# Build the normal CPU tts image first: docker compose build tts.
# Test-only image: no keys, no live stand, local models mounted read-only by caller.
FROM rust:1.98-slim-bookworm AS rust
FROM clinic-voice-tts:latest
USER root
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup PATH=/usr/local/cargo/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev git && rm -rf /var/lib/apt/lists/*
COPY requirements/tts.txt requirements/asr.txt requirements/nlu-semantic.txt requirements/editor.txt /app/requirements/
RUN --mount=type=cache,id=clinic-voice-pip-modules,sharing=locked,target=/root/.cache/pip pip install -r /app/requirements/tts.txt -r /app/requirements/asr.txt -r /app/requirements/nlu-semantic.txt -r /app/requirements/editor.txt pytest==9.1.0 pytest-timeout==2.4.0
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 CARGO_BUILD_JOBS=2 APP_MODE=demo
WORKDIR /workspace
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src/bin && touch src/main.rs src/lib.rs src/bin/bench_nlu.rs build.rs && cargo fetch --locked
ENTRYPOINT ["sh","-c"]
