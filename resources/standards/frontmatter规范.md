---
status: active
reviewed_by: 原规范审核人
modified: 2026-07-31 09:56:00
tags:
  - 知识库规范
  - frontmatter
---

# Frontmatter 规范

> 团队知识库 frontmatter 唯一标准。所有正式文档必须遵循本规范。
> 关联：[[团队知识库方案设计]] §3

---

## 1. 字段总览

通用字段 11 个（必填 4 + 按需 7），另有类型专用字段（见 §1.3）。

### 必填（4 个）

| 字段 | 取值 | 说明 |
|------|------|------|
| `status` | `draft` / `active` | `draft` = AI 草稿；`active` = 人工审核后 |
| `reviewed_by` | `AI-draft` 或审核人 | AI 产出填 `AI-draft`，人工审核后改审核人（不写死具体人名） |
| `modified` | `YYYY-MM-DD HH:mm:ss` | 最后修改文件时间，精确到时分秒，手动填写，非 git 提交时间 |
| `tags` | 字符串列表 | 必填，用于双向链接和检索 |

### 按需（7 个）

| 字段 | 取值 | 说明 |
|------|------|------|
| `version` | 版本号字符串 | 文档对应的版本 |
| `customer` | 客户名 | 仅客户定制知识填写 |
| `modules` | 字符串列表 | 关联的代码模块 |
| `author` | 人名 | 撰写人，与 `reviewed_by`（审核人）区分 |
| `source` | 溯源信息 | 知识来源溯源。代码类文档填源码 URL 或资料路径（如 `code/example-project develop`），与 `git_branch`/`git_commit` 配合使用；需求文档填需求来源类别：客户反馈 / 产品规划 / 内部优化 / 竞品对标 |
| `git_branch` | 分支名 | 源码分支，与 `source` 配合使用 |
| `git_commit` | commit hash | 源码精确版本，与 `git_branch` 配合使用 |

### 类型专用（按需）

| 字段 | 适用类型 | 取值 | 说明 |
|------|---------|------|------|
| `priority` | 需求 | `P0` / `P1` / `P2` / `P3` | 需求优先级，判定口径见下表 |

`priority` 各级判定口径（[[需求]] 模板与本表一致，以本表为准）：

| 取值 | 含义 | 判定口径 |
|------|------|---------|
| `P0` | 核心 | 缺失则设备无法正常工作或无法出货 |
| `P1` | 核心竞争力 | 大幅提升效率，或直接影响产品价值、用户体验、市场竞争力 |
| `P2` | 一般 | 提升部分体验或产品完整性，不阻塞出货但影响特定场景 |
| `P3` | 可选／预留 | 后续迭代或特定型号按需支持 |

⚠️ `P0`–`P3` **专属需求优先级**，不得用于缺陷严重度——严重度用中文五级 致命/严重/一般/提示/建议（权威定义见下表）。优先级答的是「多要紧」，严重度答的是「坏得多厉害」，两者正交。

**缺陷严重度五级**（团队唯一权威；产品验收阻塞判定、软测缺陷分级均以本表为准）：

| 等级 | 默认判定口径 | 是否阻塞发布 |
|------|------|------|
| 致命 | 设备无法启动/无法通讯、数据丢失或损坏、需求核心功能完全不可用 | **阻塞** |
| 严重 | 核心功能部分不可用或结果错误、有可复现的异常但存在临时规避手段 | **阻塞**（除非产品在 G1 逐项接受并记明理由） |
| 一般 | 非核心功能缺陷、边界场景处理不当、体验明显受损但功能可达 | 不阻塞，记入遗留清单评估 |
| 提示 | 提示文案、日志措辞、极端场景下的轻微表现差异 | 不阻塞 |
| 建议 | 优化建议/增强类，非缺陷 | 不阻塞 |

---

## 2. 字段顺序（强制）

```text
customer → modules → source → git_branch → git_commit → author → reviewed_by → modified → status → priority → version → tags
```

规则：
- 按需字段没有时直接省略，不留空
- 必填字段不能省略
- 类型专用字段（如 `priority`）紧跟 `status` 之后
- 字段顺序固定，不得打乱

---

## 3. 关键约束

- ⚠️ **非必要不得自创字段**：frontmatter 优先用本规范定义的字段。确有必要时可扩展（如需求专用 `priority`），但需有明确理由并补充到本规范。`title`、`platform`、`type`、`project`、`folder`、`confidence` 等已在设计阶段明确剔除——文件位置由目录结构表达，标题由 Markdown 一级标题表达。
- `modified` 是**文件修改时间**（精确到时分秒，格式 `YYYY-MM-DD HH:mm:ss`），不是 git 提交时间。每次修改文件都要更新。
- `tags` 是双向链接的载体，每篇文档至少一个标签。

---

## 4. 完整示例

```yaml
---
customer: 客户A
modules:
  - report_cmpt
  - module_extend_temp
source: code/example-project develop
git_branch: develop
git_commit: af1bf2d
author: 张三
reviewed_by: AI-draft
modified: 2026-06-28 14:30:00
status: draft
version: v1.0
tags:
  - 温度告警
  - 上报
---
```

需求文档示例（`source` 表达需求来源而非代码溯源）：

```yaml
---
source: 客户反馈
author: 张三
reviewed_by: AI-draft
modified: 2026-06-28 14:30:00
status: draft
priority: P1
tags:
  - 温度告警
---
```

最小示例（只有必填字段）：

```yaml
---
reviewed_by: AI-draft
modified: 2026-06-28 14:30:00
status: draft
tags:
  - 系统架构
---
```
