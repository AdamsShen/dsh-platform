# bwrap 在非特权容器内的可行性矩阵（2026-11-14）

## 环境

- 宿主：macOS（OrbStack），容器架构 `linux/arm64`
- 镜像：`docker.m.daocloud.io/library/python:3.12-slim`（Debian，Python 3.12.16 装在 `/usr/local`）
- 沙箱：`bubblewrap 0.12.0`（apt 安装）
- 被测代码：`src/ao/sandbox.py` 的 `build_bwrap()` 原样复用，容器内只跑探针脚本
- `max_user_namespaces = 31975`（内核允许非特权 userns，限制来自 seccomp/能力）

## 矩阵结果

四种组合 = `mount_proc` × `mount_dev`，容器条件三档。

| 容器条件 | `--proc` | `--dev` | 结果 | 失败点 |
|---|---|---|---|---|
| 默认 seccomp | 任意 | 任意 | ✗ 全部 | `No permissions to create a new namespace` |
| `seccomp=unconfined` | ✓ | ✓ | ✗ | `Can't mount proc on /proc: Operation not permitted` |
| `seccomp=unconfined` | ✓ | ✗ | ✗ | 同上（proc 是硬失败点） |
| `seccomp=unconfined` | ✗ | 任意 | ✓ 能启动 | — |
| `seccomp=unconfined` + `CAP_SYS_ADMIN` | ✓ | ✓ | ✗ | `loopback: Failed RTM_NEWADDR`（前移到网络配置） |
| `seccomp=unconfined` + `CAP_SYS_ADMIN` | ✗ | 任意 | ✓ 能启动 | — |

结论：

1. **默认 seccomp 一定失败**：`clone`/`unshare` 被拦，与参数无关。容器必须 `--security-opt seccomp=unconfined`。
2. **`--proc` 是硬失败点**：放开 seccomp 也挂不上，加 `CAP_SYS_ADMIN` 只是把失败点前移到网络配置。要走通只剩 `--privileged`，对"跑不可信代码"的场景不可接受。
3. **可用组合是"不挂 /proc + 不挂 /dev"**，对应 `AO_SANDBOX_MOUNT_PROC=0`、`AO_SANDBOX_MOUNT_DEV=0`。
4. root 与 uid 1000 两种运行身份结论一致（都验证过）。

## 裸机实测（阿里云 ECS，Ubuntu 24.04.4 / kernel 6.8.0-136）

用 `workbench` CLI 远程执行（`profile=intl`，`cn-hongkong`，实例 `i-j6cazclc2tnx3fdg38g8`），
装 `bubblewrap 0.9.0`（apt），把同一份 `src/ao/sandbox.py` + `settings.py` 打包上传后原样跑：

| 配置 | probe | 沙箱内 python3 | 写 workspace | 网络 | 宿主目录 |
|---|---|---|---|---|---|
| 默认（挂 `/proc` + `/dev`） | True | `PY_OK` | 成功 | 不可达 | 不可见 |
| `MOUNT_PROC=0` `MOUNT_DEV=0` | True | `PY_OK` | 成功 | 不可达 | 不可见 |

宿主 userns 完全放开：`kernel.apparmor_restrict_unprivileged_userns = 0`、
`kernel.unprivileged_userns_clone = 1`、`max_user_namespaces = 122238`；
nobody 用户建 userns 得到 `0 65534 1`（非 identity）。

三条结论：

1. **裸机形态默认配置就能用**，不需要关 `/proc`，也不涉及 seccomp——与容器形态正好相反。
2. **隔离面有效**：`/data`、`/root`、`/home/*`、`/etc/shadow` 全部不可见；`workspace/` 之外不可写。
3. **网络被切断**（`--unshare-all` 含 `--unshare-net`，实测 `NET_BLOCKED`）。这是后续把 harness
   包进沙箱的硬约束：harness 必须访问模型 API，包进去就得另开出口（代理 / veth / 选择性共享网络）。

## 过程中钉住的四个真实缺陷（已修）

### 1. 探测与运行时配置不一致，形成死锁

`probe()` 原本固定 `mount_proc=True, mount_dev=True`。于是在"必须关掉 /proc 才能跑"的环境里：
运行时能跑，但探测永远失败 → `require_available()` 一直拒绝执行，等于沙箱不可用。

修法：`probe()` 改为读取 `settings.sandbox_mount_proc/dev`，跟运行时同一套配置。

