"""FastAPI front for LTX-2.3 Image-to-Video.

This is a thin orchestration layer: it takes an image + prompt over HTTP,
injects them into the official LTX-2.3 ComfyUI I2V workflow (API format), and
submits it to the containerized ComfyUI's /prompt endpoint, then waits for the
render and returns the resulting mp4.

Scope (intentionally I2V-only for now):
  image + prompt  ->  LTX-2.3 I2V  ->  vertical/landscape video out
No text-to-video, multi-keyframe, audio-conditioned, video-to-video, or latent
upscale paths are exposed here.
"""

import json
import os
import time
import uuid

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
WORKFLOW_PATH = os.environ.get("LTX_WORKFLOW", "/app/workflows/ltx23_i2v_api.json")
# Where ComfyUI writes finished renders (mounted from ~/ltx2/output).
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
POLL_TIMEOUT_S = int(os.environ.get("LTX_POLL_TIMEOUT_S", "1800"))

CLIENT_ID = uuid.uuid4().hex
app = FastAPI(title="spark-ltx2 I2V")

# Class types we recognize when patching the workflow graph.
_TEXT_ENCODE_TYPES = {"CLIPTextEncode", "GemmaAPITextEncode", "LTXVGemmaEnhancePrompt"}
_LATENT_TYPES = {"EmptyLTXVLatentVideo"}
_SEED_KEYS = ("noise_seed", "seed")
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv")


class Gen(BaseModel):
    prompt: str
    image: str  # filename present in the shared input dir (required: this is I2V)
    negative_prompt: str = "worst quality, blurry, distorted, jittery, low resolution"
    # width/height must be divisible by 32. Defaults are vertical 9:16.
    width: int = 704
    height: int = 1280
    # num_frames must be 8k+1 (e.g. 121, 193).
    num_frames: int = 121
    frame_rate: int = 30
    seed: int = 42


def _load_workflow() -> dict:
    try:
        with open(WORKFLOW_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(
            500,
            f"Workflow not found at {WORKFLOW_PATH}. Author it against the live "
            f"ComfyUI /object_info and mount it into the api container.",
        )


def _patch_workflow(wf: dict, r: Gen) -> dict:
    """Inject request params into the API-format graph, matching by class_type.

    Positive vs negative prompt nodes are distinguished by their _meta.title
    (a node whose title contains 'neg' gets the negative prompt).
    """
    patched_image = patched_pos = patched_neg = patched_latent = False
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type")
        inputs = node.setdefault("inputs", {})
        title = (node.get("_meta", {}) or {}).get("title", "").lower()

        if ctype == "LoadImage":
            inputs["image"] = r.image
            patched_image = True
        elif ctype in _LATENT_TYPES:
            inputs["width"] = r.width
            inputs["height"] = r.height
            # node calls this 'length' (number of frames)
            if "length" in inputs:
                inputs["length"] = r.num_frames
            else:
                inputs["num_frames"] = r.num_frames
            patched_latent = True
        elif ctype in _TEXT_ENCODE_TYPES and "text" in inputs:
            if "neg" in title:
                inputs["text"] = r.negative_prompt
                patched_neg = True
            else:
                inputs["text"] = r.prompt
                patched_pos = True
        for k in _SEED_KEYS:
            if k in inputs and isinstance(inputs[k], int):
                inputs[k] = r.seed

    missing = [
        n
        for n, ok in {
            "LoadImage": patched_image,
            "positive prompt": patched_pos,
            "latent size": patched_latent,
        }.items()
        if not ok
    ]
    if missing:
        raise HTTPException(
            500, f"Workflow missing expected node(s): {', '.join(missing)}"
        )
    return wf


def _comfy(method: str, path: str, **kw):
    try:
        resp = requests.request(method, f"{COMFYUI_URL}{path}", timeout=30, **kw)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        raise HTTPException(502, f"ComfyUI {method} {path} failed: {e}")


def _collect_video(history_outputs: dict) -> str | None:
    """Scan a /history outputs blob for a saved video filename."""
    for node_out in history_outputs.values():
        for key in ("images", "gifs", "videos"):
            for item in node_out.get(key, []) or []:
                fn = item.get("filename", "")
                if fn.lower().endswith(_VIDEO_EXTS):
                    return fn
    return None


@app.get("/health")
def health():
    try:
        stats = requests.get(f"{COMFYUI_URL}/system_stats", timeout=5).json()
        return {"status": "ok", "comfyui": stats.get("system", {}).get("comfyui_version")}
    except requests.RequestException as e:
        raise HTTPException(503, f"ComfyUI unreachable at {COMFYUI_URL}: {e}")


@app.post("/generate")
def generate(r: Gen):
    if r.width % 32 or r.height % 32:
        raise HTTPException(422, "width and height must be divisible by 32")
    if (r.num_frames - 1) % 8:
        raise HTTPException(422, "num_frames must be 8k+1 (e.g. 121, 193)")

    wf = _patch_workflow(_load_workflow(), r)
    resp = _comfy("POST", "/prompt", json={"prompt": wf, "client_id": CLIENT_ID})
    prompt_id = resp.json()["prompt_id"]

    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        hist = _comfy("GET", f"/history/{prompt_id}").json()
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise HTTPException(500, f"ComfyUI render error: {status}")
            filename = _collect_video(entry.get("outputs", {}))
            if filename:
                return {"output": filename, "prompt_id": prompt_id}
        time.sleep(2)
    raise HTTPException(504, f"Render timed out after {POLL_TIMEOUT_S}s (prompt {prompt_id})")
