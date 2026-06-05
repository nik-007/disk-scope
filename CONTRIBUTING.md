# Contributing to DiskScope

Thanks for your interest! DiskScope is intentionally small and focused, so
contributions are very welcome as long as they keep it that way.

## Project philosophy

- **One job, done well:** analyze directory sizes and delete safely. Features
  that don't serve that goal are likely out of scope.
- **Single file, minimal dependencies:** the whole app is `diskscope.py` and it
  depends only on **PyQt5** (plus the Python standard library). Please don't add
  third-party dependencies without discussing it first in an issue.
- **Fast and scalable:** it should stay responsive on trees of tens of millions
  of files. Keep the directory-only memory model and avoid per-file objects on
  the scan path.

## Development setup

```bash
git clone https://github.com/<you>/diskscope.git
cd diskscope
pip install PyQt5            # or: sudo apt install python3-pyqt5
./diskscope                  # run it
```

## Running headless (for quick checks / CI-like smoke tests)

PyQt needs a display, but you can run it off-screen:

```bash
QT_QPA_PLATFORM=offscreen python3 -c "import diskscope"   # import sanity
```

There is opt-in benchmark logging for performance work:

```bash
DISKSCOPE_BENCH=1 ./diskscope /some/big/tree   # -> /tmp/diskscope_bench.log
DISKSCOPE_THREADS=8 ./diskscope /some/big/tree # parallel scan (cold storage)
```

## Code style

- **PEP 8**, max line length **100** (enforced by flake8 via `.flake8`).
- Comments and docstrings in **English**.
- Run the linter before opening a PR — this is exactly what CI runs:

  ```bash
  pip install flake8
  python -m py_compile diskscope.py
  flake8 diskscope.py
  ```

## Submitting changes

1. Fork the repo and create a branch: `git checkout -b my-change`.
2. Make your change. Keep commits focused and the diff small.
3. Make sure `flake8 diskscope.py` is clean and the app still launches.
4. For any **UI change, include a before/after screenshot** in the PR.
5. Open a pull request with a clear description of *what* and *why*.

## Reporting bugs

Open an issue with:

- what you did and what happened vs. what you expected,
- your OS, Python version, and PyQt5 version
  (`python3 -c "import PyQt5.QtCore as q; print(q.QT_VERSION_STR)"`),
- the directory shape if relevant (huge fan-out, deep nesting, network mount…).

## A note on deletion

DiskScope deletes **permanently** (no Trash). Any change touching the deletion
path must keep the confirmation dialog and must never be able to remove the
analyzed root. Please add or update a manual test note in your PR when you touch
this area.
