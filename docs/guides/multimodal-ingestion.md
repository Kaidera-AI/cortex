# Multimodal Ingestion — Voice, Video and Images

> **Question this answers:** can Cortex ingest voice recordings, videos and images —
> not just documents and PDFs?

**Short answer: audio and images, yes — through the CLI and the `POST /artifacts`
API. Video, no — not yet.** The worker containers exist and contain real, working
code, but they are profile-gated, internal-only, and nothing in the API request
path calls them. The path that actually works today runs the transcription and
vision models **on the host**, via the CLI, and stores the results as L5
artifacts.

This guide documents what is real, what is scaffolded, and exactly how to use
what works.

## What it is

Multimodal ingestion in Cortex has three layers:

1. **L5 artifact store** (`artifacts` table, `POST /artifacts`) — the durable,
   searchable record. Every modality lands here as a row with a `modality`
   label (`audio`, `image`, `pdf`, `diagram`, `table`, `code`, `text`),
   the extracted text in `raw_content`, and provenance in `metadata`.
2. **Host-side CLI extractors** — `cortex-ingest-audio` and
   `cortex-ingest-artifact` run Whisper (speech-to-text) and a local Ollama
   VLM (image description) on the machine running the command, then write the
   result through `POST /artifacts`. **This is the production path today.**
3. **Worker containers** (`cortex-audio-worker`, `cortex-vision-worker`) —
   self-contained FastAPI services in the compose stack. The code is real and
   functional, but they run only under the `full` compose profile, are
   reachable only inside `cortex-net`, and the Cortex API never proxies to
   them (they appear solely in `GET /workers/health`). Treat them as a
   built-but-unwired alternative implementation, not the live path.

## Why it exists

The Kaidera OS platform needed agents and users to contribute more than text:
voice memos from phones, UI screenshots, whiteboard photos, architecture
diagrams, meeting recordings. Text-only memory loses all of that. The L5
multimodal artifact tier ("L5 Multimodal Artifacts" in the Cortex memory
model) exists so that a screenshot or a voice note becomes searchable project
memory the same way a decision or a lesson does — extracted into text, stored
with provenance, and returned by search.

## How it works

```mermaid
flowchart LR
    subgraph Inputs
        A[Audio file<br/>.mp3 .wav .m4a .aac .flac .ogg]
        I[Image file<br/>.png .jpg .jpeg .gif .webp .bmp .tiff .heic]
        V[Video file<br/>NOT SUPPORTED]
    end

    subgraph Host["Host-side extraction (live path)"]
        W[whisper / whisper-cli<br/>local transcription]
        O[Ollama VLM on 127.0.0.1:11434<br/>qwen2.5vl / gemma3]
    end

    subgraph Containers["Worker containers (full profile, internal-only)"]
        AW[cortex-audio-worker :9003<br/>POST /transcribe]
        VW[cortex-vision-worker :9002<br/>POST /describe-image]
    end

    subgraph Cortex
        API[POST /artifacts<br/>cortex-api]
        DB[(artifacts table<br/>L5 store)]
        S[/search<br/>stage 0.5 lexical + trigram]
    end

    A -->|cortex-ingest-audio| W --> API
    I -->|cortex-ingest-artifact| O --> API
    V -.->|no ingestion path| API
    AW -.->|not called by API| API
    VW -.->|not called by API| API
    API --> DB --> S
```

The flow for a working ingest:

1. The CLI hashes the file (SHA-256), infers `modality` from the extension,
   and extracts text:
   - **Audio** → Whisper transcript (`extraction_method: transcribed`)
   - **Image** → VLM description (`extraction_method: vlm_enriched`), or
     dimensions + metadata only when no VLM is configured
     (`extraction_method: metadata_only`)
2. `POST /artifacts` upserts the row (idempotent on
   `project + source_file + content_hash`), optionally attaches an
   `artifact_edges` graph edge, and emits a team event.
3. Search stage 0.5 matches artifacts lexically (trigram + ILIKE) across
   `raw_content`, `caption`, `source_file` and `modality`, labelled by
   modality as the result category.

## How to use it

### Audio — `cortex-ingest-audio`

```bash
# Transcribe and ingest a voice memo
cortex-ingest-audio ~/recordings/standup-2026-09-01.m4a quill kaidera-os

# Higher-quality model (default is `base`)
cortex-ingest-audio meeting.wav quill kaidera-os --whisper-model medium

# You already have a transcript — skip transcription
cortex-ingest-audio call.mp3 quill kaidera-os --transcript-file call.txt

# Link the artifact into the knowledge graph
cortex-ingest-audio memo.m4a quill kaidera-os \
  --edge-type informs --target-type decision --target-ref "<decision-id>"

# Prove the write before doing it
cortex-ingest-audio memo.m4a quill kaidera-os --dry-run
```

The script stores the audio file path as the artifact source and the
transcript as `raw_content`, with `transcription_tool`, `audio_source` and
`transcript_source` recorded in metadata.

### Images — `cortex-ingest-artifact`

There is no dedicated `cortex-ingest-image` command; images go through the
generic artifact ingester, which detects them by extension:

