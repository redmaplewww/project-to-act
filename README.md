# Project to Act

`project-to-act` 是面向 AI 编程代理的中文项目治理 Skill。它维护唯一项目事实源，让 AI 在实施前核对路线、工作中同步状态、完成前检查验收证据，并通过条件读取控制上下文消耗。

## 核心能力

- 先发现再配置：先检查已有账本，不因 Skill 被加载就自动写文件。
- 唯一事实源：支持内置五文档和采用现有项目账本两种模式，拒绝并行维护两套状态。
- 紧凑状态视图：五份主文档只保留当前状态、少量近期历史和可追溯引用，历史正文按月归档。
- 路线与验收保护：范围冲突先确认，没有新鲜证据不声明完成。
- 显式功能对账：`--reconcile-features` 只对账配置过的源文件，不做仓库级自动搜索。
- 数据安全：项目文档按不可信数据处理，证据不保存密钥、完整个人信息或原始顾客对话。
- 生命周期：支持 dry-run、旧版迁移、结构验证、审计、整理、恢复和并发安全创建。

## 两种模式

### Managed

适用于没有项目账本的新项目：

```text
.project-to-act/
├── PROJECT_CONFIG.json
├── PROJECT_OVERVIEW.md
├── PROJECT_PROGRESS.md
├── PROJECT_VERSIONS.md
├── PROJECT_FEATURES.md
└── PROJECT_ACCEPTANCE.md
```

### External ledger

适用于已有 `PROJECT_LEDGER.md`、`AGENT_PROJECT.md` 或 `docs/project-ledger.md` 的项目。`.project-to-act/` 只保存一个配置文件，指向原账本，不复制或分叉项目状态。

## 安装

使用 Codex 的 skill 安装器：

```text
从 GitHub 仓库 redmaplewww/project-to-act 安装 project-to-act/ skill。
```

或将仓库中的 `project-to-act/` 内容安装到：

```text
$CODEX_HOME/skills/project-to-act/
```

未设置 `CODEX_HOME` 时使用 `~/.codex/skills/project-to-act/`。更新安装时先备份旧目录；不要把仓库根级 README 和测试复制进运行时 skill 目录。

## 使用

显式调用：

```text
使用 $project-to-act 检查当前项目的既有账本，并安全采用或维护唯一事实源。
```

下面命令中的 `<skill>` 指已安装的 `project-to-act` 目录，`<project>` 必须是明确的项目根目录。

### 只读检查

```powershell
python <skill>/scripts/init_project_management.py --project-root <project> --check
```

### 新项目初始化

```powershell
python <skill>/scripts/init_project_management.py --project-root <project> --dry-run
python <skill>/scripts/init_project_management.py --project-root <project>
```

如果发现已有账本，默认初始化会失败，防止产生第二事实源。

### 采用已有账本

```powershell
python <skill>/scripts/init_project_management.py --project-root <project> --adopt-ledger docs/project-ledger.md --dry-run
python <skill>/scripts/init_project_management.py --project-root <project> --adopt-ledger docs/project-ledger.md
```

### 迁移旧版五文档目录

```powershell
python <skill>/scripts/init_project_management.py --project-root <project> --migrate --dry-run
python <skill>/scripts/init_project_management.py --project-root <project> --migrate
```

迁移只补 `PROJECT_CONFIG.json` 和缺失模板，不覆盖已有管理内容。

如果旧路径仍被 README 或工具引用，可把它改成只含下列首行标记和跳转说明的兼容页；检查器会验证目标位于 `.project-to-act/` 后忽略该旧路径，不再把它判定为第二账本：

```text
<!-- project-to-act-redirect: .project-to-act/PROJECT_OVERVIEW.md -->
```

### 验证

```powershell
python <skill>/scripts/init_project_management.py --project-root <project> --validate
python <skill>/scripts/init_project_management.py --project-root <project> --audit
python <skill>/scripts/init_project_management.py --project-root <project> --compact --dry-run
```

所有 CLI 成功结果输出 UTF-8 JSON，失败返回非零退出码。

## 读取规则

Managed 模式每次只从总览开始，再按任务读取进度、功能、版本或验收文件。External-ledger 模式先搜索规范账本中的目标和当前状态，再按任务搜索相关标题；仅在一致性审计时读取全文。

## 开发验证

初始化脚本只依赖 Python 3.10+ 标准库。

```powershell
$env:PYTHONIOENCODING='utf-8'
python -m unittest discover -s tests -v
python -X utf8 $HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py ./project-to-act
```

## 仓库结构

```text
.
├── README.md
├── docs/
├── project-to-act/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/ledger-contract.md
│   ├── scripts/init_project_management.py
│   └── assets/templates/
└── tests/
```

## 许可证

当前仓库尚未指定开源许可证。公开仓库可供查看和评估；正式再分发前请补充许可证。
