# Rollout reflink repair operator guide

`scripts/codex_repair_rollout_reflinks.py` 是 macOS-only 的存量 rollout
reflink inventory、repair、retry 和 crash recovery 工具。它只处理
`~/.codex/{sessions,archived_sessions}` 与 mirror 中按 rollout ID 配对、且在操作前后都能证明字节完全一致的 regular files。

工具要求 source、mirror 和同目录 staging 位于同一个支持 clonefile 的文件系统。实际修复只接受 `fclonefileat` clone；clone 失败时 fail closed，不会退回普通 copy。

## Defaults and output

默认路径由现有 backup 环境变量推导：

- source root: `$CODEX_ROOT`，默认 `~/.codex`
- mirror root: `$CODEX_MIRROR_ROOT`，默认 `~/.dotfiles/codex-backup/mirror`
- state root: `$CODEX_BACKUP_STATE_ROOT`，默认 `~/.dotfiles/codex-backup/state`
- repair state: `$CODEX_BACKUP_STATE_ROOT/reflink-repair`
- retry queue: `$CODEX_BACKUP_STATE_ROOT/reflink-repair/retry-queue.json`

带 `--json` 时，stdout 只输出一个 machine-readable receipt；诊断信息写到 stderr。文档不依赖 receipt 内部字段，operator 应保留整份 receipt 和对应命令。

无 queue、空 queue、已完成或本次 deferred 都返回 `0`。CLI/state/manifest 损坏、recovery 歧义，或安全检查与底层 syscall 的 fatal failure 返回 `2`。

所有子命令都会取得 `repair.lock`。即使是 inventory 或 dry-run，工具也可能创建并加固 owner-private repair state directory 和 lock file：目录收敛为 mode `0700`，文件收敛为 mode `0600`，并清除 extended ACL 与不安全 flags。因此这里的“read-only”只指 source/mirror data plane，不代表 control-plane state 完全不落盘。Dry-run 不会创建事务 intent、写 retry queue、创建 clone 或执行 namespace swap。

读取 queue、intent 和 manifest 等 protected state JSON 时，工具绑定文件 identity/access policy，并对内容做 bounded double-read。字节保持相同时，短暂的 mtime/ctime generation churn 会在有限次数内重试；size 是内容长度的一部分，size 或字节改变都会 fail closed。identity/access-policy 改变、不可读，或持续 generation instability 也会分别 fail closed；工具不会把一次单独的 mtime/ctime `stat` delta 当作内容 mutation。

一次 batch command 不是全局原子操作：每个已进入 durable transaction 的文件由它自己的 intent/manifest 保护，但后续 candidate 出现 fatal failure 不会回滚先前已完成的 candidate。如果 fatal 发生前已有处理结果，stdout receipt 会保留这些 result、追加 fatal result，并在 summary 中给出 `completed_before_fatal`；operator 必须依 receipt 和 durable state 判定已发生的 mutation，不能把 exit `2` 理解为整批“什么都没做”。

## Data-plane read-only inventory and dry-run

Inventory 是只读操作；`repair` 在没有 `--apply` 时也是 dry-run：

```bash
python3 scripts/codex_repair_rollout_reflinks.py inventory --json
python3 scripts/codex_repair_rollout_reflinks.py inventory \
  --rollout-id 019e4f9b-d3cb-7c92-9637-722ebb48c3db \
  --json
python3 scripts/codex_repair_rollout_reflinks.py repair --json
```

Rollout 可能在 `sessions` 与 `archived_sessions` 之间移动，因此 selection 和 retry 都按 rollout ID 重新定位，不依赖旧的相对路径。

`--rollout-id` 只缩小 inventory/repair 的 candidate selection，不会屏蔽全局 discovery error；只处理 queue 的 retry 也遵守相同原则。这些 receipt 仍会包含任何 scan error 并标记为 `incomplete`，apply 在 discovery 不完整时拒绝继续。Operator 不应把一个 candidate 的局部成功解释为完整扫描已经通过。

## Protected properties and skip classes

修复保护的性质是 content stability（内容稳定）和 access policy（访问策略），不是旧 mirror object identity。每个候选都需要 no-follow regular-file 检查、同卷与单链接约束、完整逐字节比较，以及 swap 前后的重新验证。Source 与 mirror 的 policy 会分别 snapshot 并要求在事务期间保持稳定；它们不必彼此相同。工具把旧 mirror 的 owner/group、mode、ACL、BSD flags、mtime 和全部 caller-visible extended attributes 应用到 clone 并重新验证，从而保留旧 mirror policy；当前 exclusive-writer 前提同时要求旧 mirror 的 extended ACL 为空。Parent directory 也必须没有被 symlink 或其他对象替换。

