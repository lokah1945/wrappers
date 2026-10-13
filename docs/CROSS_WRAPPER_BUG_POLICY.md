# Cross-wrapper bug checking policy

Before analysis, load lifecycle status from `wrappers.json` and read root `AGENTS.md`. Check defect patterns across **active production wrappers only**: NVIDIA, Nous and OpenRouter. Shared registry and translation code remain eligible.

BLACKBOX and the OpenCode provider wrapper are pending and frozen. Do not inspect their implementations, environment files, logs or runtime state, and do not edit, test, probe or deploy them. Explicit CLI selection cannot bypass the freeze. Preserve existing changes until the owner explicitly reactivates the provider.

For an active-wrapper defect:

1. Record the concrete trigger and expected behavior.
2. Check corresponding paths in other active wrappers and shared code.
3. Fix affected active paths, preserving matching protocols and the exact requested model.
4. Run isolated active-only regression and runtime gates, recording actual passes and exclusions.
5. Update the current production report without rewriting historical audit snapshots.

Use `common.sse.iter_chunks_with_idle` for heartbeat scheduling. Do not cancel pending upstream reads on idle with `asyncio.wait_for`. Inject heartbeats only at complete frame boundaries and release upstream responses and key reservations in `finally`, including cancellation. Native paths forward bytes; translators own converted framing.

OpenCode clients remain supported consumers of active wrappers. They are separate from the frozen OpenCode provider directory.
