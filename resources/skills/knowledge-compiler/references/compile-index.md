# 编译索引

只在增量编译、重编译判定或更新 `AI/Index/compile_index.json` 时读取本文件。

## 目的

编译索引是内部追踪账本，不是知识本身。它负责回答：哪些来源在什么契约和模板下生成了哪些输出，来源或输出后来是否发生变化。

## 兼容性

先兼容读取现有 v1 结构：

```json
{
  "compiled_files": [
    {
      "source_file": "Raw/example.md",
      "source_hash": "sha256:...",
      "output_files": ["项目/Example/功能/需求/需求.md"],
      "compile_date": "2026-08-18",
      "status": "draft"
    }
  ]
}
```

不得仅为了升级 schema 而一次性重写所有旧记录。下次实际编译相关条目时再迁移，并在 diff 中说明。

## 推荐 v2 结构

```json
{
  "schema_version": 2,
  "compilations": [
    {
      "compilation_id": "project-feature-kind",
      "sources": [
        {
          "id": "Raw/example.md",
          "kind": "file",
          "hash": "sha256:...",
          "revision": null
        }
      ],
      "outputs": [
        {
          "path": "项目/Example/功能/需求/需求.md",
          "hash": "sha256:..."
        }
      ],
      "compiler_version": "2",
      "contract_hash": "sha256:...",
      "template_hash": "sha256:...",
      "compiled_at": "2026-08-18T12:00:00+08:00",
      "state": "draft"
    }
  ]
}
```

`kind` 可取 `file`、`directory`、`repository` 或 `url`。源码来源的 `revision` 记录 commit；普通文件可以为 `null`。一个编译单元允许多个来源和多个输出。

`contract_hash` 应覆盖本次实际读取的关键规范内容，至少包含 Frontmatter、命名和双链规范；`template_hash` 对应实际使用的库内模板。hash 组合方式必须稳定，并在实现报告中说明。

## 重编译判定

以下任一条件成立都需要检查：

- 来源 hash 或 revision 改变
- 编译器主要规则版本改变
- 权威规范或使用的模板 hash 改变
- 输出文件缺失
- 输出当前 hash 与索引不同，说明发生了人工修改或其他流程修改
- 来源删除、移动或不可访问

输出漂移不能直接触发覆盖。先读取人工修改，进行三方比较：旧索引版本、当前输出、新来源编译结果，然后提出最小合并 diff。

来源没有变化不代表知识仍然有效。客户、版本或上游权威文档发生变化时，也要重新执行作用域和冲突检查。

## 状态

推荐状态：

- `draft`：已生成待审核输出
- `reviewed`：输出已人工审核；仅在人类审核事实可验证时更新
- `needs_recompile`：来源、规范或模板改变
- `output_drift`：输出被其他流程修改，需要合并判断
- `source_missing`：来源删除或不可访问，保留历史记录
- `conflict`：存在未裁决冲突

索引状态不替代文档 Frontmatter，也不得据此自动把文档改为 `active`。

## 原子更新与校验

更新前解析现有 JSON；更新后再次解析并检查：

- 来源 ID 唯一且可追溯
- hash 使用带算法前缀的小写十六进制
- 输出路径是知识库相对路径且真实存在
- 没有因路径分隔符不同产生重复记录
- 未丢失与本次编译无关的历史条目

如果索引损坏，停止写入并报告；不得用空索引覆盖。
