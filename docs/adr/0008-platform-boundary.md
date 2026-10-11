# The framework owns the inside of a Run; scheduling belongs to the Platform

**Pernix is responsible for:**

- Correctness within a Run.
- Making a Run observable: `run_id`, structured logs, stats snapshots.
- Making a Run controllable: the semantics of Pause, Drain, and Force Stop, plus pluggable control sources and stats reporting.

**The Platform is responsible for:**

- When to run, and how many Workers.
- Deployment and scheduled triggers.
- Issuing start and stop commands.
- Cleaning up data after a Run ends.

The framework never deletes persisted shared state on shutdown. Cleanup must be an explicit action by the Platform or the user, because automatic cleanup when any one Worker exits would destroy a Frontier that other Workers are still using.
