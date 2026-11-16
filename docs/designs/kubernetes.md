# Design: run the demo server on Kubernetes

Date: 2026-11-16. Status: approved by the owner on 2026-11-16.

## Intent

Show that the demo server runs on Kubernetes, with a CI job as the proof. The smallest change that does it.
What it is: one replica, tested on a kind cluster (Kubernetes in Docker) in CI. What it is not: a production cluster.

## Goals

- A container image of `python -m agentic_ems.demo serve`.
- Plain manifests: one Deployment, one ClusterIP Service.
- A CI job that builds the image, starts a kind cluster, deploys and checks the answers with real requests.
- The same steps run once on the owner's machine, with the real output in `docs/review-log.md`.
- A README section that holds only commands that were run.

## Not in scope

Helm, Terraform, a cloud cluster, an Ingress, autoscaling, monitoring, more than one replica, anything paid.

## Would make it a failure

- A key, or a private file (`BRIEF.md`, `.env`, `CLEANROOM-PLAN.md`, `docs/problem-classes.md`, `.gstack/`), inside the image or the repository.
- The Host check switched off or widened with a wildcard.
- A README claim with no command behind it.

## Approaches

1. **Leave the Host check as it is; probes send `Host: localhost`.** No code change. But the Service answers 400 to
   every pod that calls it by name, so it works through port-forward only.
2. **Make the list of allowed hosts configurable. Picked by the owner.** The DNS-rebinding defence stays on; only
   the list changes. The Service then answers inside the cluster, and CI can prove it.
3. **Switch the check off in the container.** Dropped: it removes a defence to make a test pass.

## Decided by the owner

- An environment variable holds the allowed hosts. Default unchanged: `127.0.0.1` and `localhost`.
- The Deployment sets it to the Service's short name, its full name in the namespace, and `localhost` for
  port-forward.
- Probes send a `Host` header with the Service name, because they arrive on the pod's IP.
- No wildcard, and the check is never switched off.
- CI checks from inside the cluster (a temporary pod calls the Service by name and gets 200 on `/api/health`)
  and through port-forward.
- An offline test: an allowed host gets 200, another host gets 400.
- The cluster runs with recorded answers only, no API key. The key is an optional Secret, never committed.
- Versions pinned, GitHub Actions pinned by commit, kind and kubectl checked against their checksums.
- If kind does not fit in 15 minutes, only the new job gets a longer timeout, and the review log says so.
- Work on a branch; no push and no merge until the owner says so.

## My calls (the owner can change any of them)

**Server (`src/agentic_ems/demo.py`)**

- `DEMO_HOST`: the address to bind, default `127.0.0.1`. Only the Dockerfile sets `0.0.0.0`.
- `DEMO_ALLOWED_HOSTS`: comma-separated, default `127.0.0.1,localhost`. An entry that holds `*` stops the
  server at start with a clear message, because the middleware would read it as a wildcard.
- The port stays the constant 8000: nobody asked to change it.

**Image (`Dockerfile`, `.dockerignore`)**

- Three stages: Node builds the frontend; uv installs from `uv.lock` without dev dependencies and runs the
  generator to write `data/plant.duckdb`; a slim Python stage runs the server.
- The image keeps the repository layout under `/app`, because the server finds `frontend/dist` and
  `data/plant.duckdb` relative to its own source file.
- Base images pinned by digest. User id 10001, no shell login.
- `.dockerignore` starts from "ignore everything" and lists what the build needs, so a new private file stays
  out by default.

**Manifests (`deploy/k8s/deployment.yaml`, `service.yaml`)**

- Name `agentic-ems`, namespace `default`. The allowed hosts are `agentic-ems`,
  `agentic-ems.default.svc.cluster.local`, `localhost` and `127.0.0.1`. Another namespace means editing that line.
- Readiness and liveness probes on `/api/health`, which exists today.
- Non-root, read-only root filesystem, no privilege escalation, all capabilities dropped, default seccomp
  profile. Small CPU and memory requests and limits. A writable `/tmp` volume is added only if the running
  container shows it is needed.
- `strategy: Recreate`. The "one model call at a time" limit is a lock inside one process; a rolling update
  would run two pods for a moment.
- `ANTHROPIC_API_KEY` comes from a Secret named `agentic-ems-api-key`, marked optional. No Secret manifest is
  committed; the README gives the `kubectl create secret` command. This path is not run, since it is paid, and
  the README says it was not run. It also says that with a key any pod in the cluster can ask questions on it.

**Checks (`deploy/k8s/check.sh`, used by CI and locally)**

Through port-forward: the page returns 200; `samples/1.json` returns the first recorded question;
`/api/health` says `has_key: false`; a request with a host not on the list gets 400.
From inside the cluster: a temporary pod from the same image calls `http://agentic-ems:8000/api/health` with
Python's standard library and gets 200. Using the same image means no second image to pull or pin.

**CI (`.github/workflows/test.yml`)**

A second job, `kubernetes`, beside `test`: build the image, download pinned kind and kubectl and verify their
sha256, create the cluster with a node image pinned by digest, load the image, apply the manifests, wait for
the rollout, run `check.sh`. Any failing step fails the job.

**Local run**

kind and kubectl go into the session's scratch folder, with `KUBECONFIG` pointing there too, so nothing outside
this repository is read or changed. The cluster is deleted at the end.

## Order of work (one commit each)

1. Environment variables for bind address and allowed hosts, with the offline test.
2. `Dockerfile` and `.dockerignore`; check non-root and read-only filesystem on the running container.
3. Manifests.
4. `check.sh` and the CI job.
5. Local run on kind; README section from the commands that ran.
6. Review-log entry with the real output.

## Open point

The CI job can only be shown green on GitHub, and that needs a push of the branch. Until the owner allows it,
the proof is the local run of the same steps, and the README and CV sentence must say "CI job added", not
"CI job green".

## Left to the builder

Exact versions and digests (the newest stable ones on the day, verified, not taken from memory), resource
numbers, the wording of the README section.
