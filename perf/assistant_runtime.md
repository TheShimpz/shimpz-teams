# gVisor runsc for Assistant workloads: measured and rejected (2026-10-09)

Tool: `uv run --frozen --python 3.14 python -m perf.assistant_runtime --cold-samples 20 --warm-samples 240`
(teams `1d58ad2`). Each container uses the exact kwargs of `AssistantLifecycle._create_assistant_container` plus only
`runtime`, and passes Team's own `isolation.inspect_profile`. Every invocation runs `list-zones` of the staged
Cloudflare Assistant (`sha256:be0aa730bc69…`) through Team's `rpc_exchange`, with a broker that answers its one
provider call from a canned zones page. Cold runs interleave the two runtimes and alternate which goes first. The run
left no container or network behind.

Host: Debian 13, kernel 6.12.63, Docker 29.9.0, runc 1.5.2, runsc release-20260706.0 on the `systrap` platform, Intel
Xeon Platinum 8160, 96 CPUs. Team's cpuset is `0-47`, with 0.25 CPU, 128 MiB of memory, and 64 pids.

## Latency (ms)

| Measure | runc p50 / p95 | runsc p50 / p95 | runsc delta at p50 |
|---|---|---|---|
| Create (n=20) | 124.5 / 146.4 | 125.3 / 328.5 | +0.8 |
| Start to running (n=20) | 354.8 / 449.9 | 1457.5 / 1599.4 | +1102.7 (4.1x) |
| Create + start (n=20) | 485.0 / 580.7 | 1634.7 / 1760.5 | +1149.7 (3.4x) |
| First invocation (n=20) | 1503.2 / 1586.7 | 2977.9 / 3185.9 | +1474.7 (2.0x) |
| Warm invocation (n=240) | 1413.6 / 1683.8 | 2301.4 / 2505.7 | +887.8 (+63%) |
| Warm invocation p99 / max | 2609.4 / 2708.9 | 3278.8 / 3817.3 | |

Every Action is a fresh `docker exec` of the SDK's Python process at 0.25 CPU, so process startup dominates both
columns. gVisor adds about 0.9 s to every Action and leaves less of the fixed 8 s RPC deadline
(`RPC_TIMEOUT_SECONDS`) for provider calls and file delivery.

## Memory (host cgroup v2 of the container)

| Measure | runc | runsc |
|---|---|---|
| `memory.current` idle / after 241 invocations | 2.5 / 4.5 MiB | 19.6 / 29.7 MiB |
| `memory.peak` after warm run | 21.8 MiB | 48.1 MiB |
| Host processes in the cgroup / summed RSS after warm run | 1 / 8.8 MiB | 7 / 91.7 MiB |
| `pids.current` idle / after warm run | 1 / 1 | 38 / 42 |

## Compatibility and enforcement

- The Action works unchanged under runsc. All 20 cold invocations, all 241 warm invocations, and 2 concurrent
  invocations returned the identical expected result with empty stderr.
- Both runtimes refused the same operations: uid/gid 10001, `CapEff` 0, no-new-privs 1, nofile 1024, a raw socket
  (EPERM), egress from the internal network (ENETUNREACH), and exec from `/tmp` (EACCES). Writes to the root
  filesystem fail under both runtimes, with EROFS under runc and EACCES under runsc.
- **The seccomp profile is not the one Team admits.** The workload's seccomp mode is 2 (Docker's default filter)
  under runc and 0 under runsc. runsc ignores the OCI filter (`oci-seccomp=false`) and filters only its own Sentry.
- **The pids limit changes meaning.** Under runsc, the host `pids.max=64` also counts the Sentry and gofer threads (38
  at idle). The forking probe was killed after 12 children: `exec` exit 128 with `WaitPID … EOF`, where runc returns a
  clean EAGAIN after 62. Threads inside the sandbox are not bounded at all (100 started, where runc allows 62).
- The memory limit holds under both runtimes (OOMKilled). An exec exceeding it exits 137 under runc and 128 under runsc.
- `HostConfig` does not record whether a workload actually runs under gVisor. `inspect_profile` does not check
  `Runtime`, so opting in would need new fail-closed runtime admission and readiness checks. That is the
  machinery ADR-0108 retired with the Hosted profile. Docker Desktop has no runsc, so the option would be Linux-only.

## Decision

Rejected; no runtime switch and no ADR. The Action runs unchanged, but every Action gets 63% slower (+0.9 s at
p50), install and resume take 2.0–3.4x longer, and resident memory is about 6x higher. Most importantly, the
enforced profile is no longer the one Team admits: the workload has no seccomp filter and the pids limit means
something else. The existing boundary already removes the threats gVisor would mainly reduce for a Local Space. The
workload has no network route and no credential (ADR-0106), is not root (ADR-0109), drops every capability, and
runs read-only under no-new-privileges with Docker's default seccomp filter.

Revisit only if the Space threat model changes to require kernel isolation. A new measurement must cover the KVM
platform (`/dev/kvm` exists on this host but was not measured, because the platform is a daemon-wide runsc flag). It
must also cover a pids budget that excludes Sentry threads, and runtime-checked admission.
