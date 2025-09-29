# JouleTrace Energy Measurement Service

JouleTrace is a production‑ready service for measuring energy usage of code under test using Linux `perf` + RAPL. It exposes a FastAPI HTTP API, executes correctness validation, and offloads energy measurements to Celery workers pinned to dedicated CPU cores for fair, isolated measurements.

## What You Get
- Correctness gate before measurement (ensures apples‑to‑apples energy).
- CPU isolation per worker (one worker per core recommended).
- Hardware energy via RAPL through `perf stat` (package/DRAM).
- Asynchronous job queue (Redis + Celery) with scalable throughput.
- Health checks, basic metrics, and Flower dashboard.

---

## Architecture Overview
- API (`greenCode/jouletrace/api`): FastAPI app handling requests, health, and status.
- Workers (`worker-*` in docker‑compose): Celery workers running validation and energy measurement. One per physical core, `concurrency=1`.
- Redis: Broker + result backend.
- Energy meter (`greenCode/jouletrace/energy`): `PerfEnergyMeter` backend (RAPL events via `perf`).

Key paths:
- API service entry: `greenCode/jouletrace/api/service.py`
- Routes: `greenCode/jouletrace/api/routes.py`
- Celery tasks: `greenCode/jouletrace/api/tasks.py`
- Core pipeline: `greenCode/jouletrace/core/pipeline.py`
- Safe executor: `greenCode/jouletrace/core/executor.py`
- Perf meter: `greenCode/jouletrace/energy/perf_meter.py`
- Compose: `greenCode/docker/docker-compose.yml`
- Image build: `greenCode/docker/Dockerfile`

---

## Quick Start (Docker Compose)
Prereqs: Linux x86_64 host with RAPL support, Docker, Docker Compose v2 (`docker compose`).

1) Build + start
- `docker compose -f greenCode/docker/docker-compose.yml up -d --build`

2) Verify health
- API ping: `curl http://127.0.0.1:8000/ping`
- Service health: `curl http://127.0.0.1:8000/api/v1/health`
- Flower: http://127.0.0.1:5555
- Compose ps: `docker compose -f greenCode/docker/docker-compose.yml ps`

3) Submit a measurement
```
curl -s http://127.0.0.1:8000/api/v1/measure \
  -H 'Content-Type: application/json' \
  -d '{
        "candidate_code": "def solve(x):\n    return x * x\n",
        "function_name": "solve",
        "test_cases": [
          {"test_id": "case-1", "inputs": [2], "expected_output": 4},
          {"test_id": "case-2", "inputs": [7], "expected_output": 49}
        ],
        "timeout_seconds": 10,
        "energy_measurement_trials": 3,
        "warmup_trials": 1
      }'
```
Then poll the returned `task_id`:
```
curl -s http://127.0.0.1:8000/api/v1/tasks/<TASK_ID>
```

---

## Configuration and Environment
All services are configured in `greenCode/docker/docker-compose.yml`. Important knobs:

- Energy and validation (service‑wide defaults)
  - `ENERGY_DEFAULT_TIMEOUT` (seconds per test‑case validation)
  - `ENERGY_DEFAULT_MEMORY_LIMIT` (MB per test‑case validation)
  - `ENERGY_PERF_TIMEOUT` (seconds per perf measurement trial)
  - `ENERGY_USE_SUDO` (true|false) — run `perf` via `sudo` inside the container
  - `ENERGY_MEASUREMENT_CORE` (int) — core id for worker pinning
  - `ENERGY_ISOLATE_PROCESSES` (true|false)

- Celery
  - `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` (point to Redis service)
  - `CELERY_WORKER_CONCURRENCY=1` (keep 1 per worker for isolation)
  - `CELERY_TASK_SOFT_TIME_LIMIT`, `CELERY_TASK_TIME_LIMIT` (overall job budget)

- API
  - `API_WORKERS` (parallel HTTP workers)

- Logging
  - `LOG_LEVEL`, `LOG_TO_FILE`, `LOG_JSON_LOGGING`

- Permissions/host settings (host‑side)
  - `sudo sysctl -w kernel.perf_event_paranoid=0` (or `-1` where allowed)
  - Alternatively, run `perf` with sudo (containers set NOPASSWD for `/usr/bin/perf`).

Note: On Debian base images, install `linux-perf` (not Ubuntu’s `linux-tools-generic`). The Dockerfile already does this.

---

## Using the API
- `POST /api/v1/measure` (queue a measurement)
  - Body: `{ candidate_code, function_name, test_cases[], timeout_seconds?, memory_limit_mb?, energy_measurement_trials?, warmup_trials? }`
  - Returns: `{ task_id, request_id, status: "queued", estimated_completion_seconds, poll_url }`
- `GET /api/v1/tasks/{task_id}` (poll status)
  - Returns one of:
    - `queued`: task id + poll URL
    - `running`: progress info
    - `completed`: validation + energy metrics
    - `failed`: error_type + error_message
