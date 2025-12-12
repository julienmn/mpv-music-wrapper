# Development and testing

These steps are only needed for contributors. Normal playback users can ignore everything here.

## Test runner
- Use the helper script from the repo root: `python tests/tools/run_tests.py`
- The script creates `.venv` (in the repo root), installs dev deps from `tests/requirements-dev.txt`, and runs unit tests.
- Optional: add `--library /path/to/your/music` to also run the integration test that exercises the album-spread planner against a real library.

## Direct pytest use
- You can also run `python -m pytest tests/unit` (and `tests/integration` with `MPV_MUSIC_LIBRARY` set) using your own environment.

## Cover candidate dump tool (for TDD on art selection)
- From repo root: `PYTHONPATH=. python tests/tools/dump_cover_candidates.py "/path/to/track.flac"`
- It scans the track folder (and parent album scope when relevant), extracts embedded art to a temp file, and prints the exact candidate metadata the wrapper uses (scope, bucket, keywords, area, size, name tokens).
- Use this to capture real-world data before writing a unit test: run the tool, copy the emitted candidates into a test, and assert the expected pick. This keeps tests faithful to actual albums without touching the library.

## Notes
- Runtime usage of `mpv_music_wrapper.py` does not require the dev venv or pytest.
- The library path used for integration tests is read-only; the wrapper never writes to your music library.
