# agent-review

多模型代码审核技能：多个模型后端独立审核同一范围，再由一个汇总模型合并去重，生成 Markdown 报告。

本目录是一个供 AI 代理调用的技能（`SKILL.md`），仅在使用 `agent-review` 名称明确要求调用时启用；`agent-review.sh` 是执行入口，也可以脱离技能直接使用。

审核与汇总都通过各家的命令行程序完成。模型只输出自由正文，脚本不解析结构化结果；“完成”只表示进程正常退出且有正文，不代表覆盖了全部范围。

## 依赖

- Bash、Python 3（只用标准库）
- 按 `models.conf` 中各模型 `backend` 的值安装对应命令行程序：

| backend | 命令行程序 | 说明 |
|---|---|---|
| `opencode`（默认） | `opencode` | 以 `--format json` 事件流读取正文 |
| `codex` | `codex` | `codex exec`，只读沙箱 |
| `kimi` | `kimi` | `stream-json` 输出 |
| `cursor` | `cursor-agent` | `stream-json` 输出 |
| `agycli` | `agy` | 纯文本输出，`--print-timeout` 跟随剩余预算 |
| `devin` | `devin` | `-p` 非交互模式，纯文本输出 |

## 配置

```bash
cp models.conf.example models.conf
```

`models.conf` 每个小节定义一个模型，小节名即 `--review-models` 使用的短名称：

- `model`：模型 ID，原样传给后端 CLI 的 `--model` 参数。
- `backend`：上表中的后端名，省略时为 `opencode`。
- `[summarizer]` 小节：默认汇总模型，填其他小节的短名称。

## 用法

```bash
cd /path/to/被审核项目
bash /path/to/agent-review/agent-review.sh "审核最近一次提交涉及的登录逻辑"
```

- 第一个位置参数是审核内容（必填），第二个是报告路径（可选；省略时写入本次运行的独立临时目录 `report.md`）。
- `--review-models "短名称1 短名称2"`：选择审核模型，默认全部。
- `--summarizer-model 短名称`：指定汇总模型，默认取 `[summarizer]` 小节。
- `--review-timeout` / `--summary-timeout`：单次审核 / 汇总的时间预算，支持 `s`、`m`、`h` 后缀；默认 `2000s` / `1200s`。审核最多尝试 2 次，汇总最多 3 次。
- `--list-models`：列出可用模型与默认汇总模型后退出。
- `--config`：指定其他配置文件，默认使用技能同目录的 `models.conf`。

## 输出

每次运行创建独立临时目录 `${TMPDIR:-/tmp}/agent-review-*`，包含：

- `run.json`：运行状态、各模型每次尝试的退出码与正文字数。
- `run.log`：运行日志。
- `models/<编号>/answer.md`：各模型的审核正文；`attempt-N/` 下是当次尝试的原始 stdout/stderr。
- `summary/`：汇总尝试与最终报告正文。

报告文件只含汇总正文，不含运行信息。退出码：

| 退出码 | 状态 | 含义 |
|---|---|---|
| `0` | `complete` | 所有模型正常退出且有正文，汇总成功 |
| `0` | `partial` | 至少一个模型完成，其余失败、超时或无正文；汇总成功 |
| `1` | `failed` | 没有模型完整完成，或汇总失败 |
| `130` | `interrupted` | 运行被中断，已有结果保留 |

## 测试

```bash
cd /path/to/agent-review
python3 -m unittest tests.test_agent_review -v
```

所有模型命令由 `tests/mock_cli.py` 模拟，不需要真实后端和网络。
