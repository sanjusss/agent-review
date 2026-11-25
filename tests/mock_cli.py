#!/usr/bin/env python3
"""以受控输出模拟模型命令，不读取项目配置或连接外部服务。

角色和模型短名称通过环境变量传入，模拟程序据此选取规则并记录调用。
"""

import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

REVIEW_BODY = """## 审核结论

### 1. [一般] 空值导致请求失败

触发条件：调用方传入空值。

实际影响：请求失败，接口返回 500。

涉及位置：`src/a.py` 第 1-2 行。
"""

SUMMARY_BODY = """## 汇总结论

合并后的结论：空值处理需要补充校验，位置在 `src/a.py` 第 1-2 行。
"""


def emit_stdout(text):
    if Path(sys.argv[0]).name == "codex" and "--json" in sys.argv:
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                         ensure_ascii=False), flush=True)
    elif Path(sys.argv[0]).name == "opencode" and "--format" in sys.argv and sys.argv[sys.argv.index("--format") + 1] == "json":
        print(json.dumps({"type": "text", "part": {"type": "text", "text": text}}, ensure_ascii=False), flush=True)
    elif Path(sys.argv[0]).name == "kimi" and "stream-json" in sys.argv:
        print(json.dumps({"role": "assistant", "content": text}, ensure_ascii=False), flush=True)
    elif Path(sys.argv[0]).name == "cursor-agent" and "stream-json" in sys.argv:
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                         ensure_ascii=False), flush=True)
    else:
        print(text, flush=True)


def write_result(result_file, body):
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(body, encoding="utf-8")


def record(role, model, prompt, result_file, prompt_file=None):
    log = Path(os.environ["MOCK_LOG"])
    with log.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        rows = [json.loads(line) for line in stream if line.strip()]
        keys = [f"{role}:{model}", role, "default"]
        rules = json.loads(Path(os.environ["MOCK_CONTROL"]).read_text(encoding="utf-8")).get("rules", {})
        key = next((candidate for candidate in keys if candidate in rules), "default")
        count = sum(row["rule"] == key for row in rows)
        rule = rules.get(key, {})
        if isinstance(rule, list):
            rule = rule[min(count, len(rule) - 1)]
        row = {"request": {"kind": role, "model": model, "prompt": prompt}, "rule": key, "argv": sys.argv,
               "pid": os.getpid(), "time": time.monotonic(), "cwd": os.getcwd(),
               "opencode_config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
               "server_password_set": "OPENCODE_SERVER_PASSWORD" in os.environ,
               "prompt_file": prompt_file, "result_file": str(result_file)}
        stream.seek(0, 2)
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
    return rule


def body_for(role, rule):
    body = REVIEW_BODY if role == "review" else SUMMARY_BODY
    if rule.get("body_size"):
        body += "x" * rule["body_size"]
    return body


def resolve_result_file(prompt, allowed_root):
    match = re.search(r"^本次结果文件（唯一可写路径）：(.+)$", prompt, re.MULTILINE)
    if match is None:
        return None
    path = Path(match.group(1).strip()).resolve()
    if not path.is_relative_to(allowed_root):
        raise RuntimeError("模拟进程只允许写入本次测试目录内的结果文件")
    return path


def main():
    role = os.environ.get("AGENT_REVIEW_ROLE", "review")
    model = os.environ.get("AGENT_REVIEW_MODEL", "")
    allowed_root = Path(os.environ["MOCK_CONTROL"]).resolve().parent
    prompt = next((arg for arg in sys.argv[1:] if "审核内容：" in arg or "各模型的审核结论：" in arg), None)
    prompt_file = None
    if prompt is None:
        reference = next((arg for arg in sys.argv[1:] if arg.startswith("请读取文件 ")), None)
        if reference is not None:
            prompt_file, _ = json.JSONDecoder().raw_decode(reference[len("请读取文件 "):])
            path = Path(prompt_file).resolve()
            if not path.is_relative_to(allowed_root):
                raise RuntimeError("模拟进程只允许读取本次测试目录内的提示文件")
            prompt = path.read_text(encoding="utf-8")
    if prompt is None:
        print("missing prompt", file=sys.stderr)
        return 91
    result_file = resolve_result_file(prompt, allowed_root)
    if result_file is None:
        print("missing result_file", file=sys.stderr)
        return 92
    rule = record(role, model, prompt, result_file, prompt_file)
    mode = rule.get("mode", "complete")
    if rule.get("delay"):
        time.sleep(rule["delay"])
    if mode == "fail":
        print("模拟服务异常", file=sys.stderr, flush=True)
        return 7
    if mode == "empty":
        return 0
    if mode == "empty_file":
        write_result(result_file, "   \n")
        return 0
    if mode == "stdout_only":
        emit_stdout(body_for(role, rule))
        return 0
    if mode == "sleep":
        time.sleep(60)
        return 0
    if mode == "descendant":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])

        def stop(signum, _frame):
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=2)
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, stop)
        Path(os.environ["MOCK_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
        time.sleep(90)
        return 0
    body = body_for(role, rule)
    if mode == "partial_then_sleep":
        # 先把已确认的部分结论写入结果文件，再挂起，验证超时后保留阶段性文件结果。
        write_result(result_file, body)
        emit_stdout("正在继续检查其余模块")
        time.sleep(60)
        return 0
    if mode == "revise":
        emit_stdout("正在检查调用方，怀疑存在伪问题 DELETED_FALSE_POSITIVE")
        write_result(result_file, "### [严重] DELETED_FALSE_POSITIVE\n\n初稿误报，待核对调用链。\n\n" + body)
        write_result(result_file, body)
        emit_stdout("已完成修订并定稿")
        return 0
    write_result(result_file, body)
    emit_stdout("已完成并写入结果文件")
    if mode == "write_then_fail":
        return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
