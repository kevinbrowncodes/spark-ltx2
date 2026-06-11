# BUG_001 — Video jobs hang forever even after the video is finished

**Status:** Resolved (see Resolution → STORY_001)
**Reported:** 2026-06-11
**Severity:** High — a finished render looks like a stuck/slow job to every caller

## Summary

The `POST /generate` endpoint holds the HTTP connection open for the **entire**
render and only responds once it has detected the finished file. When the
poll-and-respond step stalls, the caller is left blocked with no result — even
though ComfyUI has already produced a perfectly good mp4 on disk. To the
operator it looks like "the model got slow," when in fact the render finished on
time and the **result handoff** is what hung.

## Steps to reproduce

1. Have the `spark-ltx2` stack running (`ltx2-api` on `:8090`, `ltx2-comfyui`
   on `:8189`).
2. Submit an I2V job via `POST /generate` (here: 1280×704, 241 frames, 30fps —
   driven by the ogtv-studios pipeline).
3. Watch `docker logs -t ltx2-comfyui` and `docker logs -t ltx2-api`.

## Expected vs actual behaviour

**Expected:** ComfyUI finishes the render (~9–10 min for this size), the api
returns `{"output": "...mp4", "prompt_id": "..."}` within a couple of seconds of
completion, and the caller copies the file out and marks the job done.

**Actual (observed 2026-06-11, all times UTC):**

| Time | Event |
|------|-------|
| 18:37:02 | api submits the prompt; ComfyUI logs `got prompt`. |
| 18:46:39 | ComfyUI logs `Prompt executed in 577.68 seconds`; writes `ltx_i2v_native_00008_.mp4` (5.1 MB) to `~/ltx2/output`. |
| 19:07 | api's own `LTX_POLL_TIMEOUT_S` (1800 s) deadline — a `504` should have been returned by now. |
| 19:22 | **Still no `POST /generate` response logged.** The request has been open ~45 min; the caller is still blocked; the job still shows "running" with an empty output dir. |

`GET /history/{prompt_id}` at the time clearly showed the render succeeded, with
the filename exposed under the `save` node's `images` key:

```json
"save": {"images": [{"filename": "ltx_i2v_native_00008_.mp4", "subfolder": "", "type": "output"}], "animated": [true]}
```

So a valid video existed on disk for ~36 minutes while the caller saw only a
hung "running" job.

## Root cause

Two compounding problems, one architectural and one reliability:

1. **Architectural (the real fault):** `/generate` is **synchronous** — it keeps
   the caller's connection open for the full render while it polls ComfyUI's
   `/history`. This couples the caller's job state to the api continuously
   holding one long-lived connection. Any stall in that window (a hung poll, a
   dropped api↔ComfyUI connection, a ComfyUI hiccup) strands the caller with no
   result and no signal, even when the render has succeeded and the file is on
   disk.
2. **Reliability:** the synchronous loop's own `LTX_POLL_TIMEOUT_S` deadline did
   **not** fire (no `504` after 1800 s) and no transient-error path returned, so
   the "safety" timeout is not actually reliable. The exact stall point inside
   the poll loop is undetermined, but the architectural flaw above makes the
   precise trigger moot — the failure mode must be removed, not just timed out.

`_collect_video` itself is **not** at fault: the deployed code already scans the
`images` key and the filename ends in `.mp4`, so detection logic would have
matched. The problem is that the loop never returned that match to the caller.

## Acceptance criteria

- [x] Submitting a render returns control to the caller **immediately** (no
      connection held for the duration of the render).
- [x] A finished render is reported to the caller within a few seconds of
      ComfyUI completing, regardless of how long the render took.
- [x] A stall or error in the api↔ComfyUI poll loop results in a **deterministic
      terminal state** (failed/timed-out) for the job, never an indefinitely
      open request.
- [x] A valid mp4 on disk is never lost to a hung request — the caller can
      always retrieve the result via a status lookup.

## Resolution

Fixed by **STORY_001 — Return a job ID right away and let callers poll for
status**. `/generate` now submits the render and returns a `job_id`
immediately; callers poll `GET /jobs/{job_id}` for `running` / `completed`
(with `output`) / `failed` (with `error`). The render-watching poll loop runs
in a background thread that enforces the deadline and marks the job terminal on
any exception, so a stall can no longer hang the caller. See STORY_001 for the
full change and verification.
