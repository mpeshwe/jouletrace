# JouleTrace Energy Measurement Service (Socket‑0, PCM‑Backed)

JouleTrace measures how much energy a piece of code burns while it runs. The service is built around a Socket‑0 isolation architecture: code under test executes on a dedicated CPU socket while the API, Celery workers, and Redis live on the other socket. Energy is measured with per‑socket RAPL counters exposed under `/sys` (PCM‑style), with baseline subtraction and statistical aggregation.

## Highlights
- Socket‑0 isolation: one socket dedicated to measurement; infrastructure on the other.
- Hardware energy via RAPL sysfs (PCM‑style). No external `pcm` binary and no `perf` dependency.
- Strict correctness gate before energy; repeatable trials with adaptive early‑stop (CV target).
- Asynchronous pipeline with Redis + Celery; progress polling and task cancelation.
- Observability: health/capabilities endpoints and structured logs.

## Architecture
- Clients POST to the FastAPI service.
- The API enqueues Celery tasks and exposes status at `/api/v1/tasks/{id}`.
- Workers serialize access to Socket‑0 via a Redis lock, pin the child process to a specific core, measure energy deltas from RAPL, subtract calibrated idle power, and aggregate trials.
- Calibration data lives at `config/socket0_calibration.json`.

## Software Component Architecture

Client → API → Redis → Workers → Socket‑0 Executor

![Software Component Architecture](docs/assets/socket0_architecture.png)

Key flow
- Client: POST `/api/v1/measure-socket0`
- FastAPI (Socket 1): validate request, enqueue Celery task
- Redis (Socket 1): broker, result backend, Socket‑0 lock
- Workers (Socket 1): correctness check, orchestrate trials, aggregate results
- Socket‑0 Executor (isolated): taskset pinning, RAPL (package + DRAM), baseline subtraction, CV early‑stop

## Repository Layout
- `jouletrace/api` – FastAPI app, routes, schemas, Celery tasks (Socket‑0 measurement task).
- `jouletrace/core` – Validation, socket executor, statistical aggregator.
- `jouletrace/energy` – Energy interfaces and PCM‑style meter that reads RAPL sysfs.
- `jouletrace/infrastructure` – Config, logging, health helpers.
- `docker/` – Dockerfile, docker‑compose for API, workers, Redis, Flower.
- Notebooks (`demonstration.ipynb`, `demo2.ipynb`) – example usage and analysis.

## Requirements
- Linux x86_64 host with Intel/AMD RAPL support (verify `/sys/class/powercap/intel-rapl`).
- Docker and Docker Compose v2.
- Ability to run privileged containers for workers (and API for health checks) to read `/sys` energy counters.
- Socket‑0 isolation configured on the host (recommended) and a recent calibration file.

## Quick Start (Docker Compose)
1) Build and start
- `docker compose -f docker/docker-compose.yml up -d --build`

2) Verify health
- API ping: `curl http://127.0.0.1:8000/ping`
- Socket‑0 status: `curl http://127.0.0.1:8000/api/v1/socket0/status | jq`
- Flower: `http://127.0.0.1:5555`

3) Submit a measurement
```
curl -sS -X POST http://127.0.0.1:8000/api/v1/measure-socket0 \
  -H 'Content-Type: application/json' \
  -d '{
        "candidate_code": "def solve(x):\n  return x * x\n",
        "function_name": "solve",
        "test_cases": [
          {"test_id": "t1", "inputs": [2], "expected_output": 4},
          {"test_id": "t2", "inputs": [7], "expected_output": 49}
        ],
        "energy_measurement_trials": 3,
        "timeout_seconds": 20
      }'
```
Then poll the returned `task_id`:
```
curl -sS http://127.0.0.1:8000/api/v1/tasks/<TASK_ID> | jq
```

## API Overview (Socket‑0 First)
- `POST /api/v1/measure-socket0` – queue a Socket‑0 measurement task (primary endpoint).
- `GET /api/v1/tasks/{task_id}` – poll task status (`queued`, `running`, `completed`, `failed`).
- `POST /api/v1/validate` – correctness‑only, synchronous check (no energy).
- `GET /api/v1/socket0/status` – readiness (calibration age, isolation, Redis lock check).
- `GET /api/v1/health` – system health summary.
- `GET /ping` – liveness.

Note: `/api/v1/measure` is kept as an alias and delegates to Socket‑0; prefer `/measure-socket0`.

## Calibration
- Ensure `config/socket0_calibration.json` exists and is fresh (≤7 days). The Socket‑0 endpoint refuses to run without a valid profile.
- Regenerate with the provided calibration script (run on the host so the file appears under `config/`).

## Permissions and Isolation
- RAPL counters are read from `/sys/class/powercap/intel-rapl/.../energy_uj`.
- Containers mount `/sys` read‑only. Workers run as root and are privileged to ensure access to sysfs energy files. The API is also privileged to read RAPL for health.
- Workers are pinned via `cpuset` and the child process is affinity‑pinned with `taskset` to a known core on Socket‑0 (default: core `4`). Make sure the worker container’s `cpuset` includes that core.

## Best Practices for Stable Numbers
- Make each trial last ≥100–200 ms; otherwise energy deltas and power may be noisy or inflated.
- Strategies: heavier inputs or repeat the function in an inner loop while returning the correct output.
- Keep the machine quiet; avoid co‑scheduling noisy workloads on the measurement socket.

## Example Response
```
{
  "request_id": "…",
  "status": "completed",
  "validation": { "is_correct": true, "passed_tests": 2, … },
  "energy_metrics": {
    "median_package_energy_joules": 7.49,
    "median_ram_energy_joules": 0.0,
    "median_total_energy_joules": 7.49,
    "median_execution_time_seconds": 0.12,
    "energy_per_test_case_joules": 3.74,
    "power_consumption_watts": 62.4,
    "energy_efficiency_score": 3.74
  },
  "measurement_environment": {
    "meter_type": "PCM_Socket",
    "measurement_core": 4,
    "thermal_controlled": false,
    "timestamp": 1.759e9
  }
}
```

## Troubleshooting
- Stuck in `queued`:
  - Check workers and queues: `docker compose logs -f worker-socket0-1`
  - Ensure Redis is healthy and worker has the `socket_measurement_task` registered.
- 503 “Socket 0 not calibrated”:
  - Create/refresh `config/socket0_calibration.json`.
- Permission errors (`read RAPL sysfs`) in API/worker logs:
  - Confirm `/sys` is mounted (`/sys:/sys:ro`) and services run privileged.
  - Ensure the host exposes RAPL: `ls -l /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj`.
- “All trials failed”:
  - Make sure the worker container’s `cpuset` includes the measurement core (default `4`).
  - Ensure `taskset` is available in the image (provided via `util-linux`).
- Unrealistic power (very large W):
  - Trials too short; target ≥100–200 ms per trial.
- Redis “read only replica”:
  - Point `CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND` to a writable Redis master (compose uses `redis://redis:6379/0`).

## Local Development (optional)
You can run without Docker using the startup script (root required for RAPL):
- `sudo ./scripts/start_jouletrace.sh`
This brings up Redis, a worker on the Socket‑0 queue, and the API. Docker is recommended for production.

## License
MIT. Contributions and suggestions are welcome—open an issue with your scenario and measurement notes.
