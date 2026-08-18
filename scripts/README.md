# Codex backup launchd helpers

本目录包含 Codex rollout 备份相关的 `launchd` installer、实际执行脚本，以及几个轻量 helper。
当前方案只保留两层数据：

- 本地 `~/.dotfiles/codex-backup/mirror` mirror，通过 macOS held-FD helper 优先执行 strict clone，只在已分类的文件系统不兼容时退回 ordinary copy
- OneDrive 上按日生成的 `snapshots/codex-rollouts-YYYY-MM-DD.tar.{zst|gz}`，归档先写到本地 staging，再重试发布到 OneDrive，写完后尝试 `/unpin` evict

不再把 `sessions/` / `archived_sessions/` 逐文件发布到 OneDrive，避免额外占用一份本地云盘空间。

## Files

- `codex_backup_install.sh`: 生成并安装用户级 snapshot `launchd` job，每天 `02:00` 执行一次 mirror-based 日快照，并卸载旧的 legacy launch agents。
- `codex_backup_trigger.sh`: 手工 `kickstart -k` snapshot job，便于加载后立即验证。
- `codex_rollout_mirror_common.sh`: mirror discovery/orchestration、archive/unarchive relocate 和 stale duplicate pruning。每个需要刷新的 rollout 都交给 held-FD copy helper，shell 只消费已验证的 receipt。
- `codex_rollout_mirror_copy.py`: 单个 rollout 的 macOS held-FD copy/publish helper。它使用 no-follow/nonblocking 的 parent/leaf open，要求 source direct parent 与 destination container 都是 euid-owned、no group/other write、ACL-free 且 flags 安全，并排除 symlink、FIFO 和非 regular object。它在 mode `0700`、ACL-free 的私有 adjacent stage 中先尝试 strict `fclonefileat`，只对明确的 filesystem-compatibility failure 使用 `fcopyfile` ordinary-copy fallback，再将稳定 staged object 裁到最后一个完整 JSONL 换行并原子发布。Strict-clone source append 必须保持跨复验的 observed-size high-water 与稳定 mtime/ctime generation：只有 size 增长才允许 generation 改变，观察到增长后再截短 incomplete suffix 也会 deferred。`unchanged`/`no-complete-line` 还要在 identity-bound stage cleanup 后通过最后 source-prefix/path/destination 验证，才会返回成功 receipt。
- `codex_repair_rollout_reflinks.py`: macOS-only 的存量 reflink inventory、repair、retry 与 crash recovery 工具。操作流程见 [`docs/reflink-repair.md`](../docs/reflink-repair.md)。
- `codex_reflink_darwin.py`: repair utility 和 mirror copy helper 共用的 macOS held-FD backend，封装 strict clone、compatibility-only ordinary copy、atomic publish/swap、mirror policy 保留与 recovery primitives；它不是独立的 operator entrypoint。
- `codex_snapshot_lock.py`: 日快照的 macOS lock supervisor；安全打开并加固 state root 与固定 lock leaf，取得 nonblocking kernel lock 后通过继承的 fd 9 启动实际 snapshot，并在 locked path 再次复核。
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
- snapshot 压缩输出默认先写到 `~/.dotfiles/codex-backup/state/snapshot-tmp/`，可用 `CODEX_SNAPSHOT_STAGING_DIR` 覆盖。发布到 OneDrive 的最终 rename 默认重试 12 次、每次间隔 5 秒，可用 `CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS` 和 `CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS` 覆盖。
- snapshot 使用固定的 `~/.dotfiles/codex-backup/state/snapshot.lock`（随 `CODEX_BACKUP_STATE_ROOT` 覆盖）和 macOS Python supervisor，在整个 mirror、retry、压缩与发布期间持有 fail-fast kernel lock。supervisor 逐组件 nofollow 打开 state root，只接受 current-user-owned、单链接 regular lock leaf，并把 state root/lock 加固为 mode `0700`/`0600` 且清除扩展 ACL；它在 flock 前后重新走完整 absolute path，复核所有祖先解析后的 canonical state root 与 held directory identity，同时复核 lock leaf 与 held inode，acquire 到 exec 之间的替换还会被 locked worker 的第二次验证拦截。其可保证的边界是：所有遵守该协议、且 canonical ancestors/state root/lock namespace 保持稳定的调用会串行使用同一 held inode；最终验证之后由任意同 UID 进程主动替换 namespace 不在保证内。lock file 会持久保留；kernel lock 要等 shell 及所有继承 fd 9 的子进程都关闭该 holder 后才释放。Snapshot staging path 必须是 non-symlink directory；既存 symlink 或非 directory 会在接触其 target 前 fail closed，缺失时则先创建目录再记录 `st_dev:st_ino` baseline。脚本在 zstd 旧 tmp 清理、zstd 新 tmp 创建和 gzip 新 tmp 创建前复核同一最终 staging directory object identity。每次开始新的 zstd 压缩前会清理该 bound directory 中旧的 `codex-rollouts-*.tar.zst.tmp.*`；清理失败会在创建新 tmp 前中止，因此共享同一 state-root lock 的协作调用不会主动生成第二个 matching zstd staging file。这个规则也会轮换上次发布失败后保留的完整 recovery staging；它不匹配 OneDrive 侧的 `*.tar.zst.publish.tmp.*`、gzip 的 `*.tar.gz.tmp.*` 或其他临时文件。最后一次 identity check 后由恶意 same-euid actor 替换 staging namespace 的窄窗口仍不在保证内。
- 每次日快照会先在 snapshot-wide lock 内执行 durable recovery，再做普通 mirror sync，然后处理持久 retry queue。此前因源文件仍在写入、重新校验失败或其他不稳定状态而跳过且已入队的 rollout，会在下一次运行中按 rollout ID 重新定位；仍不稳定时继续 fail closed，不会用普通 copy 冒充 reflink repair。
- Common sync 先完整捕获 NUL-delimited source/mirror inventory 再验证 producer status，partial/I/O failure 不会被 process substitution 吞掉。内部 TSV 只接受 canonical `sessions/*` 或 `archived_sessions/*`；配置 root/rel 含 tab、newline 或 backslash，或已存在的 source/mirror root 是 symlink/非 directory，都 fail closed。Suppression 和 snapshot archive 列表使用 NUL delimiter，tar 使用 `--null -T`。所有 inventory/index 收纳在 owner-private temporary directories，EXIT cleanup 保留原始 status。
- Held-FD helper 加固的是单个 rollout 的 source read、stage、copy 和 publish 边界，不是整个 shell mirror sync namespace。周边 orchestration 仍包含 shell `mkdir -p`、stale relocation 和 duplicate prune；它们不在同一个 held-FD transaction 中，且 Darwin 没有 inode-conditional `mkdir`/`unlink`。Python native-resource ownership 从 syscall/C-function 返回值已 Python-visible 且控制进入 owner/caller 首个 guarded body point 后开始；guarded acquire/handoff 必须原子地把资源登记到已激活且可枚举它的 owner，或在登记失败时安全 drain，完成登记后资源继续由该 owner 持有。重试 pre-publication cleanup 时仍绑定最初捕获的 parent/name/object identity；只有证明 namespace durability disposition 后才报告 terminal state。在这个 Python-visible guarded-body 边界后，契约覆盖 guarded acquire、handoff、operation 或首轮 cleanup 期间的一次 asynchronous `BaseException`：保留 first-primary error 并进行有界第二轮 drain；deterministic line-trace injection 只是这次中断的测试代理。Tracing `call`/`return` event、尚未进入任何 cleanup guard 的函数入口有限嵌套边界、重复/重入 tracing 或 profiling、无限 cancellation、interpreter shutdown 不在保证内，`__del__` 只是 best-effort，不是 correctness 依据。Native FD/ACL acquisition 已成功但返回值尚未交给 Python 时的异步中断不在纯 Python identity-bound cleanup 保证内，实现不会扫描 `/dev/fd`，也不宣称 native signal masking。Snapshot-wide lock 可串行所有遵守协议的 run，但它与 helper 都假设 absolute ancestors/canonical namespace 在最后 check-to-syscall 微小窗口中稳定；该窗口内主动替换 namespace 的恶意 same-euid actor 仍在保证范围之外。
- 通过 installer 环境传入的 snapshot、mirror 和 OneDrive 覆盖项会写入生成的 `launchd` plist；生成的 job 默认包含 Homebrew-friendly `PATH`，也可用 `CODEX_SNAPSHOT_PATH` 覆盖。修改这些覆盖项后需要重新运行 installer。
- 需要清理旧 job 时，用 `CODEX_SNAPSHOT_LEGACY_LABELS` 传入空格分隔的 legacy launchd labels；公开 installer 默认不硬编码个人旧 label。
