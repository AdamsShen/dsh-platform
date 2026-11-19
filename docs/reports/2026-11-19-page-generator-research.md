# 页面生成器调研复核（P4-T01，M4a-W40）

日期：2026-11-19
目的：为 P4-T02（Page Schema 契约）与 P4-T03（编译器）定形态，先看业内怎么做，再决定本项目怎么做。

## 1. 结论摘要

| 问题 | 业内主流做法 | 本项目取舍 |
|---|---|---|
| LLM 产出什么 | **声明式 JSON / 组件树**，不是可执行代码 | 沿用：Page Schema 是 IR，LLM 只写 IR |
| 如何防止乱用组件 | 组件目录 + 白名单 + 客户端预注册（A2UI 的 catalog/registry） | 沿用：`COMPONENTS` 白名单 + 未知 type 直接拒绝 |
| 校验放在哪 | 生成侧与渲染侧各一次，错误定位到节点 | 沿用：发布期 `validate()` 一次报全，编译期再断言产物无 `eval(` / 内联 `on*=` |
| 表达式 | 受控子集（`$filters.x` 这类路径），不落任意 JS | 沿用：只允许 `$workflow/$selection/$filters/$user` 的路径表达式 |
| 渲染 | 运行时解释 JSON，或编译成代码 | **本项目走编译**（2026-11-19 D15 修订）：产物是静态 `index.html/app.js/theme.css`，可 diff、可下载 |
| iframe 托管 | `sandbox="allow-scripts"` 且**不给** `allow-same-origin`；CSP 拦不住 srcdoc 内的脚本，必须靠 sandbox | 沿用：iframe 无 `allow-same-origin`，数据走平台 API + 短期 token |

## 2. 来源

1. **A2UI（Google，面向 Agent 的声明式跨端 UI 规范）**：UI 作为声明式 JSON 传输，客户端维护 catalog/registry 把抽象组件映射到本地实现，agent 不直接下发代码。
   <https://refft.com/google_A2UI.html>
   对本项目有用的是三点：扁平组件 + ID 引用便于增量更新；组件注册表预批准；wrapper 上施加沙箱策略。
2. **MateChat NL2UI 架构（CSDN）**：把组件库简化成 RAG 索引 + few-shot，Prompt 里明确"仅使用白名单内的组件"，LLM 输出 `[{component, props}]`。
   <https://blog.csdn.net/2301_79779756/article/details/155360456>
   证实"白名单写进生成契约 + 生成后校验"是通行做法，而不是靠模型自觉。
3. **Generative UI 一年实践（Jacar）**：生产环境里 LLM 返回的是受控描述，不是任意前端代码；失败模式集中在"模型编造不存在的组件/字段"。
   <https://jacar.es?p=99/>
4. **Delta4：Generative UI 的精度分级**：按后果分级——低后果允许模型自由填参，高后果（转账、删除）必须"模型建议 + 人确认"。
   <https://delta4.io/blog/what-is-generative-ui-how-ai-renders-interactive-components>
   对应本项目 `confirm: true` 的写操作动作。
5. **iframe sandbox 安全指南**：sandbox 是基线，CSP 是补充；需要脚本时给 `allow-scripts`，但要拿不到宿主 cookie 就不能给 `allow-same-origin`。
   <https://nhimg.org/iframe-sandbox-and-srcdoc-security-guide>
6. **CSP 对 iframe srcdoc 不生效的实证**：父页面的 meta CSP 不约束 srcdoc 文档，托管安全必须落在 sandbox 与独立响应头上。
   <https://nebelwolfi.xyz/blog/2026-04-03-the-meta-tag-csp-escape-hatch-that-iframe-srcdoc-opens>

## 3. 与本项目现有约定的兼容点

- 与 `docs/decisions/ADR-006-graph-codegen.md` 一致：代码由模板生成、生成物不作为可编辑真值。
- 与 P2 的包冻结一致：`ui/compiled/` 随包一起内容寻址，运行期不编译。
- 与 P1 的自研内核一致：不引入 Node 构建链（选型 A：无框架静态站点）。

## 4. 未采用的做法及原因

| 做法 | 不采用的原因 |
|---|---|
| amis v6 运行时渲染 | 产出是配置而不是代码，不满足"前端代码自动生成"的要求（P4 修订已记录） |
| React + Vite 生成工程 | 每页构建秒级、产物 MB 级、镜像要带 Node 工具链；本项目页面类型固定，收益不足 |
| 让 LLM 直写 HTML/JS | 无法做字段级校验与 diff，XSS 面不可控；发布时间也不可预测 |

## 5. 待验证假设（进 M4b 前必须回答）

- 无框架运行时能否覆盖"表格内联编辑 + 拖拽排序"？若不能，把这两类控件降级为 B 档预编译组件，而不是整体换 React。
- 分享态（P4-T10）需要服务端裁剪字段，编译产物要能表达"分享态隐藏列"；当前 Schema 用 `shareVisible: false` 预留，实现前先用真实数据验证。
