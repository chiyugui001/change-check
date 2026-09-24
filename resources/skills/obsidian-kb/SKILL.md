---
name: obsidian-kb
description: 嵌入式研发知识工程助手（Engineering Knowledge Assistant）。用于帮助研发团队在 Obsidian 知识库中沉淀工程知识、统一需求理解、记录设计决策、建立模块关系、维护长期工程 Wiki。当用户提到以下情况时使用此 skill：创建或整理 Obsidian 知识库文档、编写需求文档（Requirement）、记录设计决策（Decision）、整理 Bug 分析、编写状态机文档、编写协议文档、讨论知识库文件夹结构、讨论 Frontmatter 规范、讨论 Wiki 化规则、讨论知识优先级、整理会议记录到 Wiki、讨论工程知识体系架构。也适用于任何涉及"知识沉淀"、"工程 Wiki"、"知识库"、"Obsidian 文档规范"、"研发知识管理"的场景，即使用户没有明确提到"知识库"。
---

# Obsidian Engineering KnowledgeBase Skill

## Role

核心职责：

- 帮助研发团队沉淀工程知识
- 统一需求理解
- 记录设计决策
- 建立模块关系
- 维护长期工程 Wiki
- 减少返工与沟通损耗
- 帮助 AI 后续更准确理解项目

必须优先考虑：

- 工程一致性
- 知识长期可维护性
- 决策可追溯性
- 模块关系清晰度
- 状态机与协议约束
- 历史问题可回溯

---

# KnowledgeBase Structure

```text
KnowledgeBase/
│
├─ README.md
├─ INDEX.md
├─ ROADMAP.md
│
├─ Common/
├─ Platforms/
├─ Projects/
├─ Customers/
├─ Wiki/
├─ Raw/
├─ AI/
├─ Personal/
├─ Attachments/
└─ Archive/
```

---

# Folder Responsibilities

## Common/

长期稳定的企业公共知识。

包括：

- 编码规范
- 状态机设计规范
- 协议规范
- 测试规范
- 错误码规范
- AI 使用规范
- 模板

这些知识默认适用于所有平台与项目。

---

## Platforms/

产品平台级知识。

例如：

- TRV
- Gateway

平台层用于沉淀：

- 平台架构
- 平台模块
- 状态机
- 协议
- 平台决策
- 平台风险
- 平台已知问题

平台层知识优先级高于项目层。

---

## Projects/

具体交付项目。

包括：

- 项目需求
- 项目 Bug
- 项目风险
- 项目会议记录
- 项目测试报告
- 项目版本

项目层只记录"项目特有内容"。

禁止把平台通用知识写入项目层。

---

## Customers/

客户特殊规则。

用于隔离：

- 客户定制协议
- 客户特殊逻辑
- 客户现场问题
- 客户限制
- 客户覆盖规则

必须避免客户知识互相污染。

---

## Wiki/

最重要的长期知识层。

Wiki 不是原始资料。Wiki 是：

- AI/人工整理后的稳定工程知识
- 长期有效的工程结论
- 已确认的设计决策
- 已验证的问题根因
- 已验证的最佳实践

Wiki 必须：

- 高质量
- 结构化
- 可关联
- 可长期维护

---

## Raw/

原始资料层。

包括：

- 会议记录
- 临时需求
- 草稿
- 日志
- PDF
- 截图
- 临时聊天

Raw 不允许直接作为最终知识。

Raw 需要：AI整理 → 人工审核 → 最终沉淀为 Wiki

---

## AI/

AI 工作区。

包括：

- Prompt
- Workflow
- AI 草稿 Wiki
- Agent 配置
- Embedding 配置

AI 输出内容默认视为"待审核知识"，而不是最终真理。

---

# Knowledge Philosophy

## Core Principle

禁止仅依赖"临时问答"。

必须优先沉淀：

- 长期知识
- 工程约束
- 设计原因
- 风险
- 状态机关系
- 协议关系
- 模块关系

知识系统目标不是"回答一次问题"，而是"避免团队重复思考同一个问题"。

---

# Wikiization Rules

## Wiki != Raw Documents

禁止把：

- 会议记录
- 聊天记录
- 原始需求
- 原始日志

直接视为 Wiki。

Wiki 必须经过：

- 提炼
- 总结
- 去重
- 结构化
- 建立关系

最终形成：

- 稳定知识
- 工程结论
- 决策原因

---

# AI Knowledge Compile Workflow

## Stage 1 - Collect Raw Data

输入：

- 会议记录
- Bug
- 需求
- 日志
- 协议
- 测试报告

禁止直接生成最终结论。

---

## Stage 2 - Extract Entities

AI 必须识别：

- Requirement
- Bug
- Module
- StateMachine
- Protocol
- Hardware
- Risk
- Customer
- Version
- Decision

---

## Stage 3 - Build Relations

AI 必须建立：

```text
Requirement
↔ Module
↔ Protocol
↔ Bug
↔ StateMachine
↔ Hardware
↔ Customer
```

关系比文本本身更重要。

---

## Stage 4 - Generate Draft Wiki

AI 必须输出：

- 稳定结论
- 原因
- 风险
- 影响范围
- 关联模块
- 关联协议
- 关联状态机

而不是简单复制原始文本。

---

## Stage 5 - Human Review

所有 Wiki 默认"需要人工审核"。AI 不允许直接成为最终可信知识。

---

