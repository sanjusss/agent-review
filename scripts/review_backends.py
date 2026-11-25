"""命令行后端及所属进程组的执行与回收。"""
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time

BACKENDS = ('opencode', 'codex', 'kimi', 'cursor', 'agycli', 'devin')


def effort_error(backend, model_id, effort):
    """检查思考强度配置；可用或未配置时返回空字符串，否则返回错误说明。"""
    if not effort:
        return ''
    if not re.fullmatch(r'[A-Za-z0-9._-]+', effort):
        return f'effort 只能包含字母、数字、`.`、`_`、`-`：{effort}'
    if backend in ('kimi', 'devin'):
        return f'后端 {backend} 不支持单独设置 effort，请改用模型 ID 或该后端自身的配置'
    if backend == 'opencode' and '#' in model_id:
        return '模型 ID 已包含 `#` 变体，不能同时设置 effort'
    if backend == 'cursor' and 'effort=' in model_id:
        return '模型 ID 已包含 effort 参数，不能同时设置 effort'
    return ''


def command(backend, model_id, prompt, work_dir, result_dir, seconds, summary=False, effort='', input_dir=None):
    env = os.environ.copy()
    result_dir = str(Path(result_dir).resolve())
    extra_dirs = [result_dir] + ([str(Path(input_dir).resolve())] if input_dir else [])
    if backend == 'codex':
        profile = (f'permissions.agent_review={{ extends = ":read-only", '
                   f'filesystem = {{ {json.dumps(result_dir)} = "write" }} }}')
        args = ['codex', 'exec', '--model', model_id,
                '-c', f'model_reasoning_effort="{effort or "high"}"',
                '-c', 'default_permissions="agent_review"',
                '-c', profile,
                '--skip-git-repo-check', '--ephemeral',
                '-C', work_dir, '--add-dir', result_dir, '--json', prompt]
    elif backend == 'kimi':
        args = ['kimi', '--model', model_id, '--output-format', 'stream-json']
        for directory in extra_dirs:
            args += ['--add-dir', directory]
        args += ['--prompt', prompt]
    elif backend == 'cursor':
        if effort:
            model_id = model_id[:-1] + f',effort={effort}]' if model_id.endswith(']') else f'{model_id}[effort={effort}]'
        args = ['cursor-agent', '-p', '--trust', '--force', '--workspace', work_dir]
        for directory in extra_dirs:
            args += ['--add-dir', directory]
        args += ['--model', model_id, '--output-format', 'stream-json', prompt]
    elif backend == 'agycli':
        executable = shutil.which('agy') or str(Path.home() / '.local/bin/agy')
        args = [executable, '-p', prompt, '--mode', 'accept-edits', '--dangerously-skip-permissions']
        for directory in extra_dirs:
            args += ['--add-dir', directory]
        args += ['--output-format', 'text', '--model', model_id]
        if effort:
            args += ['--effort', effort]
        args += ['--print-timeout', f'{max(1, math.ceil(seconds))}s']
    elif backend == 'devin':
        args = ['devin', '--model', model_id, '--permission-mode', 'dangerous',
                '--respect-workspace-trust', 'false', '-p', prompt]
    else:
        role = 'summarizer' if summary else 'code-reviewer'
        config = {'agent': {role: {'description': '代码审核' if not summary else '审核结果汇总',
                                   'mode': 'primary', 'temperature': 0.2,
                                   'permission': {'*': 'allow', 'edit': {'*': 'deny', f'{result_dir}/*': 'allow'}}}}}
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, ensure_ascii=False)
        env.pop('OPENCODE_SERVER_PASSWORD', None)
        args = ['opencode', 'run', '--standalone', '--model', f'{model_id}#{effort}' if effort else model_id,
                '--agent', role, '--title', 'agent-review-summary' if summary else 'agent-review',
                '--format', 'json', prompt]
    return args, env


def stop_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def execute(args, env, cwd, output_dir, seconds, stop_event, aggregate=None):
    """预算包含启动与等待；超时后只清理本次创建的进程组。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    start = time.monotonic()
    rc, reason = None, ''
    with (output_dir / 'stdout.log').open('wb') as stdout, (output_dir / 'stderr.log').open('wb') as stderr:
        aggregate_out = (aggregate / 'stdout.log').open('ab') if aggregate else None
        aggregate_err = (aggregate / 'stderr.log').open('ab') if aggregate else None
        try:
            process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
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
                    else:
                        stderr.write(chunk)
                        stderr.flush()
                        if aggregate_err:
                            aggregate_err.write(chunk)
                            aggregate_err.flush()
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