Extended-attribute 保证只覆盖当前 process 使用 options `0` 能列举和读取的 caller-visible attributes；hidden compression attributes 不在该保证内。检测到 internal BSD flags 时工具会 fail closed，不会声称已安全保留这类隐藏状态。

任何可能成为 final survivor 的 mirror file 都必须满足 conservative exclusive-writer policy：由当前 euid 拥有，mode 不得含 group/other write bits (`0022`)，不得带 extended ACL，并且只能带已知安全、可由用户设置的 BSD flags。初始 inspection 就不满足时，inventory/direct repair 把它分类为 `unsupported` 并 skip；`retry --apply` 把它视为 terminal，并从 queue 删除该 rollout ID，而不会一直重试一个不满足安全前提的文件。事务绑定后才出现的 policy drift 则是重新验证失败；工具会按当前 durable phase 安全回滚或保留证据，不会强行提交。

Mirror file 所在的 stage container（也就是创建私有 stage 的父目录）必须由当前 euid 拥有、不得对 group/other 可写、不得带 extended ACL，并且只能含已知安全的 BSD flags。工具不会放宽这个周边目录的 access policy；前提不满足时会 fail closed。新建的 private stage 自身则必须是 euid-owned、mode `0700`、ACL-free 且 flags 安全。

成功替换会有意改变 mirror inode，并可能改变 ctime 或 birthtime；这些 metadata transition 本身不等于内容变更。相反，object replacement、字节变化或 access-policy 变化会阻止提交。`st_blocks` 也不作为共享 extent 的证明；clone provenance 来自成功的 clone syscall、持有的文件描述符和最终路径 identity chain。

Operator 应把下列结果保持为不同类别，不要把它们统称为“不相等”：

- source 或 mirror missing
- source/mirror bytes mismatch
- unreadable 或 revalidation failure
- 正在写入或对象被替换而 unstable
- source 或 mirror policy 在验证期间发生变化
- unsupported filesystem、cross-device 或其他安全前提不满足

在 `repair` 入口中，只有 `repair --apply --queue-unstable` 才会根据当次 discovery 新增持久 retry obligation；所有 dry-run 都不写 queue。入队范围是 unstable/revalidation、`active-complete-prefix`，以及 `missing`、`source-missing`、`mirror-missing`（用于 archive move 或暂时消失后重试）。内容 mismatch、无法安全保留 mirror policy 或 unsupported pair 不会自动变成可覆盖任务。另外，`retry --apply` 会根据 terminal/deferred/repaired 结果重写 queue，`recover --apply`（包括其他 apply 命令入口先执行的 startup recovery）也可以履行既有 durable transaction 中记录的 queue obligation。因此“只有 repair 写 queue”不是整个工具的保证。

## Daily mirror copy boundary

普通 mirror sync 和存量 repair 是两条不同的 copy policy。Shell 通过 `codex_rollout_mirror_copy.py sync-one` 把每个需要刷新的 rollout 交给 macOS held-FD helper。Helper 逐组件 no-follow 打开 parent，并以 `O_NOFOLLOW|O_NONBLOCK` 打开 leaf；symlink、FIFO 和 directory 会在绑定时被拒绝，绑定后的 regular-file 或 parent replacement 也会 fail closed，不会跟随或阻塞在 FIFO 上。Source held FD 是 copy 期间的 authoritative object，pathname 只在边界处用来重新证明这个对象仍是所选 source。Source、source direct parent、destination container 和已存在的 destination 都必须满足 euid ownership、no group/other write、no extended ACL 和 safe-flags policy（directory 不要求 mode 精确为 `0700`，但不能让 group/other 可写）。

