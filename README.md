# Codex Rollout Backup

Public macOS-oriented helpers for daily snapshots of the complete Codex
`sessions/` and `archived_sessions/` directory trees. Files are archived without
filename, extension, or content filtering, including compressed `.zst` rollouts.

See [scripts/README.md](scripts/README.md) for usage.
See [docs/reflink-repair.md](docs/reflink-repair.md) for the separate legacy
JSONL mirror inventory, canary repair, retry, and recovery tools. Daily snapshots
leave that mirror and its repair state untouched.

## Test

```bash
for script in scripts/*.sh tests/test_codex_launchd_scripts.sh; do
  bash -n "$script" || exit
done
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x scripts/*.sh tests/test_codex_launchd_scripts.sh
fi
bash tests/test_codex_launchd_scripts.sh
python3 -m py_compile scripts/codex_reflink_darwin.py scripts/codex_repair_rollout_reflinks.py scripts/codex_rollout_mirror_copy.py scripts/codex_snapshot_lock.py scripts/codex_snapshot_path.py scripts/codex_snapshot_validate_trees.py tests/test_codex_repair_rollout_reflinks.py tests/test_codex_rollout_mirror_copy.py
python3 -m unittest discover -s tests -p 'test_codex_repair_rollout_reflinks.py' -v
python3 -m unittest discover -s tests -p 'test_codex_rollout_mirror_copy.py' -v
```

The Python tests target Python 3.9 and newer. `shellcheck` is an optional local
prerequisite; CI runs the syntax, shell smoke, and Python test suites.