- `GET /api/v1/health` — overall health + counts
- `GET /ping` — basic liveness

Statuses
- `queued` → in Redis waiting for a worker
- `running` → worker started (validation or measurement)
- `completed` → success, includes `energy_metrics`
- `failed` → validation or measurement failed (see `error_type`/`error_message`)

---

## Fairness and Scaling
- Use exactly one Celery worker per physical CPU core with `concurrency=1`.
- Pin each worker to a distinct core (`cpuset`) for stable energy numbers.
- For heavy workloads, set `energy_measurement_trials=1` and `warmup_trials=0`, then scale out workers to increase throughput.

---

## Troubleshooting (Critical Issues We Fixed)
- Docker build failed on `linux-tools-generic` (Debian base)
  - Fix: use `linux-perf` package in Dockerfile.

- API healthcheck 204 with body → FastAPI assertion failure
  - Fix: set `response_class=Response` for `204` route and return an empty body.

- Dataclass error: `non-default argument 'validation' follows default argument`
  - Fix: reorder fields in `JouleTraceMeasurementResult` so required fields come first.

- Celery used `redis://localhost` inside containers → connection failure
  - Fix: read broker/backend from config/env (`redis` hostname from compose).

- Status mapping bug
  - Worker returned `{status: "failed"}` payload but routes always coerced to success schema.
  - Fix: if payload contains `status: failed`, map to `TaskFailedResponse`.

- Missing imports for schemas caused 500s while polling
  - Fix: import `TaskRunningResponse`/`TaskFailedResponse` in routes.

- Perf not available / permission errors
  - Host: set `kernel.perf_event_paranoid=0` (or `-1` if permitted).
  - Containers: run workers privileged and/or `ENERGY_USE_SUDO=true` (NOPASSWD for `/usr/bin/perf`).
  - Debian: ensure `linux-perf` installed. Optionally `setcap cap_perfmon+ep $(command -v perf)` on host.

- Perf meter setup/permission checks failed early
  - Fix: avoid using internal `_build_perf_command` during permissions probe; call `perf stat sleep 0.1` directly (with sudo when configured).

- Inline measurement script `.format` collisions (KeyError: 'e')
  - Fix: convert to triple‑quoted template and escape braces in f‑strings (`{{ }}`), so only `user_code`/`test_inputs`/`function_name` are formatted.

- Argument unpacking mismatch between validator and perf runner
  - Fix: always unpack list/tuple inputs as positional args in the perf runner (even length 1), matching validator behavior.

- Workers dying with `SIGXCPU` (CPU rlimit)
  - Fix: stop using `RLIMIT_CPU` (kills Celery process). Enforce wall‑clock timeouts via `SIGALRM` and memory via `RLIMIT_AS` with clamped soft limits.

- Compose v1 `ContainerConfig` KeyError / BuildKit metadata mismatch
  - Workarounds: use `docker compose` v2; or disable BuildKit for v1. We also removed `version:` key to silence warnings in v2.

- `jq` pipeline error in shell examples
  - Ensure the `| jq` is on the same line as the `curl` command or use parentheses.

---

## Do’s and Don’ts
**Do**
- Run one worker per physical core; pin with `cpuset`.
- Set `ENERGY_USE_SUDO=true` (workers) and run privileged so `perf` can read RAPL.
- Increase `ENERGY_DEFAULT_TIMEOUT`/`ENERGY_DEFAULT_MEMORY_LIMIT` for heavy but correct solutions.
- Use `energy_measurement_trials=1`, `warmup_trials=0` for heavy jobs; increase later for stability.
- Keep Redis healthy; purge stale tasks before benchmarks.
- Use Flower to monitor queue depth and worker health.

**Don’t**
- Don’t set CPU rlimits (`RLIMIT_CPU`) — they kill the worker via SIGXCPU; rely on wall‑clock timeouts.
- Don’t exceed physical core count with workers (avoid SMT for fair energy numbers).
- Don’t use Ubuntu’s `linux-tools-generic` package on Debian slim — use `linux-perf`.
- Don’t ignore host perf permissions; set `perf_event_paranoid` or rely on sudo perf.

---

## Performance Tuning Checklist
- Scale workers up to the number of physical cores.
- Raise API workers (`API_WORKERS`) if request throughput is the bottleneck.
- Increase `ENERGY_PERF_TIMEOUT` for longer trials.
- Adjust Celery `*_TIME_LIMIT` for end‑to‑end job budgets.
- Use median metrics and adequate trials for stability on small workloads.

---

## Security Notes
- Passwordless sudo is limited to `/usr/bin/perf` inside containers.
- Prefer dedicated hosts for accurate energy work; avoid noisy neighbors.
- Consider cgroup limits and monitoring in production; aggregate logs centrally.

---

## Examples
- See `greenCode/try.ipynb` for batch, A/B comparisons (brute vs optimal), and stress tests (1k+ jobs).
