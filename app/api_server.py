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
import threading
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

# In-memory job registry (STORY_001). A render is submitted, a job_id is handed
# back immediately, and a background thread watches ComfyUI's /history and writes
# the terminal state here. Lost on restart — acceptable for a single-host service.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_S = 3600  # prune completed/failed records older than this on each submit


def _set_job(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        _JOBS.setdefault(job_id, {}).update(fields)


def _prune_jobs() -> None:
    """Drop terminal jobs older than _JOB_TTL_S so the map can't grow forever."""
    now = time.time()
    with _JOBS_LOCK:
        stale = [
            jid
            for jid, j in _JOBS.items()
            if j.get("status") in ("completed", "failed")
            and now - j.get("started", now) > _JOB_TTL_S
        ]
        for jid in stale:
            del _JOBS[jid]

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
            inputs["length"] = r.num_frames
            patched_latent = True
        elif ctype == "LTXVEmptyLatentAudio":
            # keep the (empty) audio latent aligned with the video
            inputs["frames_number"] = r.num_frames
            inputs["frame_rate"] = int(round(r.frame_rate))
        elif ctype == "LTXVConditioning":
            inputs["frame_rate"] = float(r.frame_rate)
        elif ctype == "CreateVideo":
            inputs["fps"] = float(r.frame_rate)
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


def _watch_render(job_id: str, prompt_id: str) -> None:
    """Background worker: poll ComfyUI's /history until the render reaches a
    terminal state, then record it on the job. Every exit path is terminal —
    a stall or error can never leave the job stuck 'running' (BUG_001)."""
    deadline = time.time() + POLL_TIMEOUT_S
    try:
        while time.time() < deadline:
            try:
                hist = _comfy("GET", f"/history/{prompt_id}").json()
            except HTTPException:
                # Transient ComfyUI/network blip — keep trying until the deadline.
                time.sleep(2)
                continue
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    _set_job(job_id, status="failed", error=f"ComfyUI render error: {status}")
                    return
                filename = _collect_video(entry.get("outputs", {}))
                if filename:
                    _set_job(job_id, status="completed", output=filename)
                    return
            time.sleep(2)
        _set_job(job_id, status="failed", error=f"Render timed out after {POLL_TIMEOUT_S}s")
    except Exception as exc:  # never let the watcher die silently
        _set_job(job_id, status="failed", error=f"render watcher crashed: {exc}")


@app.post("/generate", status_code=202)
def generate(r: Gen):
    if r.width % 32 or r.height % 32:
        raise HTTPException(422, "width and height must be divisible by 32")
    if (r.num_frames - 1) % 8:
        raise HTTPException(422, "num_frames must be 8k+1 (e.g. 121, 193)")

    _prune_jobs()
    wf = _patch_workflow(_load_workflow(), r)
    resp = _comfy("POST", "/prompt", json={"prompt": wf, "client_id": CLIENT_ID})
    prompt_id = resp.json()["prompt_id"]

    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        status="running",
        output=None,
        error=None,
        prompt_id=prompt_id,
        started=time.time(),
    )
    threading.Thread(target=_watch_render, args=(job_id, prompt_id), daemon=True).start()
    return {"job_id": job_id, "prompt_id": prompt_id, "status": "running"}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"Unknown job_id {job_id}")
    return {"job_id": job_id, **job}
