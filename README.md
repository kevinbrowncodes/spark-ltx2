# spark-ltx2 — LTX-2.3 Image-to-Video on the DGX Spark

A self-contained, **curl-able Image-to-Video service** for the NVIDIA DGX
Spark (GB10 Grace-Blackwell, aarch64, ~128 GB unified memory).

```
image + prompt  →  LTX-2.3 I2V  →  vertical / landscape mp4
```

Everything runs locally on the Spark. Weights, prompts, and frames never leave
the box — the text encoder is the local Gemma-3-12B, no cloud API key.

---

## 1. What this is

Two Docker containers, fully isolated from anything else on the host:

| Service   | Container       | Port (host) | Role |
|-----------|-----------------|-------------|------|
| `comfyui` | `ltx2-comfyui`  | `8189`      | ComfyUI + **official Lightricks LTX-2.3 nodes** (+ RES4LYF samplers, currently unused). Does the actual rendering on the GB10 GPU. |
| `api`     | `ltx2-api`      | `8090`      | Thin FastAPI front. Injects `image` + `prompt` (+ size, frames, seed) into the I2V workflow JSON and submits it to ComfyUI's `/prompt` endpoint. |

> The bundled ComfyUI lives **entirely inside this stack**. It never touches
> any existing host ComfyUI install — different binaries, different port
> (`8189`, not `8188`).

**Scope (current):** single-image I2V, prompt-conditioned, vertical 9:16 by
default, local Gemma-3 text encoder.

