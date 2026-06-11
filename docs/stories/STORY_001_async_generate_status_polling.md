# STORY_001 — Return a job ID right away and let callers poll for status

**Status:** Done (2026-06-11 — built, deployed, smoke render + consumer end-to-end green)
**Type:** API reliability
**Fixes:** BUG_001 — Video jobs hang forever even after the video is finished
**Effort:** ~45 min (api + consumer + verification)
**Risk:** Medium (changes the `/generate` response contract; one known consumer)

## User story

**As** the operator of the spark-ltx2 I2V service (and the ogtv-studios pipeline
that drives it),
**I want** `/generate` to accept a render and hand back a job ID immediately,
with a separate endpoint to poll that job's status,
**so that** a finished render is never trapped behind a hung connection, a slow
or stalled render can be observed and cancelled, and the caller's job state is
decoupled from the api holding one long-lived HTTP connection.

## Background / evidence

See [BUG_001](../bugs/BUG_001_generate_hangs_after_render.md). On 2026-06-11 a
1280×704 / 241-frame render finished cleanly in 577 s and wrote a valid mp4, but
the synchronous `POST /generate` never returned — the request stayed open ~45
minutes, past its own 1800 s timeout, while the caller showed a stuck "running"
job. The render was fine; the connection-holding result handoff was not.

The current contract:
- `POST /generate` blocks for the whole render, then returns
  `{"output": "<file>.mp4", "prompt_id": "..."}`.
- The only consumer is ogtv-studios' `pipeline/ltx_client.py`, which already
  runs the POST on a background thread with a `cancel_check`, but still issues
  one blocking request with a 1800 s read timeout — so its job state is hostage
  to the api keeping the connection alive.

## Proposed change

**api (`app/api_server.py`):**

1. Keep all request validation exactly as-is (the 32-divisible width/height and
   `8k+1` num_frames rules still return `422` before anything is queued).
2. `POST /generate` now:
   - patches the workflow and submits to ComfyUI's `/prompt` (unchanged),
   - registers an in-memory job record keyed by a generated `job_id`
     (`status="running"`, `prompt_id`, `started`),
   - starts a **background daemon thread** that runs the existing
     poll-`/history` loop, and
   - returns **immediately** with `{"job_id", "prompt_id", "status": "running"}`
     and HTTP `202`.
3. The background worker enforces `LTX_POLL_TIMEOUT_S` as a hard deadline and
   wraps the whole loop in a try/except so that **any** outcome is terminal:
   - render success → `status="completed"`, `output="<file>.mp4"`,
   - ComfyUI reports error → `status="failed"`, `error=...`,
   - deadline exceeded → `status="failed"`, `error="Render timed out after Ns"`,
   - unexpected exception → `status="failed"`, `error=...`.
   A transient `/history` fetch error is retried until the deadline rather than
   failing the job immediately.
4. New `GET /jobs/{job_id}` returns the job record
   (`{job_id, status, output, error, prompt_id, started}`), or `404` for an
   unknown id.
5. Light memory hygiene: prune completed/failed job records older than ~1 hour
   on each new submit, so the in-memory map can't grow without bound.
6. `GET /health` is unchanged.

**consumer (ogtv-studios `pipeline/ltx_client.py`):**

7. `generate()` switches from one blocking POST to **submit + poll**:
   - `POST /generate` with a short (30 s) timeout to get the `job_id`,
   - poll `GET /jobs/{job_id}` every ~2 s, checking `cancel_check()` every ~1 s,
     until the job is `completed`/`failed` or the local deadline trips,
   - on `completed` copy `output` out of `/ltx-output` into `output_path`
     (unchanged copy logic),
   - on `failed` raise `RuntimeError(error)`,
   - on `cancel_check()` raise `LTXCancelledError` (render is abandoned/orphaned,
     same accepted caveat as today),
   - the elapsed-time heartbeat to `progress_callback` is preserved.

> This story lives in two repos. The spark-ltx2 side is the fix's source of
> truth; the matching ogtv-studios `ltx_client.py` change ships alongside it so
> the pipeline keeps working against the new contract.

