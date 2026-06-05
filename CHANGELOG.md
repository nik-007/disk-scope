# Changelog

All notable changes to DiskScope are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-05

First public release.

### Added
- Single-window tree view of directory sizes, with human-readable sizes and
  recursive file counts, sorted largest-first.
- Share-of-parent bars (blue → amber → coral) with readable inline percentages
  and a warm tint on the heaviest sizes.
- Partition usage bar showing used / total / free of the current disk.
- In-memory caching — re-opening a folder or going *Up* is instant; `F5`
  rescans from disk.
- Safe, confirm-first **permanent** deletion; parent totals and the disk bar
  update instantly, with no re-scan. The analyzed root can never be deleted.
- About / help dialog (`F1`) and a desktop-menu installer (`install.sh`).
- Optional parallel scanning via `DISKSCOPE_THREADS` (helps on cold/slow/
  network storage).
- Opt-in benchmark logging via `DISKSCOPE_BENCH`.

### Performance
- Directory-only in-memory model: files are listed on demand instead of being
  stored as objects, so memory scales with the number of directories, not
  files — comfortably handles tens of millions of files.
- Cyclic GC is disabled during a scan (~2.5× faster at 10M entries and no
  progressive slowdown).

[1.0.0]: https://github.com/nik-007/disk-scope/releases/tag/v1.0.0
