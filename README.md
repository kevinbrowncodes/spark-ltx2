# spark-ltx2 — LTX-2.3 Image-to-Video on DGX Spark

A self-contained, curl-able **Image-to-Video** service for the NVIDIA DGX
Spark (GB10 Grace-Blackwell, aarch64, ~128 GB unified memory).

```
image + prompt  →  LTX-2.3 I2V  →  vertical/landscape video
```

Two containers, isolated from anything else on the box:

| Service   | Container       | Port (host) | Role |
|-----------|-----------------|-------------|------|
| `comfyui` | `ltx2-comfyui`  | `8189`      | ComfyUI + **official Lightricks LTX-2.3 nodes** (+ RES4LYF samplers). Does the rendering. |
| `api`     | `ltx2-api`      | `8090`      | Thin FastAPI front. Injects image+prompt into the LTX-2.3 I2V workflow and submits it to ComfyUI. |

> The bundled ComfyUI lives **entirely inside this stack**. It does **not**
> touch or run alongside any existing host ComfyUI — different install,
> different port (8189, never 8188).

**Scope (current):** single-image I2V, prompt-conditioned, vertical 9:16
supported, local Gemma-3 text encoder (no cloud API, prompts never leave the
box). Intentionally *not* implemented yet: text-to-video, multi-keyframe,
audio-conditioned, video-to-video, latent upscale.

## Models

Weights live outside the repo at `~/ltx2/models` (mounted read-only at
`/models`) and are mapped into ComfyUI's folders by
[`comfyui/extra_model_paths.yaml`](comfyui/extra_model_paths.yaml) — no copying
or moving the ~51 GB.

```
~/ltx2/models/
├── ltx-2.3-22b-dev.safetensors                 # checkpoint (full/dev)
├── ltx-2.3-22b-distilled-lora-384.safetensors  # distilled LoRA
└── text_encoders/
    └── gemma-3-12b-it-qat-q4_0-unquantized/    # local text encoder
```

Fetch / complete the set (idempotent — won't re-download what you have; the
Gemma encoder is the main new download):

```bash
bash scripts/download_models.sh
```

## Run

```bash
cp .env.example .env            # optional overrides
docker compose build            # builds the ComfyUI image (long first time)
docker compose up -d
docker compose logs -f comfyui  # watch model load on first request
```

Generate (image must already exist in `~/Documents/cosmos-media`):

```bash
curl -s http://localhost:8090/health

curl -s -X POST http://localhost:8090/generate \
  -H 'content-type: application/json' \
  -d '{
        "image": "portrait.png",
        "prompt": "a slow cinematic push-in, gentle wind in the hair",
        "width": 704, "height": 1280,
        "num_frames": 121, "frame_rate": 30, "seed": 42
      }'
# -> {"output": "output_xxor.mp4", "prompt_id": "..."}  (written to ~/ltx2/output)
```

Constraints: `width`/`height` divisible by 32 (use 704 or 736, not 720);
`num_frames` must be `8k+1` (121, 193, …).

## Layout

```
app/api_server.py              FastAPI → ComfyUI /prompt client (I2V only)
comfyui/extra_model_paths.yaml maps ~/ltx2/models into ComfyUI model dirs
workflows/ltx23_i2v_api.json   the I2V workflow submitted to ComfyUI (API format)
workflows/reference/           official Lightricks 2.3 example workflows
Dockerfile.comfyui             ComfyUI + LTX-2.3 nodes + RES4LYF
Dockerfile.api                 slim FastAPI client
docker-compose.yml             both services
scripts/download_models.sh     stage weights + Gemma encoder
```
