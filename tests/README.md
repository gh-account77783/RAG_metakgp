# Test layout

All test code and fixtures live outside `src/` and are excluded from the wheel
and source distribution. Install the `test` extra under the core, platform and
engine constraints, then run the offline suite from the repository. The test
extra includes real local PageIndex/PDF raster dependencies; model weights and
PyTorch inference are separate explicit native checks.

```console
python -B -m unittest discover -s tests -t . -v
```

- `contracts/`: public boundaries, identity/browser/MCP, packaging, compatibility
  and hardening regressions.
- `engines/graphmind/`: current retrieval, publication, models and evaluation.
- `engines/test_tree_navigation.py`: vector/graph-free PDF ingestion/navigation,
  shared source publication, restart and deletion.
- `unit/`: configuration, durable jobs, parsing and source lifecycle.
- `legacy/`: preserved crawler/text prototype regression tests.
- `support/`: shared synthetic fixtures and stable checkout paths.
- `fixtures/`: immutable evaluation and small ingestion fixtures.
- `integration/`: explicit real-store checks, not part of default offline
  discovery. Run through `tools/run_real_acceptance.py --stores` with a separately
  approved disposable service configuration; never target original data.
- `native/`: native installation/service/reboot acceptance evidence requirements.
  These are not inferred from hosted Windows CI or successful unit tests.

The offline suite includes temporary local HTTP/protocol fixtures, mocked
identity/model calls, a Node browser-script regression and disposable local
Chroma checks. It makes no real model requests. The frozen full-corpus semantic
evaluation remains a separate, explicitly budgeted acceptance task.
