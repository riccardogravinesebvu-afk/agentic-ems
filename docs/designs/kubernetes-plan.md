# Run the demo on Kubernetes: implementation plan

**Goal:** the demo server runs as one replica on a kind cluster, and a CI job proves it with real requests.

**Architecture:** two environment variables in `demo.py` (bind address, allowed hosts); a three-stage image
that keeps the repository layout under `/app`; two plain manifests; one check script shared by CI and the
local run.

**Tech stack:** Docker, kind, kubectl, GitHub Actions; no new Python or npm dependency.

**Spec:** [`kubernetes.md`](kubernetes.md). Executed in one session, task by task, then one review of the whole branch.

## Global constraints

- Clean room: nothing outside this repository is read. Local tools live in the session's scratch folder.
- Branch `kubernetes`; one commit per task; author riccardogravinese; no trailers; no push.
- Every version pinned: base images and the kind node image by digest, kind and kubectl by version and sha256,
  GitHub Actions by commit. Versions come from the official release pages on the day, not from memory.
- No key and no private file in the image or the repository.
- No paid call. `uv run pytest` passes offline after every task.
- The README states only what a command showed.

## Review focus

Inputs the spec implies and a person will meet; each has its test in Task 1 or its check in Task 4.

1. `DEMO_ALLOWED_HOSTS` with a wildcard (`*`, `*.example`): the server refuses to start.
2. The variable set but empty, or only commas and spaces: the server refuses to start, instead of refusing every request.
3. Spaces around entries (`a, b`): trimmed.
4. A request whose Host carries a port (`agentic-ems:8000`): served, the check ignores the port.
5. A host not on the list, on the running cluster: 400 (in `check.sh`).

---

### Task 1: bind address and allowed hosts from the environment

**Files:** modify `src/agentic_ems/demo.py`, `tests/test_demo.py`.

**Produces:**
- `demo.allowed_hosts() -> list[str]`: reads `DEMO_ALLOWED_HOSTS` (comma-separated), default
  `["127.0.0.1", "localhost"]`; trims entries; raises `ValueError` naming the variable when an entry holds `*`
  or no entry is left. `build_app` passes its result to `TrustedHostMiddleware`.
- `main()` binds to `DEMO_HOST`, default `127.0.0.1`, and turns the `ValueError` into a message on stderr and exit 1.

- [ ] Tests first, in `tests/test_demo.py`, and see them fail:
  - `test_an_allowed_host_from_the_environment_is_served`: with
    `DEMO_ALLOWED_HOSTS="agentic-ems, agentic-ems.default.svc.cluster.local,localhost"`, `/api/health` is 200 for
    `http://agentic-ems:8000`, the full name and `http://localhost:8000`; 400 for `http://127.0.0.1:8000` and
    `http://evil.example:8000`.
  - `test_a_wildcard_or_empty_host_list_stops_the_server`: `*`, `*.example.com`, `""` and `" , "` each raise
    `ValueError` from `build_app`.
  - The existing default tests stay as they are and keep passing.
- [ ] Implement; `uv run pytest`, `uv run ruff check`, `uv run mypy src` clean.
- [ ] Commit.

### Task 2: the image

**Files:** create `Dockerfile`, `.dockerignore`.

- `.dockerignore`: `*` first, then `!` lines for `pyproject.toml`, `uv.lock`, `README.md` (if the build backend
  needs it), `src/`, `frontend/` minus `node_modules` and `dist`.
- Stages: `frontend` (Node 22, `npm ci`, `npm run build`); `build` (uv, `uv sync --locked --no-dev`,
  `python -m agentic_ems.generator`); final (slim Python of the same minor version, copies `/app` with `.venv`,
  `src`, `data/plant.duckdb`, `frontend/dist`). `USER 10001`, `ENV DEMO_HOST=0.0.0.0`, `EXPOSE 8000`,
  `CMD ["python", "-m", "agentic_ems.demo", "serve"]`.

- [ ] `docker build -t agentic-ems:local .` succeeds.
- [ ] `docker run --rm --read-only --cap-drop ALL -p 127.0.0.1:8000:8000 agentic-ems:local`: `/api/health` is
  200 with `has_key` false, `/` is 200, `/samples/1.json` is 200, `id -u` in the container is 10001. If the
  read-only run fails, find what is written and give it a `/tmp` volume; note it for Task 3.
- [ ] The image holds none of: `BRIEF.md`, `CLEANROOM-PLAN.md`, `.env`, `problem-classes.md`, `.gstack`, `tests`, `results`.
- [ ] Commit.

### Task 3: the manifests

**Files:** create `deploy/k8s/deployment.yaml`, `deploy/k8s/service.yaml`.

- Names and labels `agentic-ems`; image `agentic-ems:local`, `imagePullPolicy: IfNotPresent`; one replica;
  `strategy: Recreate`; container port 8000; Service port 8000, type ClusterIP.
- `DEMO_ALLOWED_HOSTS=agentic-ems,agentic-ems.default.svc.cluster.local,localhost,127.0.0.1`.
- `ANTHROPIC_API_KEY` from Secret `agentic-ems-api-key`, key `ANTHROPIC_API_KEY`, `optional: true`.
- Readiness and liveness: `httpGet /api/health` port 8000 with header `Host: agentic-ems`.
- Pod and container security context as in the spec; requests and limits.

- [ ] `kubectl apply --dry-run=client -f deploy/k8s/` accepts both files (run in Task 5's local cluster setup if
  kubectl is not yet there; then the commit follows that check).
- [ ] Commit.

### Task 4: the check script and the CI job

**Files:** create `deploy/k8s/check.sh`; modify `.github/workflows/test.yml`.

- `check.sh` (`set -euo pipefail`; port-forward on a local port, killed on exit): page 200; `samples/1.json`
  holds the first sample question; `/api/health` holds `"has_key": false`; `Host: evil.example` gets 400; a
  temporary pod from `agentic-ems:local` gets 200 from `http://agentic-ems:8000/api/health` and from the full name.
- Job `kubernetes` in `test.yml`: checkout, build, install pinned kind and kubectl with `sha256sum -c`, create
  the cluster with the pinned node image, `kind load docker-image`, `kubectl apply -f deploy/k8s/`,
  `kubectl rollout status deployment/agentic-ems --timeout=120s`, `deploy/k8s/check.sh`; on failure print
  `kubectl describe` and the pod's logs. Own `timeout-minutes`.

- [ ] Commit after Task 5's local run has passed with this script.

### Task 5: local run, then the README

- [ ] Install kind and kubectl into the scratch folder, sha256 verified; `KUBECONFIG` there too.
- [ ] Run the job's steps in order; keep the output; time them, and set the job's timeout from that.
- [ ] Break it once on purpose (wrong `Host` in a probe or a host removed from the list) to see the check fail, then restore.
- [ ] `kind delete cluster`.
- [ ] README: "Run it on Kubernetes" with those commands, the one sentence on what it is and is not, one line on
  the allowed-hosts list, the Secret command marked as not run. `docs/stack.md` if it lists the stack.
- [ ] Commit.

### Task 6: review and log

- [ ] One fresh Opus reviewer reads the whole branch diff; fix what holds.
- [ ] `uv run pytest`, ruff, mypy, `npm test` once more.
- [ ] `docs/review-log.md`: what was added, what was checked, the real output, what was not run (the CI job on
  GitHub until the branch is pushed; the path with a key).
- [ ] Commit.
