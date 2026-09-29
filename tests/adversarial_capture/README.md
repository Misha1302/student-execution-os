# Natural-language capture adversarial research harness

This directory belongs to the isolated research branch for immutable baseline
`7ba92ae0fa99c1526b083cd6bc8afed82dc993fc` (`v0.6.2`). It is a finding harness,
not a production fix.

The oracle is intent-first: expected semantics are produced before any parser is
called. `run_js_batch.mjs` executes the browser parser/capture kind selection in
one Node process; `ui_payload_probe.mjs` checks event reminder UI presets against
the event payload boundary. `scripts/run_capture_adversarial.py` creates the
structured, human, ambiguous, metamorphic and adaptive corpora, runs Python and
JS, clusters failures, and writes the evidence bundle under
`artifacts/capture-adversarial/`.

The workflow has a baseline mutation allowlist and fails if the research commit
changes production source files. Semantic failures do **not** fail the workflow;
they are the result being measured. The canonical final evidence run is:

```bash
PYTHONPATH=src TZ=Europe/Moscow python scripts/run_capture_research_pipeline.py
```

It executes corpus generation, named coverage audit, JS/Python differential,
property probes, 12 rendered UI/API/SQLite scenarios, targeted mutations, a
parser-blind deterministic realism review, true failure shrinking, and report
generation. `pipeline-run.json` records exact commands, results, and durations.