Helper 在 private adjacent stage 中先尝试 strict `fclonefileat`；只有 `ENOTSUP`/`EOPNOTSUPP`/`EXDEV` 这类已分类的 filesystem compatibility failure，并且失败后仍能证明 clone child 不存在时，才允许使用 `fcopyfile` ordinary-copy fallback。其他 clone error 或遗留 partial child 都不会降级。完整 staged object 经过重新验证后，裁成以最后一个 LF 结尾的稳定 JSONL prefix，然后才原子发布。Strict-clone 后的 source append 可以被接受，但必须仍是同一 held object、保留 access policy，且 staged bytes 仍是 source prefix。Helper 会跨验证记录 source size high-water 与稳定的 mtime/ctime generation：后续 size 只能保持或增长，只有 size 增长时才允许 generation 改变；同长 rewrite 或 generation churn 会 deferred。一旦观察到更大长度，之后即使只截短未完成的 suffix、完整 candidate prefix 仍保留，也会 deferred。如此当次可发布已稳定的完整行，后续增长留到下次 sync。`fcopyfile` fallback 则要求 copy 前后的完整 source snapshot 一致，变化时 deferred。`no-complete-line` 和 `unchanged` 也不会在 stage cleanup 之前就返回成功；helper 保留 unlinked candidate FD，清理 child/stage 后重新 snapshot candidate，再做最后 source-prefix、source path 和 destination binding 验证。只增长 append 仍可接受，truncate、suffix shrink 或 prefix rewrite 都会 deferred。这个 ordinary-copy fallback 只属于普通 mirror sync；存量 repair 始终要求 strict clone，绝不用普通 copy 冒充 reflink repair。

进入 per-file helper 之前，common shell 会先把 source 和 mirror inventory 完整捕获为 NUL-delimited data，并验证每个 producer 的终止状态；遍历只输出部分 record 后发生 I/O/error exit 会令 sync 以 status `1` 停止，不会把不完整 inventory 当成成功并继续 helper/publish。内部 TSV 只在路径经过 fail-closed validation 后使用：rel 必须是 canonical `sessions/*` 或 `archived_sessions/*`，配置 root 或 rel 包含 tab、newline 或 backslash，以及已存在的 source/mirror root 是 symlink 或非 directory，都以 status `2` 拒绝。Suppression 列表和 snapshot `TMP_LIST` 保持 NUL-delimited，archive 使用 `tar --null -T`，因此 downstream 不会重新分割已接受的路径；带 tab/newline 的路径在更早的 validation 阶段已被拒绝。Inventory/index 位于 owner-private temporary directories，EXIT cleanup 在清理它们时保留原始 primary status。

这些保证只覆盖单个 rollout 的 source read、stage、copy 和 publish。Identity-bound cleanup 会尝试 drain 所有已拥有的 FD/ACL resource，并且不会在第一个 close/free failure 后停止。重试 pre-publication cleanup 时仍绑定最初捕获的 parent/name/object identity；只有证明 namespace durability disposition 后才报告 terminal state。已有主错误时保留其原始对象、类型与 traceback，只附加有界 cleanup diagnostic；没有主错误且 namespace 尚未 durable-complete 时，多个 cleanup failure 会聚合成 actionable fail-closed error。发布异常后，如果 helper 既不能证明 namespace 仍处于 publish 前方向、也不能证明新对象已经发布，它会跳过 cleanup 并附加 `cleanup skipped; orientation unverified`；这不等于保证 stage evidence 仍存在，因为排除在 threat model 外的 same-euid actor 仍可能在窄窗口中删除或替换名称。这个 ownership 保证从 native syscall/C function 的返回值已经 Python-visible、且控制已进入 owner/caller 的首个 guarded body point 后开始。Guarded acquire/handoff 必须原子地把资源登记到已激活且可枚举它的 owner，或在登记失败时安全 drain；完成登记后资源继续由该 owner 持有。在这个边界之后，契约覆盖 guarded acquire、handoff、operation 或首轮 cleanup 期间发生的一次 asynchronous `BaseException`：实现保留 first-primary error 并进行有界的第二轮 drain。测试中的 deterministic line-trace injection 只是这次中断的代理；tracing `call`/`return` event、尚未进入任何 cleanup guard 的函数入口有限嵌套边界、重复或重入 tracing/profiling、无限 cancellation 与 interpreter shutdown 不在保证内，`__del__` 也只是 best-effort 而不是 correctness 依据。如果 native FD 或 ACL acquisition 已在 kernel/C library 中成功、但返回值尚未交给 Python 时恰逢异步中断，纯 Python helper 无法对那个尚未 Python-visible 的资源执行 identity-bound close/free。实现不会扫描 `/dev/fd` 猜测遗留 FD，也不宣称使用 native signal masking。Native acquisition 自身在没有交给 Python 资源时直接报告错误，仍按普通错误路径处理；上面的异步中断保证从资源已 Python-visible 且控制进入首个 guarded body point 后开始。Shell orchestration 仍有 `mkdir -p`、stale relocation 和 duplicate prune 等不在同一个 held-FD transaction 中的 namespace operation，因此不应将整个 mirror sync 描述为已完成 race hardening。Helper 对路径做逐组件 no-follow 打开和 final reopen，但不验证 source/destination direct parent 之上所有 canonical ancestor 的 access policy；这些更高层 namespace 必须由运行环境保证稳定且不能被其他 principal 替换，例如经过 `/private/tmp` sticky directory 的测试路径必须依赖对应的环境边界。Snapshot-wide lock 可以排除所有遵守协议的并行 run，但它和 held-FD helper 保护的是点时绑定。Shell `mkdir`/prune 周边与最后 check-to-syscall/receipt 微小窗口内的恶意 same-euid namespace replacement 都在保证范围外，这与 snapshot-lock 的 namespace 边界一致。