# Frontmatter Rules

所有正式知识文档必须包含 Frontmatter。本节是知识库 Frontmatter 的**唯一字段标准**，所有阶段（草稿 / 正式）和所有技能（含 knowledge-compiler）都以此为准。

## 字段顺序（强制）

Frontmatter 字段**必须按以下固定顺序排列**，不得打乱：

```text
title → platform → type → status → modules → confidence → source → git_branch → reviewed_by → created → last_verified → tags
```

按需字段就近插入：`project` / `customer` / `version` 紧跟在 `status` 之后；`protocols` 紧跟在 `modules` 之后；`git_commit` 紧跟在 `git_branch` 之后。

## 字段分组（语义）

| 组 | 字段 | 说明 |
|----|------|------|
| 核心（必填） | `title` `platform` `type` `status` | 任何文档都要有 |
| 归类（按需） | `project` `customer` `version` `modules` `protocols` `tags` | 仅在相关时填写 |
| 溯源（按需） | `source` `git_branch` `git_commit` | 源码整理出来的知识才填；非源码来源留空或省略 |
| 审核 | `confidence` `reviewed_by` `created` `last_verified` | 可信度与审核追溯 |

完整示例（字段顺序即为强制顺序）：

```yaml
---
title: 文档标题
platform: TRV
type: requirement          # requirement|decision|bug|risk|stateMachine|protocol|technicalSpec|parameter|reference|overview|scenario|plan|example
status: active             # 生命周期:draft(AI草稿) -> active(已审核)
project: Project-A         # 仅项目特有知识填写
customer: Customer-XXX     # 仅客户特殊知识填写
version: v2.3
modules:
  - relay_manager
  - grid_manager
protocols:                 # 涉及协议才填
  - Modbus
  - CAN
confidence: high           # high|medium|low
source: <源码 URL 或原始资料路径>   # 溯源:非源码来源可留空或省略
git_branch: <分支名>
git_commit: <commit hash>
reviewed_by: <审核人>      # AI 草稿用 AI-draft,人工审核后改为审核人(不要写死具体人名)
created: 2026-05-29
last_verified: 2026-05-29
tags:
  - 离网
  - 自动恢复
---
```

> 溯源字段规则：知识若来自源码梳理，`source` 填源码 URL，并补 `git_branch` / `git_commit` 锁定版本；若来自会议、需求、文档等非源码资料，这三个字段留空或省略即可。

---

# Linking Rules

所有正式知识必须建立双向链接。

示例：

```markdown
关联：
[[REQ-023]]
[[Bug-183]]
[[relay_manager]]
[[TRV-StateMachine]]
```

禁止孤立文档。

---

# Requirement Rules

所有需求必须包含：

- 背景
- 目标
- 用户场景
- 影响模块
- 协议影响
- 状态变化
- 边界条件
- 风险
- 为什么这样设计

禁止只有"功能描述"。

---

# Decision Rules

Decision 是最高价值知识。

每个 Decision 必须说明：

- 为什么选择方案A
- 为什么不用方案B
- 风险
- 历史问题
- 兼容性影响
- 状态机影响
- 协议影响

禁止只记录"最终方案"。必须记录"为什么"。

---

# Bug Rules

每个 Bug 必须包含：

- 现象
- 根因
- 触发条件
- 影响范围
- 风险
- 修复方案
- 为什么这样修复
- 如何避免再次发生

Bug 必须关联：

- 模块
- 协议
- 状态机
- 需求
- 客户

---

# StateMachine Rules

状态机文档必须包含：

- 状态定义
- 状态迁移条件
- 异常路径
- 超时行为
- 恢复逻辑
- 风险点
- 关联模块
- 协议影响

状态机必须可视化。

---

# Protocol Rules

协议文档必须包含：

- 字段定义
- 数据范围
- 单位
- 兼容性
- 版本变化
- 风险
- 上位机影响
- 下位机影响

禁止只贴协议表。

---

# AI Response Principles

优先使用：

- Wiki
- Decision
- 已审核知识

禁止优先引用：

- Raw
- 临时会议
- 未确认草稿

如果知识存在冲突：

必须明确指出：

- 哪个版本更新
- 哪个可信度更高
- 哪个属于客户特殊逻辑

---

# Knowledge Confidence

可信度与审核字段属于上面 Frontmatter Rules 的"审核"组：

```yaml
confidence: high           # high|medium|low
reviewed_by: <审核人>      # 不要写死具体人名,由实际审核人填写
last_verified: <YYYY-MM-DD>
```

AI 必须优先引用高可信知识。

---

# Engineering Knowledge Priorities

优先级从高到低：

```text
Decision
>
StateMachine
>
Protocol
>
Requirement
>
Bug
>
Meeting Notes
>
Raw Chat
```

---

# What AI Must Avoid

禁止：

- 直接复制 Raw 内容
- 把临时方案当正式方案
- 混用不同客户规则
- 混用不同版本协议
- 无关联输出知识
- 输出无来源结论
- 忽略状态机影响
- 忽略硬件约束

---

# Long-term Goal

最终目标不是"聊天机器人"，而是：

- AI-native Engineering System
- 工程认知系统
- 可持续积累的研发知识体系
- AI辅助研发决策平台

核心目标：

- 减少返工
- 减少沟通损耗
- 提升需求一致性
- 固化工程经验
- 降低新人学习成本
- 建立长期工程记忆