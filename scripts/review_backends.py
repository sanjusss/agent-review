"""命令行后端及所属进程组的执行与回收。"""
import codecs
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time

BACKENDS = ('opencode', 'codex', 'kimi', 'cursor', 'agycli', 'devin')


def command(backend, model_id, prompt, work_dir, final_path, seconds, summary=False):
    env = os.environ.copy()
    if backend == 'codex':
        args = ['codex', 'exec', '--model', model_id, '-c', 'model_reasoning_effort="high"', '--skip-git-repo-check', '--ephemeral', '-s', 'read-only', '-C', work_dir, '-o', str(final_path), '--json', prompt]
        native = 'codex'
    elif backend == 'kimi':
        args = ['kimi', '--model', model_id, '--output-format', 'stream-json', '--prompt', prompt]
        native = 'kimi'
    elif backend == 'cursor':
        args = ['cursor-agent', '-p', '--trust', '--force', '--workspace', work_dir, '--model', model_id, '--output-format', 'stream-json', prompt]
        native = 'cursor'
    elif backend == 'agycli':
        executable = shutil.which('agy') or str(Path.home() / '.local/bin/agy')
        args = [executable, '-p', prompt, '--mode', 'plan', '--dangerously-skip-permissions', '--output-format', 'text', '--model', model_id, '--print-timeout', f'{max(1, math.ceil(seconds))}s']
        native = 'text'
    elif backend == 'devin':
        args = ['devin', '--model', model_id, '--permission-mode', 'dangerous',
                '--respect-workspace-trust', 'false', '-p', prompt]
        native = 'text'
    else:
        role = 'summarizer' if summary else 'code-reviewer'
        config = {'agent': {role: {'description': '代码审核' if not summary else '审核结果汇总', 'mode': 'primary', 'temperature': 0.2, 'permission': {'*': 'allow', 'edit': 'deny'}}}}
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, ensure_ascii=False)
        env.pop('OPENCODE_SERVER_PASSWORD', None)
        args = ['opencode', 'run', '--standalone', '--model', model_id, '--agent', role, '--title', 'agent-review-summary' if summary else 'agent-review', '--format', 'json', prompt]
        native = 'opencode'
    return args, env, native


class TextEvents:
    """原始日志保持不变，仅从已完成的助手消息中提取正式文本。"""
    def __init__(self, native, receive):
        self.native = native
        self.receive = receive
        self.buffer = ''

    def feed(self, text, final=False):
        if self.native == 'text':
            self.receive(text)
            return
        self.buffer += text
        while '\n' in self.buffer or (final and self.buffer):
            if '\n' in self.buffer:
                line, self.buffer = self.buffer.split('\n', 1)
            else:
                line, self.buffer = self.buffer, ''
            try:
                event = json.loads(line)
            except ValueError:
                # 某些兼容 CLI 返回普通正文；只接收显式协议记录，其他文字不会被判完成。
                self.receive(line + '\n')
                continue
            if not isinstance(event, dict):
                continue
            if self.native == 'codex':
                item = event.get('item', {})
                if event.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
                    self.receive(item['text'] + '\n')
            elif self.native == 'kimi':
                if event.get('role') == 'assistant' and isinstance(event.get('content'), str):
                    self.receive(event['content'] + '\n')
            elif self.native == 'cursor':
                message = event.get('message', {})
                if event.get('type') == 'assistant' and isinstance(message, dict):
                    for part in message.get('content', []):
                        if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                            self.receive(part['text'] + '\n')
            elif self.native == 'opencode':
                if event.get('type') == 'text' and isinstance(event.get('part'), dict) and isinstance(event['part'].get('text'), str):
                    self.receive(event['part']['text'] + '\n')


def stop_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def execute(args, env, cwd, output_dir, seconds, stop_event, receive, native, aggregate=None):
    """预算包含启动与等待；超时后只清理本次创建的进程组。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    start = time.monotonic()
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    events = TextEvents(native, receive)
    rc, reason = None, ''
    with (output_dir / 'stdout.log').open('wb') as stdout, (output_dir / 'stderr.log').open('wb') as stderr:
        aggregate_out = (aggregate / 'stdout.log').open('ab') if aggregate else None
        aggregate_err = (aggregate / 'stderr.log').open('ab') if aggregate else None
        try:
            process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            stderr.write(str(exc).encode())
            if aggregate_err:
                aggregate_err.write(str(exc).encode())
            if aggregate_out:
                aggregate_out.close()
                aggregate_err.close()
            return 127, time.monotonic() - start, str(exc)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        terminating = None
        try:
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if terminating is None and (stop_event.is_set() or now >= deadline):
                    rc = 130 if stop_event.is_set() else 124
                    reason = '运行被中断' if rc == 130 else '审核或汇总时间已用完'
                    stop_group(process, signal.SIGTERM)
                    terminating = now
                if terminating is not None and now - terminating >= 30:
                    stop_group(process, signal.SIGKILL)
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    if key.data == 'stdout':
                        stdout.write(chunk)
                        stdout.flush()
                        if aggregate_out:
                            aggregate_out.write(chunk)
                            aggregate_out.flush()
                        events.feed(decoder.decode(chunk))
                    else:
                        stderr.write(chunk)
                        stderr.flush()
                        if aggregate_err:
                            aggregate_err.write(chunk)
                            aggregate_err.flush()
            events.feed(decoder.decode(b'', final=True), final=True)
            process.wait()
            return process.returncode if rc is None else rc, time.monotonic() - start, reason
        finally:
            selector.close()
            if process.poll() is None:
                stop_group(process, signal.SIGKILL)
                process.wait()
            if terminating is not None:
                # 主进程可能先退出，仍须清理同组中忽略 `SIGTERM` 的子进程。
                stop_group(process, signal.SIGKILL)
            if aggregate_out:
                aggregate_out.close()
                aggregate_err.close()
