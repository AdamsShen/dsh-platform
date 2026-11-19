# ADR-003 工作流包存储：本地内容寻址，对象存储后置

- 状态：已实现（本地）；S3 后端待 P5-T07
- 日期：2026-11-13（记录补写于 2026-11-19）
- 相关：P2-T03 / P2-T08；`src/ao/package.py`、MODEL D7

## 背景

MVP 把 manifest 塞进 `workflow_versions.manifest_json` 一列，没有"包"的概念：提示词、`graph.py`、页面、冒烟脚本无法随版本冻结，也无法回答"这次跑的是不是发布的那份"。

## 决策

1. 包是**目录**，落 `<packages_dir>/<workflow>/<version>-<manifest_hash>/`。
2. `manifest_hash = sha256(manifest 规范序列化 + 排序后逐文件 sha256)`（`src/ao/package.py:51`）。
3. 写入原子：先写临时目录，再 rename 到目标路径（`freeze_package`，`package.py:117`）。
4. 读取必须校验 hash，不一致拒绝加载（`load_package(..., verify=True)`，`package.py:100`），防止"跑的不是发布的"。
5. `package_ref` 支持三种写法：绝对目录、`<id>@<version>`、`<id>/<version>-<hash>`（`resolve_ref`，`package.py:79`）。
6. 对象存储（S3/OSS）作为**可选后端**，默认 `local` 保持零依赖；切换后 `resolve_ref` 下载到本地缓存再加载。

## 理由

- 内容寻址让"同一版本重复发布"幂等，也让篡改可检测（改动任一文件，hash 变化，加载被拒）。
- 本地目录零依赖，开发与单机部署不需要额外服务；多机部署才需要共享存储。
- `package_ref` 三种写法兼容了"开发期直接指目录"与"发布期指冻结版本"。

## 替代方案

| 方案 | 否决原因 |
|---|---|
| 只存 DB（`manifest_json` 一列） | 无法冻结 `graph.py` 与提示词文件，改一个字节就跑错版本 |
| 直接上 OSS/S3 | 开发期需要凭据与网络，单机启动多一个失败点 |
| git 仓库当包存储 | 需要额外 clone/pull 流程，与"发布即冻结"不一致 |

## 后果

- 正向：`resolve_ref` 是运行期唯一入口，P2-T08"发布 v2 后重跑 v1 仍用 v1 的包"可验证。
- 负向：**多机部署时 `package_ref` 是本地路径，各 worker 看不到同一份包**——这是需求 #10/#14 的阻塞项，已在 `docs/STATUS.md` 遗留清单第 1 条与 P5-T07 排期。
- 过渡方案：多副本前先用共享卷把 `packages_dir` 变成同一路径（只解决同可用区）。

## 验证

- `tests/integration/test_store.py`：写入后篡改 `graph.py` → 加载失败；同一 manifest 重复写入幂等。
- 运行期：`executor` 只通过 `load_package` 取包，不读 DB 里的 manifest。
