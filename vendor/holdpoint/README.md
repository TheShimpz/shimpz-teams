# Holdpoint

A crash-proof agent runtime. Every run resumes from its last committed step.

Holdpoint journals every model call, tool computation, external effect, human request, and timer of an agent run in
SQLite. When the process dies, the network drops, or a provider is down for an hour, the run is not lost: on the
next start it continues from the first step that had not finished. Completed work is never redone, and an
interrupted effect that is not safe to repeat is reported as `uncertain` instead of being repeated.

- **Durable by construction**: an append-only, validated history per run; recovery re-executes the definition and
  replays recorded results, checking every step against the record.
- **Safe effects**: effect keys, fenced leases, and executor obligations: nothing is dispatched until an earlier
  executor is proven ended, and a non-idempotent effect that may have acted is never repeated.
- **Explicit outcomes**: `done`, `needs-person`, `cancelled`, `limited`, `expired`, or `unrecoverable`, with the
  run's durable progress when it ends early.
- **Bounded and private**: every value, history, blob, and spend is bounded; credentials never touch the ledger.
- **Small and fast**: one Rust crate, a few dependencies, no async runtime, no server. The agent loop adds about
  0.6 ms of CPU per turn; the Python package is one 4 MB wheel that imports in under a millisecond.

Status: 0.1.0, unreleased.

## Rust

```rust
use std::sync::Arc;
use holdpoint::{Agent, Store, Worker, RunOptions, Limits};

let store = Arc::new(Store::open("ledger.sqlite3".as_ref(), Limits::default())?);
let worker = Worker::builder(store, "my-service", my_host)
    .definition(Agent::new(1))
    .model("main", Default::default(), my_model)
    .activity("browse", holdpoint::ActivityOptions::external(), my_browser)
    .build();
worker.submit("agent", "run-1", &agent_input, &RunOptions::default())?;
let driven = worker.drive("run-1")?; // Ended(outcome) or Suspended(wait)
```

## Python

```python
import holdpoint

worker = holdpoint.Worker(holdpoint.Store("ledger.sqlite3"), "my-service", {"main": call_model}, host=Host())
worker.submit_agent("run-1", agent_input)
print(worker.drive("run-1"))
```

See [the host guide](docs/guides/host.md) for activities, host hooks, and recovery.

## Against LangGraph

Measured on the same machine and scenarios as the Shimpz Brain's current runtime ([details](bench/README.md)):

| | Brain AgentRuntime (LangGraph) | LangGraph alone | Holdpoint |
| --- | --- | --- | --- |
| Plain turn, CPU (tmpfs) | 13.2 ms | 4.8–5.2 ms | 0.63 ms |
| Interrupt + resume, CPU (tmpfs) | 41.9 ms | 13.8–15.2 ms | 1.47 ms |
| Plain turn, durable disk | 38.2 ms | 18.7–21.2 ms | 9.0 ms |
| Effects repeated after a crash (fixture) | n/a | 1–3 | 0 |
| Import | 538–598 ms | 556–582 ms | 0.8 ms |
| Exclusive installed footprint | 30 packages, 46 MB | | 1 package, 4 MB |

## Quality

`scripts/check` is the one gate (CI runs it): fmt, clippy pedantic with 50-line functions, docs, every test,
100% line and production-branch coverage, cargo-deny and cargo-machete, fuzzing, the Python suite, mutation testing,
and the benchmark guardrails. The crash table runs every crash point fifty times in aborted child processes.

See [the architecture](docs/architecture.md), [the decisions](docs/adr/), and [security](SECURITY.md).

## License

Apache-2.0.
