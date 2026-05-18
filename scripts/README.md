# Codex backup launchd helpers

本目录包含 Codex rollout 备份相关的 `launchd` installer、实际执行脚本，以及几个轻量 helper。
当前方案只保留两层数据：

- 本地 `~/.dotfiles/codex-backup/mirror` mirror，优先使用 `cp --reflink=always`
- OneDrive 上按日生成的 `snapshots/codex-rollouts-YYYY-MM-DD.tar.{zst|gz}`，写完后尝试 `/unpin` evict

不再把 `sessions/` / `archived_sessions/` 逐文件发布到 OneDrive，避免额外占用一份本地云盘空间。

## Files

- `codex_backup_install.sh`: 生成并安装用户级 snapshot `launchd` job，每天 `02:00` 执行一次 mirror-based 日快照，并卸载旧的 legacy launch agents。
- `codex_backup_trigger.sh`: 手工 `kickstart -k` snapshot job，便于加载后立即验证。
- `codex_rollout_mirror_common.sh`: mirror 同步、公用 reflink copy、partial-line 裁剪、archive/unarchive relocate 处理。
- `onedrive_unpin_common.sh`: OneDrive `/getpin` readiness 轮询与 `/unpin` evict helper。

## Usage

安装或重载 `launchd` job：

```bash
./scripts/codex_backup_install.sh
```

手工立即触发一次日快照：

```bash
./scripts/codex_backup_trigger.sh
```

查看 job 状态：

```bash
launchctl print gui/$(id -u)/io.github.joey-tools.codex.snapshot.daily
```

查看日志：

```bash
tail -n 50 ~/Library/Logs/codex_snapshot_daily.log
tail -n 50 ~/Library/Logs/codex_snapshot_daily.out
tail -n 50 ~/Library/Logs/codex_snapshot_daily.err
```

## Assumptions

- helper 脚本会根据自身目录动态生成 `launchd` plist；如果仓库搬到别的路径，重新运行 `./scripts/codex_backup_install.sh` 即可刷新 `ProgramArguments`。
- snapshot 默认写到 `~/OneDrive/Backup/dotfiles/codex/snapshots`，可用 `CODEX_SNAPSHOT_DIR` 覆盖；OneDrive 根目录探测默认使用 `~/OneDrive`，可用 `ONEDRIVE_ROOT` 覆盖。mirror 默认在 `~/.dotfiles/codex-backup/mirror/`。
- 通过 installer 环境传入的 snapshot、mirror 和 OneDrive 覆盖项会写入生成的 `launchd` plist；生成的 job 默认包含 Homebrew-friendly `PATH`，也可用 `CODEX_SNAPSHOT_PATH` 覆盖。修改这些覆盖项后需要重新运行 installer。
- 需要清理旧 job 时，用 `CODEX_SNAPSHOT_LEGACY_LABELS` 传入空格分隔的 legacy launchd labels；公开 installer 默认不硬编码个人旧 label。