```bash
# Ingest a screenshot — VLM-described if local Ollama is running
cortex-ingest-artifact ~/shots/dashboard-v2.png quill kaidera-os

# Force modality and add context
cortex-ingest-artifact whiteboard.heic quill kaidera-os \
  --modality image --section-context "Q3 planning whiteboard"
```

With a VLM configured, the image gets a structured description (UI surface,
navigation, sections, statuses, notable text; or diagram components and
relationships) stored as `raw_content`. Without one, the artifact is stored
with dimensions and file metadata only.

### Video

There is **no video ingestion path**. No CLI command, no modality detection
for `.mp4`/`.mov`/`.mkv`, no frame extraction. The workaround is to extract
the audio track and ingest it as audio:

```bash
ffmpeg -i recording.mp4 -vn -acodec copy audio-only.m4a
cortex-ingest-audio audio-only.m4a quill kaidera-os
```

### The API directly

```bash
# Store any extracted content as an artifact
curl -X POST "${CORTEX_API_URL}/artifacts" \
  -H "X-Agent-Name: quill" -H "X-Project: kaidera-os" \
  -H "Content-Type: application/json" \
  -d '{
    "source_file": "/path/to/memo.m4a",
    "content_hash": "<64-char sha256 of the file>",
    "modality": "audio",
    "extraction_method": "transcribed",
    "raw_content": "...transcript text...",
    "caption": "Standup memo 2026-09-01",
    "metadata": {"transcription_tool": "whisper:medium"}
  }'

# Check worker container health (full profile only)
curl -H "X-Project: kaidera-os" "${CORTEX_API_URL}/workers/health"
```

Inside `cortex-net` (with the `full` profile up), the workers themselves
accept multipart uploads directly: `POST :9003/transcribe` (audio file,
optional `model` form field) and `POST :9002/describe-image` (image file,
optional `prompt` and `model`). Nothing in cortex-api calls these today.

## What to set up

### Audio transcription (host path — what the CLI uses)

- **Option A:** `pip install openai-whisper` — provides the `whisper` CLI.
  Models download on first use. `base` is the default; `medium`/`large` are
  materially better and materially slower.
- **Option B:** `brew install whisper-cpp` — provides `whisper-cli`; pass a
  GGML model with `--whisper-model-file`, or let the script auto-discover
  `ggml-tiny.en.bin` under the vendor directory.
- **Neither installed?** You must pass `--transcript-file`; the command
  fails loudly otherwise.

### Image analysis (host path)

- Install Ollama on the host and pull a supported VLM — the ingester probes,
  in order: `qwen2.5vl:7b`, `qwen2.5vl:3b`, `gemma3`, `gemma3:4b`
  (`ollama pull qwen2.5vl:7b`).
- No Ollama or no pulled model → the image is still ingested, but as
  `metadata_only` with `parser_status: vlm_not_configured`. No silent
  failure; the status is in the artifact metadata.

### Worker containers (optional, unwired)

```bash
docker compose -f .agents/docker-compose.cortex.yml --profile full up -d \
  cortex-audio-worker cortex-vision-worker
```

- Audio worker: first request downloads the Whisper model into the
  `cortex-models` volume; override with `WHISPER_CACHE_DIR`.
- Vision worker: first request pulls `qwen3-vl:4b` (~8 GB) into the
  `cortex-models` volume; override with `CORTEX_VISION_MODEL`. Memory limit
  is 8 GB — allow for it.
- Both bind inside `cortex-net` only (no host port published).

## Limits — stated honestly

- **Video is not supported.** No modality detection, no CLI command, no
  frame or audio-track extraction. The ffmpeg workaround above is manual.
- **The worker containers are not wired in.** Real code, real models, but
  profile-gated (`full`), internal-only, and absent from the API request
  path. There are currently **two parallel implementations** (host CLI and
  containers) and only the host one is live. Productionising means either
  proxying API routes to the workers or deleting them.
- **Artifacts are not vector-embedded.** `artifacts` is not in the embedding
  backfill set and has no `search_vector` backfill — artifact content is
  searchable **lexically only** (trigram/ILIKE). Semantic ("find me things
  like this") search does not reach transcripts or image descriptions yet.
- **Transcription quality depends on the model you choose.** The default
  Whisper `base` model is fast but rough on accents, crosstalk and domain
  jargon; the vision defaults are 3–7 B parameter models that summarise
  rather than transcribe dense text in screenshots.
- **`raw_content` is truncated at 120,000 characters** (loudly, with a
  stderr warning and `truncated: true` in metadata — LCX-UR-002). Long
  meeting transcripts lose their tail.
- **Cost and latency are local but real.** Whisper `large` on CPU is slow;
  the first vision call pulls ~8 GB; image enrichment blocks ingest for up
  to 180 s per image. There are no rate limits because everything runs on
  your own hardware — the constraint is RAM and patience, not quota.
- **HEIC and less common formats** depend on Pillow/ffmpeg builds on the
  host; exotic codecs can fail extraction while still being stored as
  metadata-only artifacts.

## Verification

An ingest is done when search returns the content, not when the command
exits 0:

```bash
cortex-search "phrase from the transcript" kaidera-os
# or filter by modality via the API search stage labelled "artifacts"
```

Check the artifact row's `extraction_method` — `transcribed` or
`vlm_enriched` means real content landed; `metadata_only` means the file was
recorded but its content was not extracted.