**Intentionally not exposed yet:** text-to-video, multi-keyframe,
audio-conditioned, video-to-video, latent upscale. The workflow uses LTX-2.3's
**video-only** path; see [Audio status](#audio-status).

---

## 2. Hardware & runtime assumptions

This stack is tuned specifically for the DGX Spark and its quirks. It will
almost certainly need adjustment to run anywhere else.

- **GPU:** NVIDIA GB10 Grace-Blackwell, compute capability **sm_121** (12.1a).
- **Memory:** **unified** — there is no separate VRAM. CPU + GPU share one
  ~121 GB pool. This drives most of the design decisions below
  (`--disable-pinned-memory`, `--reserve-vram`, fp8 preference, no concurrent
  ComfyUI stacks).
- **CUDA / torch:** the image is built on **`nvidia/cuda:13.0.2-devel-ubuntu24.04`**
  + **`torch==2.9.1+cu130`** + **SageAttention v3** compiled natively for
  `sm_121a`. **The NGC `pytorch:25.12` image is intentionally avoided** — its
  alpha torch stops at `sm_120` and produces gray/desaturated frames on GB10
  via PTX-JIT fallback. See [Why we don't use the NGC image](#why-we-dont-use-the-ngc-pytorch-image).
- **Host OS:** Ubuntu / Linux with Docker + the NVIDIA container runtime.

---

## 3. Repository layout

```
spark-ltx2/
├── README.md                                ← this file (source of truth)
├── CLAUDE.md                                ← AI assistant guardrails (workflow only)
├── docker-compose.yml                       ← both services, GB10-tuned launch flags
├── Dockerfile.comfyui                       ← ComfyUI + LTX-2.3 nodes + SageAttention sm_121a
├── Dockerfile.api                           ← slim FastAPI client
├── .env.example                             ← optional overrides (copy → .env)
├── .gitignore                               ← keeps weights / outputs / .env out of git
│
├── app/
│   └── api_server.py                        ← FastAPI → ComfyUI /prompt client (I2V only)
│
├── comfyui/
│   ├── extra_model_paths.yaml               ← maps ~/ltx2/models into ComfyUI's folders
│   └── torchaudio_stub/                     ← legacy NGC-build stub, unused on the cu130 image
│
├── workflows/
│   ├── ltx23_i2v_api.json                   ← THE I2V workflow submitted to ComfyUI (API format)
│   └── reference/                           ← official Lightricks 2.3 example workflows (read-only refs)
│
├── scripts/
│   └── download_models.sh                   ← idempotent HF download for LTX weights + Gemma encoder
│
└── docs/
    └── stories/                             ← STORY_NNN_short_slug.md files (see CLAUDE.md §3)
```

**Git-ignored, never commit:** `.env`, `models/`, `output/`, `*.safetensors`,
`*.mp4`, `__pycache__/`. Weights live outside the repo entirely (see next
section).

---

## 4. Models

Weights live **outside the repo** at `~/ltx2/models` (mounted read-only at
`/models` inside the container) and are mapped into ComfyUI's folders by
[`comfyui/extra_model_paths.yaml`](comfyui/extra_model_paths.yaml). Nothing is
copied or moved — ComfyUI just lists files at the mount point.

```
~/ltx2/models/
├── ltx-2.3-22b-dev.safetensors                       # diffusion checkpoint (~44 GB bf16)
├── ltx-2.3-22b-distilled-lora-384.safetensors        # distilled LoRA (strength 0.5 in the workflow)
└── text_encoders/
    └── gemma-3-12b-it-qat-q4_0-unquantized/          # local Gemma-3-12B text encoder (5 shards)
```

Optional: `ltx-2.3-spatial-upscaler-x2-1.0.safetensors` (latent upscaler) is
already mapped in `extra_model_paths.yaml` but **not used by the current
workflow**.

Fetch / complete the set — idempotent, resumable, won't re-download what you
have:

```bash
bash scripts/download_models.sh
```

The Gemma-3-12B encoder is the main new download (~25 GB). The LTX checkpoint
+ LoRA together are ~46 GB; total on-disk footprint is ~71 GB before any
optional weights.

> If a filename 404s on a fresh Hugging Face mirror, Lightricks may have
> shipped a `-1.1` revision. Update the filename in **both** `download_models.sh`
> **and** `workflows/ltx23_i2v_api.json`.

---

## 5. Run

```bash
cp .env.example .env                        # optional overrides
docker compose build                        # first build is long (compiles SageAttention v3)
docker compose up -d
docker compose logs -f comfyui              # watch model load on first request
```

Generate (image must already exist in `~/Documents/cosmos-media`). Generation is
**asynchronous**: `POST /generate` returns a `job_id` immediately, then you poll
`GET /jobs/{job_id}` until it is `completed`:

```bash
curl -s http://localhost:8090/health

# 1. Submit — returns a job_id right away (HTTP 202), does NOT wait for the render.
JOB=$(curl -s -X POST http://localhost:8090/generate \
  -H 'content-type: application/json' \
  -d '{
        "image": "portrait.png",
        "prompt": "a slow cinematic push-in, gentle wind in the hair",
        "negative_prompt": "worst quality, blurry, distorted",
        "width": 704, "height": 1280,
        "num_frames": 121, "frame_rate": 30, "seed": 42
      }' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# 2. Poll until completed (status: running -> completed | failed).
curl -s http://localhost:8090/jobs/$JOB
# -> {"job_id":"...","status":"completed","output":"ltx_i2v_native_xxxxx.mp4","prompt_id":"..."}
#    (the mp4 is written to ~/ltx2/output)
```

Browser access to ComfyUI for debugging: **http://localhost:8189** (the
container listens on `8188` internally; `8189` is the host-side mapping so it
never collides with another ComfyUI on `8188`).

### Constraints on request shape

The LTX-2.3 graph imposes hard divisibility rules; the API enforces both:

| Field         | Rule                                | Example values |
|---------------|-------------------------------------|----------------|
| `width`       | divisible by 32                     | 704, 736 (**not** 720) |
| `height`      | divisible by 32                     | 1280, 736 |
| `num_frames`  | of the form `8k + 1`                | 121, 193, 257 |
| `frame_rate`  | integer, written through to the video assembly | 24, 30 |

Violations come back as **HTTP 422** before anything is queued.

### Where outputs land

- Inside the container: `/opt/ComfyUI/output/`
- On the host: `~/ltx2/output/` (bind-mounted, read-write)
- `GET /jobs/{job_id}` returns just the filename in `output`; read it from
  `~/ltx2/output/<filename>`.

---

## 6. API

### `GET /health`
Returns `{"status": "ok", "comfyui": "<version>"}` if the ComfyUI container
answers `/system_stats`; 503 otherwise.

### `POST /generate` — submit a render (asynchronous)
Body (all fields optional except `prompt` and `image`):

```json
{
  "image":            "portrait.png",
  "prompt":           "...",
  "negative_prompt":  "worst quality, blurry, distorted, jittery, low resolution",
  "width":            704,
  "height":           1280,
  "num_frames":       121,
  "frame_rate":       30,
  "seed":             42
}
```

The server:
1. Validates the size + frame-count rules above (422 on failure).
2. Loads `workflows/ltx23_i2v_api.json`, walks the graph, and **patches by
   `class_type`**: `LoadImage.image`, `EmptyLTXVLatentVideo.{width,height,length}`,
   `LTXVConditioning.frame_rate`, `CreateVideo.fps`, positive vs negative
   `CLIPTextEncode.text` (distinguished by `_meta.title` containing `"neg"`),
   and any node with an `int`-valued `seed` / `noise_seed`.
3. `POST`s to ComfyUI's `/prompt` and **returns immediately** (HTTP **202**)
   with `{"job_id": "...", "prompt_id": "...", "status": "running"}`. A
   background thread watches the render; the connection is **not** held open for
   its duration.

Errors from the patcher (e.g. `LoadImage`, `EmptyLTXVLatentVideo`, or the
positive-prompt node missing from the workflow) come back as **500** with the
list of missing node roles — so if you edit the workflow JSON, that's the
signal you broke an expected role.

### `GET /jobs/{job_id}` — poll a submitted render
Returns the job record:

```json
{"job_id": "...", "status": "running", "output": null, "error": null, "prompt_id": "...", "started": 1718...}
```

- `status` is `running`, `completed` (with the mp4 filename in `output`), or
  `failed` (with the reason in `error`). The render-watcher enforces
  `LTX_POLL_TIMEOUT_S` (default 1800) as a hard deadline and marks the job
  `failed` on timeout or any internal error — a finished render is never trapped
  behind a hung request (see [BUG_001](docs/bugs/BUG_001_generate_hangs_after_render.md)).
- An unknown `job_id` returns **404**. Job records are kept in memory and pruned
  ~1 h after they finish (lost on api restart).

### Environment variables (read by `api_server.py`)

| Var                   | Default                             | Purpose |
|-----------------------|-------------------------------------|---------|
| `COMFYUI_URL`         | `http://comfyui:8188`               | Internal Docker network address of the ComfyUI service. |
| `LTX_WORKFLOW`        | `/app/workflows/ltx23_i2v_api.json` | Path to the workflow JSON inside the api container. |
| `OUTPUT_DIR`          | `/output`                           | Where finished mp4s live (read-only mount of `~/ltx2/output`). |
| `LTX_POLL_TIMEOUT_S`  | `1800`                              | Hard deadline for the background render-watcher; past it the job is marked `failed` (timeout). Some full-res renders exceed this — raise it if `/jobs/{id}` reports a timeout. |

---

## 7. The I2V workflow

Source of truth: [`workflows/ltx23_i2v_api.json`](workflows/ltx23_i2v_api.json).

Graph (high level):

```
LoadImage ─► LTXVPreprocess ─┐
                             ▼
   EmptyLTXVLatentVideo ─► LTXVImgToVideoConditionOnly ─► (latent)
                                                          │
   CLIP positive ─┐                                       │
                  ├─► LTXVConditioning ─► CFGGuider ──────┤
   CLIP negative ─┘            (cfg=3.0)                  │
                                                          ▼
        KSamplerSelect (euler) + LTXVScheduler ─► SamplerCustomAdvanced
                                                          │
                                                          ▼
                                       LTXVTiledVAEDecode (2×2 tiles, overlap 6)
                                                          │
                                                          ▼
                                            CreateVideo (fps) ─► SaveVideo (mp4)
```

Notable choices baked into the JSON:

- **`LoraLoaderModelOnly` with the distilled LoRA at `strength_model: 0.5`** —
  the distilled LoRA + euler at 8 steps is what makes single-stage I2V fast
  (~18 s on a 320×320×9-frame test).
- **`LTXVGemmaCLIPModelLoader`** with the local Gemma-3-12B shards — no cloud
  text-encode path.
- **`LTXVImgToVideoConditionOnly.strength: 0.5`** — tuned for natural motion
  (commit `b8ef1b3`); cranking this toward 1.0 freezes the image.
- **`LTXVTiledVAEDecode` 2×2 tiles, overlap 6, `working_dtype: auto`** — the
  memory-safe decode path; byte-matches the canonical reference.
- **`SamplerCustomAdvanced` with native `euler` + `CFGGuider` (cfg 3.0)**, not
  RES4LYF's `ClownSampler_Beta`. See [Audio status](#audio-status) for why.

### Reference workflows

[`workflows/reference/`](workflows/reference/) holds the official Lightricks
2.3 example graphs (I2V, HDR, lipdub, motion-track, union-control). They are
**read-only references** — if behavior in our workflow looks off, diff against
the closest reference graph before touching nodes.

---

## 8. The GB10 quirks (why the Dockerfile looks weird)

Three problems specific to the DGX Spark dictate most of the launch flags and
build choices:

### 8a. The gray/desaturated output bug

LTX-2.3 on the Spark would render usable-looking previews whose SATAVG was
~6 (vs ~30–80 for normal color, ~19 for the source still). This was
**not** a decode bug — every cached-latent decode test (`auto` vs `float32`,
`LTXVTiledVAEDecode` vs `VAEDecode`, full-range re-encode) was byte-identical.
The corruption was upstream, in the **sampled latents**.

After ruling out runtime (the cu130 + SageAttention rebuild gave the same
gray as the NGC alpha torch), the culprit turned out to be **the workflow
itself**: RES4LYF's `ClownSampler_Beta` + the LTX-2.3 multimodal A/V machinery
(`LTXVEmptyLatentAudio`, `LTXVAudioVAELoader`, `LTXVConcatAVLatent`,
`MultimodalGuider`, `LTXVSeparateAVLatent`). Swapping to a native video-only
path (`KSamplerSelect` `euler` + `CFGGuider` + `LTXVScheduler` +
`SamplerCustomAdvanced` + `VAEDecode`) fixed color **and** dropped a small
test from 30+ min to 18 s.

That is the current workflow.

### 8b. Why we don't use the NGC PyTorch image

The NGC `pytorch:25.12` image ships an **alpha** torch (`2.10.0a0+...nv25.12`)
whose compiled kernels stop at `sm_120` — there are **no native sm_121
kernels** for the GB10. Running LTX-2.3 through `sm_120` forward-compat /
PTX-JIT produces subtly wrong numerics — not a crash, just visibly degraded
output. Every known-good DGX Spark LTX image fixes this the same way:

- Stable `torch==2.9.1+cu130` on `nvidia/cuda:13.0.2-devel-ubuntu24.04`
- **SageAttention v3** built with
  `-gencode arch=compute_121a,code=sm_121a` (`TORCH_CUDA_ARCH_LIST=12.1a`)
- ComfyUI launched with `--use-sage-attention`

(Reference recipes: [`AEON-7/comfyui-aeon-spark`](https://github.com/AEON-7/comfyui-aeon-spark),
[`ecarmen16/SparkyUI`](https://github.com/ecarmen16/SparkyUI), Comfy-Org/ComfyUI
issue #11864.)

### 8c. Unified-memory OOM, not heat

When the Spark crashes during a render, **it's unified-memory exhaustion**,
not thermal throttling. Evidence (`journalctl -b -1`): NVRM
`Out of memory [NV_ERR_NO_MEMORY]`, kernel `oom-killer` taking out VS Code,
`global_oom`, "Under memory pressure, flushing caches" right before the
crash; temps were 60–72 °C the whole time.

LTX-2.3 loads ~86 GB into a 121 GB pool shared with the OS + Docker + VS Code.
One render ≈ 106–111 GB. A concurrent `docker build`, a second ComfyUI stack,
or a heavy browser tab tips it over.

**Mitigations baked into this stack (priority order):**

1. Prefer the **fp8 fused checkpoint** (~23 GB) when available — biggest
   single win, ~20 GB headroom. The current workflow points at the bf16
   `dev` checkpoint; swap the `ckpt_name` in `workflows/ltx23_i2v_api.json`
   if you have the fp8 file on disk.
2. **Run only one ComfyUI stack at a time.** Stop any host ComfyUI
   (`comfyui` / `sparkyui` / `comfyuimini`) before `docker compose up`.
3. **Never build + render concurrently.** `docker compose build` while a
   render is running is a near-guaranteed OOM.
4. Compose flags that buy headroom: `--disable-pinned-memory` (critical for
   Grace-Blackwell coherent memory), `--reserve-vram 2.0`, `--bf16-unet
   --bf16-vae --bf16-text-enc` (unified memory makes bf16 free).
5. `TORCH_COMPILE_DISABLE=1` + `TORCHDYNAMO_DISABLE=1` in the Dockerfile —
   dynamo JIT fails on sm_121a.
6. `PYTORCH_ALLOC_CONF=expandable_segments:True` — lets the allocator grow
   without fragmenting.

---

## 9. Audio status

The working workflow produces **video only**. LTX-2.3 *is* a joint
audio+video model, so audio is possible in principle — we just don't have it
in this stack.

The fix for the gray-output bug removed **two things at once**: the RES4LYF
`ClownSampler_Beta` **and** the multimodal A/V machinery. So we don't yet
know which one caused the gray:

- (a) If it was just RES4LYF → a **native multimodal guider** could give
  audio + correct color. Audio is achievable.
- (b) If the multimodal A/V latent handling itself corrupts the video latent
  on GB10 → audio is blocked until an upstream (Lightricks / ComfyUI-LTXVideo)
  fix.

The 2.2 (video-only) vs 2.3 (multimodal) anecdote — 2.2 works fine on this
box — leans toward (b), but it's not proven.

**Cheap retest loop:** tiny renders at **320×320 / 9 frames / 8 steps** run
in ~18 s. To probe audio: reintroduce the multimodal A/V path but with
**native** sampling (not `ClownSampler`), then (1) measure SATAVG (gray
threshold ~6 vs source ~19), and (2) `ffprobe` the output for an audio stream.

---

## 10. Performance notes

From real runs on the Spark (704×1280, 121 frames, 15 steps, `res_2s`,
seed 42, RES4LYF workflow — the old slow path):

- **Cold run:** 26:11
- **Warm run (same process, models + kernels hot):** 23:08

The warm run saved only ~3 min, meaning the **sampling loop** (~23 min)
dominates, not model load. The persistent-CUDA-kernel-cache idea in
[`docs/stories/cuda-kernel-cache.md`](docs/stories/cuda-kernel-cache.md) is
therefore low value here — it would only recover the ~3 min cold slice.

After moving to the **native euler** path (current workflow), a small
320×320×9-frame test runs in **~18 s** — so the new path is dramatically
faster than the numbers above. Full-res native-path timings haven't been
re-measured yet; treat ~5–10 min as a reasonable expectation rather than the
older 23–32 min figures.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl /health` 503 | `comfyui` container not up, or still loading models | `docker compose logs -f comfyui` until "Starting server" |
| 422 `width and height must be divisible by 32` | Used 720 instead of 704/736 | Round to the nearest multiple of 32 |
| 422 `num_frames must be 8k+1` | Used 120 / 128 / 144 | Use 121, 193, 257, … |
| `/jobs/{id}` reports `failed` with a timeout after 1800 s | Real render is taking longer than `LTX_POLL_TIMEOUT_S` | Raise `LTX_POLL_TIMEOUT_S`, or render at smaller resolution |
| 500 `Workflow missing expected node(s): LoadImage / positive prompt / latent size` | You edited the workflow JSON and removed/renamed a required node | Restore the role; the patcher matches by `class_type` (and `_meta.title` for pos/neg) |
| Gray / washed-out video | Workflow was edited toward the RES4LYF + multimodal path | Revert `workflows/ltx23_i2v_api.json` to the native `euler` + `CFGGuider` graph; verify SATAVG with `ffprobe` |
| Host crashes / `oom-killer` in dmesg | Unified-memory OOM (§8c) | One ComfyUI stack at a time; no concurrent `docker build`; prefer fp8 checkpoint |
| `Sampler` ComfyUI node missing in logs | RES4LYF samplers didn't install | The current native workflow doesn't use them; ignore. The Dockerfile install is best-effort (`|| echo WARN`) |
| Output still gray even after revert | Possible drift in the encoder shards or the LoRA filename in the JSON | Diff against `workflows/reference/LTX-2.3_T2V_I2V_Single_Stage_Distilled_Full.json` |

---

## 12. License & origin

This repository wires together open-source components — see each upstream for
its license:

- ComfyUI ([`comfyanonymous/ComfyUI`](https://github.com/comfyanonymous/ComfyUI))
- ComfyUI-LTXVideo + LTX-2.3 weights
  ([`Lightricks/ComfyUI-LTXVideo`](https://github.com/Lightricks/ComfyUI-LTXVideo),
  [`Lightricks/LTX-2.3`](https://huggingface.co/Lightricks/LTX-2.3))
- RES4LYF ([`ClownsharkBatwing/RES4LYF`](https://github.com/ClownsharkBatwing/RES4LYF))
- SageAttention ([`thu-ml/SageAttention`](https://github.com/thu-ml/SageAttention))
- Gemma-3-12B ([`google/gemma-3-12b-it-qat-q4_0-unquantized`](https://huggingface.co/google/gemma-3-12b-it-qat-q4_0-unquantized))

The glue code (`Dockerfile.*`, `docker-compose.yml`, `app/api_server.py`,
`workflows/ltx23_i2v_api.json`, `scripts/download_models.sh`,
`comfyui/extra_model_paths.yaml`) is the contribution of this repo.