## Quiet launchd window

存量 repair 使用独占的静默窗口，不与日快照并行。Codex 可以继续运行；仍在写入的 rollout 会 fail closed，并可排队留到下一次日快照重试。

开始前先确认 job idle 且没有相关 mirror/snapshot/repair process。如果 job 正在运行，等待它自然结束；不要使用 `kickstart -k` 强制终止：

```bash
domain="gui/$(id -u)"
label="io.github.joey-tools.codex.snapshot.daily"
plist="$HOME/Library/LaunchAgents/$label.plist"

launchctl print "$domain/$label"
pgrep -af 'codex_snapshot_daily|codex_repair_rollout_reflinks' || true
launchctl bootout "$domain/$label"
```

`bootout` 后再次确认 label 不再 loaded 且没有相关进程，再进入 canary 或 full repair。结束后恢复 job，并使用不带 `-k` 的 kickstart 补跑一次：

```bash
launchctl bootstrap "$domain" "$plist"
launchctl print "$domain/$label"
launchctl kickstart "$domain/$label"
```

补跑期间等待 job 回到 idle，检查 exit status 和 snapshot log；不要仅凭 `kickstart` 命令成功判定任务完成。

`codex_snapshot_daily.sh` 由 `codex_snapshot_lock.py` supervisor 持有一个 identity-bound、owner-private 的 snapshot-wide protocol lock；锁覆盖 mirror sync、retry、文件清单、压缩、OneDrive publish 和 unpin 的整个脚本生命周期。它会串行化所有遵守该入口且共享同一 state-root lock 的 snapshot run。Configured staging path 必须是 non-symlink directory；既存 symlink 或非 directory 会在接触其 target 前 fail closed，缺失时脚本先创建目录，再以 `st_dev:st_ino` 记录最终 staging directory object identity。脚本会在 zstd 旧 tmp 清理、zstd 新 tmp 创建和 gzip 新 tmp 创建前重新证明同一 identity。在这个 bound directory 中成功开始新的 zstd compression 之前，脚本会删除旧的 matching staging files；cleanup 失败会在创建新 tmp 之前中止，因此协作调用不会主动生成第二个 matching zstd tmp。这个 snapshot lock 与 repair utility 自己的 `repair.lock` 是不同协议，不能替代存量 one-off repair 的 launchd bootout/quiet-window 要求。

Held FDs、identity revalidation 和 private stage policy 能排除其他 principal 的普通替换，但 Darwin 没有 inode-conditional `unlink` 或 `rmdir`。恶意的 same-euid actor 若恰好在最后一次 name/identity check 与最终 syscall 之间替换 mirror、repair 或 snapshot staging 对象，超出保证范围；因此静默窗口还要求同一用户不要并行手工改动 mirror、stage、repair state 或 snapshot staging namespace。

## Canary before full repair

操作顺序固定为 dry-run、representative canary（代表性小样本）、显式确认、full repair：

1. 保存完整 `inventory --json` 和 `repair --json` dry-run receipts。
2. 从 live/archived、不同大小和近期活跃程度中选择少量 rollout ID，并同时限制文件数与总字节数。
3. 运行 canary，复核 receipt、mirror 字节和 retry queue；在继续 full repair 前取得显式确认。
4. 获确认后才去掉 canary 的 ID/size limits 运行全量 repair。

Canary 示例（ID 和上限仅为占位，执行前必须替换为本次 inventory 选出的值）：

