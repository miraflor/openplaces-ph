# Contributing to OpenPlaces PH

OpenPlaces PH is a public-data pipeline, so correctness and reproducibility matter more than cleverness.

## Before changing code

Please read:

- `README.md` for the user-facing contract;
- `docs/ARCHITECTURE.md` for design constraints;
- `docs/CODE_WALKTHROUGH.md` if you are new to the codebase;
- `DATA_LICENSES.md` before adding or redistributing a new data source.

## Local checks

Use the project environment and run:

```powershell
pytest -q
python -m compileall -q src tests run.py
openplaces --help
```

For changes involving acquisition, geometry, matching, or finalization, also run the Metro Manila smoke test before a nationwide run.

## Principles for changes

1. **Do not create a national in-memory Pandas/GeoPandas dependency.** Prefer DuckDB/Arrow/Parquet streaming or bounded tile work.
2. **Every long operation needs a durable restart boundary.** Temporary files should be promoted atomically.
3. **Optimizations must not change candidate recall silently.** Add a test around the invariant before optimizing spatial blocking or thresholds.
4. **Keep source provenance.** Canonicalization should not erase the upstream records needed to audit it.
5. **Do not overclaim independence or accuracy.** Evidence tiers are source coverage, not probabilities.
6. **Fail loudly on corrupted or ambiguous durable state.** A public-data pipeline should not silently publish partial results.
7. **Preserve the 8 GB default profile.** Faster machines may opt into more concurrency, but defaults should remain conservative.

## Pull requests

A useful pull request explains:

- what behavior changes;
- what invariant is being protected;
- what tests were added/updated;
- whether output schema or checkpoint compatibility changes;
- whether users must rebuild sources, matching, or finalization.
