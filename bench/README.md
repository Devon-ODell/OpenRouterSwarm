# Benchmarks

Fixed task packets run on real repositories, scored the same way each time, so the swarm's
effectiveness can be compared across harness versions, model pools and configs.

    .venv/bin/python bench/enqueue.py bench/skyring/wave1.json   # queue a wave (idempotent)
    .venv/bin/python bench/scorecard.py skyring                  # score it; appends a history row

- `<benchmark>/waveN.json` — the packets: one observable outcome each, acceptance checked by the
  target repository's own gate, path scopes from the target's backlog.
- `<benchmark>/waveNb.json` — an amending wave (`"amends": "N"`): it repairs packets of wave N that
  were already queued (detail, acceptance, allowed_paths) and keeps their task ids. Wave packets are
  otherwise never edited; the ids file records which tasks were amended and when.
- `history/<benchmark>-ids.json` — which swarm task ids belong to the benchmark.
- `history/<benchmark>.jsonl` — one scorecard row per scoring: landed share, attempts and requests
  per landed task, minutes to land, failure classes, landings per model, harness commit.

`skyring` is SkyRing (Elden Ring combat for Skyrim): Rust core + C ABI + SKSE bridge, gated by
`python3 tools/verify.py --profile local` plus the tools/ unit tests. Wave 1 is six
dump-independent packets from SkyRing's swarm/tasks.json; tasks that need the owner's game dump,
Skyrim or CrossOver are out of scope for the benchmark.