```bash
python3 scripts/codex_repair_rollout_reflinks.py repair \
  --apply \
  --max-files CANARY_FILE_COUNT \
  --max-bytes CANARY_BYTE_LIMIT \
  --rollout-id LIVE_ROLLOUT_ID \
  --rollout-id ARCHIVED_ROLLOUT_ID \
  --queue-unstable \
  --json
```

显式确认后的 full repair：

```bash
python3 scripts/codex_repair_rollout_reflinks.py repair \
  --apply \
  --queue-unstable \
  --json
```

## Retry and crash recovery

普通日快照先在 snapshot-wide lock 内运行 `recover --apply`，然后执行 mirror sync，再用 `retry --apply` 处理默认 retry queue，最后才生成 archive。Recovery fatal 会阻止当次 sync/retry/archive，避免 shell 在未解决的 durable namespace state 上继续变更 mirror。需要手工处理默认 queue 时：

```bash
python3 scripts/codex_repair_rollout_reflinks.py retry --apply --json
```

Repair 在创建 stage 之前先持久化 `PLANNED` intent；记录 stage identity 后推进为 `STAGE_BOUND`，记录 clone identity 和完整 protected snapshot 后推进为 `CLONE_BOUND`。Clone 验证完成后才发布 `PREPARED` manifest，随后才允许 namespace swap。这些 durable state 是中断后的 transaction fences（事务栅栏）；不要手工删除 repair state、staging clone 或 rollback object。进程中断或主机重启后，先在同一静默窗口运行 recovery：

```bash
python3 scripts/codex_repair_rollout_reflinks.py recover --apply --json
```

Recovery dry-run 只报告 pending state，不调用 cleanup backend，也不改 queue、intent、manifest、clone 或 mirror；前述 repair state/lock 创建与加固仍然适用。

对于尚未发布 `PREPARED` manifest 的 durable intent，apply recovery 只接受精确可证明的状态：

- `PLANNED` 可以接受 stage absent，或只收养 recorded container 中 exact txid-shaped path 上、policy 安全且完全为空的 stage，再将它删除。Symlink、错误路径、非空目录或不安全 policy 都会保留证据并 fail closed。
- `STAGE_BOUND`/`CLONE_BOUND` 只能清理唯一的 `clone` entry；size/digest 必须匹配 durable evidence，`CLONE_BOUND` 还要求完整 recorded clone identity/policy snapshot 匹配。
- 删除任何 pre-`PREPARED` clone 前后，final mirror name 都必须仍映射到 recorded original survivor，且其完整 protected snapshot 保持不变。无法证明 original survivor、出现额外 entry，或 namespace orientation 不唯一时，工具 exit `2` 并保留 intent/stage/object 供调查。

如果 `PREPARED` recovery 发现 recorded source path 已 move、missing 或被替换，工具不会按 rollout UUID 猜测新路径，也不会继续 forward commit。它会只凭 durable object evidence 进入 identity-bound rollback：必要时 swap back，验证 original survivor，删除 clone/stage，并把结果持久化为 `DEFERRED`；启用 queue 时再排队等待按 UUID 重新 discovery。任何 orientation、identity 或 protected snapshot 无法证明时仍会 exit `2` 并保留证据。

无论清理的是 pre-`PREPARED` intent 还是 rollback manifest，只要事务启用了 queue，retry entry 都必须先 durable publish，最后一份 intent/manifest fence 才能删除。Queue 写入失败会保留 fence，下一次 recovery 先恢复 retry obligation，不会产生“对象已清理但重试责任丢失”的静默成功。Recovery 完成后重新运行 inventory/dry-run，再决定是否继续 canary 或 full repair。

## Snapshot and OneDrive verification

Local repair 的完成标准是 mirror、repair state 和 retry behavior；OneDrive publish 是独立判定。Full repair 后补跑日快照，并分别记录：

- mirror sync/retry 是否完成，queue 是否只保留仍然 deferred 的项目
- snapshot compression 是否成功
- OneDrive publish 和 unpin 是否成功
- 在 snapshot-wide lock 边界内，staging 根目录是否最多只有一个 `codex-rollouts-*.tar.zst.tmp.*`

OneDrive publish 失败不应倒推为 local reflink repair 失败。单独报告 publish failure，并确认 zstd staging 上限；该上限不包括 `*.tar.zst.publish.tmp.*`、`*.tar.gz.tmp.*` 或其他文件。
