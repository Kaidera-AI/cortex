# Ingest

Cortex turns sessions, documents, audio, images, and video audio tracks into project-scoped,
searchable memory. The durable boundary is always a typed API write; a local parser or model
process is never allowed to become a second database path.

> **Extraction status:** the standalone v0.1.0 payload is still being extracted. The
> contracts below describe the production Kaidera OS lineage. Confirm each command and the
> `multimodal` profile in discovery before relying on them in this checkout.

## Inputs and paths

| Input | Command or route | Extraction path | Durable result |
|---|---|---|---|
| Claude/Codex/Beat session | `cortex-ingest-session`, `cortex-ingest-codex`, batch commands | Typed session parser and `POST /sessions/ingest` | Session and messages, atomically replaced on re-ingest |
| Text/code/table/PDF | `cortex-ingest-artifact` | Local deterministic parser where available | L5 artifact via `POST /artifacts` |
| Audio | `cortex-ingest-audio` or `POST /artifacts/transcribe` | Internal Whisper worker | Transcript in artifact `raw_content` |
| Image | `cortex-ingest-image` or `POST /artifacts/describe-image` | Internal Ollama/VLM worker | Description in artifact `raw_content` |
| Video | `cortex-ingest-video` or `POST /artifacts/transcribe` | ffmpeg mono 16 kHz audio extraction, then Whisper | Audio-track transcript, modality `video` |

Detailed media endpoints, profiles, failure modes, and acceptance evidence:
[Multimodal ingestion](multimodal-ingestion.md).

The media commands share one dispatcher. They stream multipart files to `cortex-api`, which
checks the project and writer before proxying to the internal worker on `cortex-net`. The
CLI then writes the result through `POST /artifacts`. Host Whisper, host `whisper-cli`, host
Ollama, and direct SQL are not fallback paths.

## Start the optional workers

The source Compose file provides a targeted `multimodal` profile; `full` selects the same
audio and vision workers for older operator workflows. Neither profile is a core API
dependency: Cortex starts without them, while media routes fail visibly with `502`.

```bash
# Source-qualification host using Podman Compose. Not a managed installer command.
podman compose -f .agents/docker-compose.cortex.yml \
  --profile multimodal up -d cortex-audio-worker cortex-vision-worker

curl -H "X-Project: my-project" \
  "${CORTEX_API_URL:-http://localhost:8501}/workers/health"
```

The current managed installer and Apple runtime still hold media profiles until their image
and runtime UAT gates accept them. Do not treat a source Compose start as release evidence.

## Use the CLIs

```bash
cortex-ingest-audio standup.m4a kai my-project
cortex-ingest-audio call.wav kai my-project --model medium
cortex-ingest-audio call.wav kai my-project --transcript-file call.txt

cortex-ingest-image dashboard.png kai my-project \
  --prompt "Describe the visible status and navigation"

cortex-ingest-video demo.webm kai my-project --model base

# Generic ingestion selects the same worker path by extension.
cortex-ingest-artifact architecture.png kai my-project
```

All wrappers also accept source type, customer/organisation boundaries, section context,
metadata, optional artifact edges, and `--dry-run`. For audio/video, `--transcript-file`
intentionally bypasses inference while preserving the supplied transcript's provenance.

## Vector-search contract

Artifacts are idempotent on `(project, source_file, content_hash)`. Backfill text is chosen
in this order:

1. non-empty `raw_content`;
2. non-empty `caption`;
3. `source_file`.

The artifact column and search path use the same 768-dimensional embedding contract as the
rest of Cortex. `cortex-embed --table artifacts` targets only artifact backlog;
`cortex-embed --table all` includes it. Search combines the existing lexical/trigram stage
with an artifact vector stage.

## Rules

- **Point ingestion at an explicit corpus or file.** Never ingest a repository root or home
  directory into project memory.
- **Use a registered writer and explicit project.** Media inference is not an anonymous
  model endpoint.
- **Treat `--dry-run` honestly.** It can prove payload construction, not a database write,
  embedding, or retrieval effect.
- **Verify the whole loop.** Done means worker output, artifact ID, embedding coverage, and
  a search hit from the intended project.
- **Expect loud truncation.** Artifact `raw_content` is capped at 120,000 characters; the
  CLI records and warns on truncation, or fails when `CORTEX_ARTIFACT_FAIL_ON_TRUNCATE=1`.

## Limits

- Video support transcribes the audio track. It does not extract keyframes or describe the
  visual timeline.
- The vision model is pulled lazily into the shared model volume, so the first request can
  be slow and needs enough disk.
- A healthy worker proves reachability, not transcription quality or search acceptance.
- PDF and deterministic document extraction remain separate from the optional audio/vision
  workers.