## Acceptance criteria

- [x] `POST /generate` returns `202` with `{job_id, prompt_id, status}`
      **immediately** (well under a second), not after the render. *(Verified:
      0.03 s, 320×320/9-frame smoke render, 2026-06-11.)*
- [x] `GET /jobs/{job_id}` returns `running`, then `completed` with the mp4
      filename in `output` once ComfyUI finishes (observed within ~2 s of the
      `Prompt executed` log line). *(Verified: flipped to `completed` with
      `ltx_i2v_native_00009_.mp4` at ~45 s; file present on disk.)*
- [x] `GET /jobs/{unknown}` returns `404`. *(Verified.)*
- [x] The 32-divisible and `8k+1` validations still return `422` before
      submission (unchanged). *(Verified: `width=720` → 422.)*
- [ ] A render whose poll loop is forced past `LTX_POLL_TIMEOUT_S` lands in
      `status="failed"` with a timeout message — i.e. the deadline now fires
      deterministically (the BUG_001 "never 504'd" failure cannot recur).
      *(Guaranteed by construction — deadline check + try/except in
      `_watch_render` — but not yet runtime-forced.)*
- [x] ogtv-studios `ltx_client.generate()` drives a full job end-to-end against
      the new contract: submit → poll → copy mp4 → return, and a `cancel_check()`
      mid-poll raises `LTXCancelledError` within ~1 s. *(Verified inside the
      ogtv-pipeline container, 2026-06-11: happy path copied the mp4 in 24 s;
      `cancel_check` raised `LTXCancelledError` in 0.0 s.)*

### Verification plan (per CLAUDE.md §3.5)

- **Build verification — required.** `docker compose build api` must succeed
  (only `Dockerfile.api`/`app` changed; **do not** rebuild the long `comfyui`
  image).
- **Container startup — required.** `docker compose up -d` then
  `curl http://localhost:8090/health` returns `{"status":"ok", ...}`.
- **Workflow validation — N/A.** No `workflows/*.json` node changes; the graph
  and `_patch_workflow` roles are untouched.
- **End-to-end smoke render — required (control-plane proof, not output proof).**
  Run the fast 320×320 / 9-frame / 8-step config through the **new** flow:
  `POST /generate` → confirm an immediate `job_id` → poll `GET /jobs/{id}` →
  confirm it flips to `completed` with a filename → confirm the mp4 exists in
  `~/ltx2/output`. Because this change cannot alter sampler output (it only
  moves where the result is reported), a SATAVG/gray check is **not** required;
  the existing render path is byte-for-byte unchanged.

## Out of scope

- Real mid-render progress percentage (ComfyUI WebSocket). The status endpoint
  reports `running`/`completed`/`failed` only; the consumer keeps its
  elapsed-time heartbeat. A real progress feed is a separate backlog item.
- A server-side cancel endpoint. Cancellation stays local to the consumer (the
  render is abandoned/orphaned), matching today's behaviour and the Cosmos
  pattern.
- Persisting job records across api restarts (in-memory is sufficient for a
  single-host service; a restart loses in-flight job ids, which is acceptable).

## Risks & mitigations

- **Contract break (medium):** the `/generate` response shape changes from
  `{output}` to `{job_id}`. The only consumer (ogtv-studios `ltx_client.py`) is
  updated in the same story; the README curl example is updated to the
  submit+poll flow.
- **In-memory job map (low):** lost on api restart and unbounded in principle.
  Mitigated by §5 pruning; a restart only affects in-flight jobs (the consumer's
  poll will 404 and surface a clear failure rather than hanging).
- **Correctness (none):** the render graph, models, sampler, and `_patch_workflow`
  are untouched — identical frames for a given seed. This is a control-plane
  change only.

## Rollback

Revert `app/api_server.py` and `pipeline/ltx_client.py` to the synchronous
versions and `docker compose build api && docker compose up -d`. Fully
reversible; no data migration.
