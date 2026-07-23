# fixtures/

Deliberately (almost) empty: the synthetic bhavcopy zips for BOTH format
eras are built **at test runtime** by the fixture-builder functions in
`../tests_offline.py` (`_old_csv` / `_new_csv` / `_zip_bytes`) into
pytest `tmp_path` — no binary files are committed (repo policy: no
binary fixtures for this study).

On the operator box the real downloaded zips land in `--cache-dir`
(default `./bhavcopy_cache`), never here.