### 2. 不挂 /proc 时无法读 uid_map，隔离失去断言

`probe()` 原本靠读 `/proc/self/uid_map` 判断 userns 是否生效。不挂 /proc 就读不到，只能"没输出也当通过"，这是把 fail closed 变成 fail open。

修法：`build_bwrap()` 补显式 `--unshare-user`。`--unshare-all` 里的 userns 是 `-try`（建不起来静默跳过，结果是"沙箱起来了但没隔离"）；显式版本建不起来会直接非零退出，因此不挂 /proc 时也能用"能不能起来"断言隔离。

### 3. 只读绑定漏掉解释器前缀，沙箱内 python 起不来

沙箱内 `python3 -V` 报 `error while loading shared libraries: libpython3.12.so.1.0`（rc=127）。

原因：只绑了 `/usr /bin /sbin /lib /lib64`，而官方 python 镜像把解释器装在 `/usr/local`。

修法：`readonly_roots()` 加入 `sys.base_prefix` 与 `/usr/local`。

### 4. 缺 `ld.so.cache`，库文件可见仍加载失败

补了 `/usr/local` 后 `libpython3.12.so.1.0` 在沙箱内 `ls` 可见，但 python 仍 rc=127。

原因：动态链接器靠 `/etc/ld.so.cache` 定位库，而只绑了 `/etc/alternatives`，缓存缺失 → 回退默认搜索路径（不含 `/usr/local/lib`）。

修法：`readonly_roots()` 加入 `/etc/ld.so.cache`、`/etc/ld.so.conf`、`/etc/ld.so.conf.d`。

附带：关掉 `--dev` 后连 `cmd 2>/dev/null` 都报 `Permission denied`。修法是补最小设备节点并**用 `--dev-bind` 而不是 `--ro-bind`**（重定向是写打开）。

## 端到端验证

容器内跑真实链路（`probe` → `require_available` → `launch_argv`）：

| 配置 | probe | require_available | 真实执行 |
|---|---|---|---|
| 默认（`proc=1 dev=1`） | False，原因 `Can't mount proc` | 被拒绝 | — |
| `proc=0 dev=0` | True | 通过 | `rc=0`，输出 `RUN_OK` / `PY_OK` |

执行内容验证了三件事：shell 能跑、`python3` 能跑（含 import 级加载）、只能写 `workspace/`（`prompts/` 未被写穿）。

## 仍未解决：沙箱没有执行点

`executor.execute()` 目前只有一行守卫：

```631:640:/Library/git-root/dsh-platform/src/ao/executor.py
    try:
        package = load_package(run["package_ref"])
        manifest = parse(package.manifest)
        from ao.sandbox import require_available

        if not settings.mock:
            # fail closed：真跑时二进制与隔离能力都要过（见 sandbox.probe 的 uid_map 断言）；
            # mock 不起子进程，不受沙箱限制
            require_available()
```

`build_bwrap` / `launch_argv` / `sanitize_inputs` 都没有调用方：graph.py 由 `_load_graph_module` 在当前进程内加载，节点是 harness turn，其子进程也未经过 `launch_argv`。

因此当前状态是"能力探测 + fail closed 守卫已就位，但没有把任何东西真正关进沙箱"。要闭环需要决定包裹边界（包 harness 进程 / 包 graph.py / 两者都包），属于设计决策，未在本轮改动范围内。

## 部署要求（按形态分）

**裸机 / ECS 直接跑（已实测通过）**

1. `apt-get install bubblewrap`；
2. `AO_SANDBOX=bwrap`，`MOUNT_PROC` / `MOUNT_DEV` 保持默认 1；
3. 无需改 seccomp / AppArmor（前提：`apparmor_restrict_unprivileged_userns=0`，Ubuntu 24.04 默认如此）。

**跑在 Docker 容器内（已实测）**

1. 必须 `--security-opt seccomp=unconfined`，默认 seccomp 下 bwrap 完全不可用；
2. 必须 `AO_SANDBOX_MOUNT_PROC=0`、`AO_SANDBOX_MOUNT_DEV=0`，`--proc` 挂不上；
3. 镜像内装 `bubblewrap`，并确认 `ld.so.cache` 存在（`ldconfig` 生成）。

**共同前提**：无论哪种形态，`--unshare-all` 都会切断网络，需要访问外网的命令不能在沙箱内直接跑。
