---
reviewed_by: AI-draft
modified: 2026-09-24 17:31:40
status: draft
tags:
  - 索引
  - 变更校验
---

# 变更校验工具

看说明进入 [文档目录](%E6%96%87%E6%A1%A3/_INDEX.md)；修改程序进入 `src/`；运行测试进入 `tests/`。

```text
change-check/
├─ 文档/                 设计方案、使用指南、规则清单和验证记录
├─ src/                  程序源码及运行所需的审查提示词
│  ├─ main.py            Python 命令入口
│  ├─ changecheck/        公共核心、审查、钩子与 modules/ 检查模块
│  └─ prompts/           AI 审查提示词
├─ tests/                自动测试代码
├─ packaging/            打包程序及本机来源映射（本机映射不发布）
├─ resources/            独立包生成的规则副本，不手工维护
├─ dist/                 本机生成的发布目录与压缩包，不提交
├─ README.md             公开仓库的快速入口
├─ install.ps1           安装入口
├─ change-check.ps1          日常使用入口
├─ requirements.txt      Python 依赖清单
├─ .gitignore            Git 忽略配置
├─ .venv/                本机生成的运行环境，不是业务源码
└─ _INDEX.md             本页
```

首次使用阅读 [使用指南](%E6%96%87%E6%A1%A3/%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97.md)，运行根目录的 `install.ps1`。
对外分发阅读 [发布指南](%E6%96%87%E6%A1%A3/%E5%8F%91%E5%B8%83%E6%8C%87%E5%8D%97.md)；GitHub 首页使用 [README](README.md)。
统一使用 change-check 名称和入口；需要直接运行 Python 时，入口为 `src/main.py`。

返回 个人工具索引。

当前阶段：独立命令行工具、安装向导和钩子适配已实现；真实知识库部署由安装向导检测环境后交互选择。
