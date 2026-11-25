#!/usr/bin/env python3
"""多模型代码审核：所有模型审核同一范围，汇总为 Markdown 报告。

模型在当次尝试的专属结果文件中写入并修订自由正文，脚本不解析结构化结果；
“完成”只表示进程正常退出且专属结果文件非空，不表示覆盖了全部范围。
"""
import argparse
import configparser
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
import uuid

from review_backends import BACKENDS, command, effort_error, execute

ROOT = Path(__file__).resolve().parent.parent

REVIEW_RULES = '''你是一名专业的代码审查者。所有模型独立审核同一完整范围。
允许读取文件、搜索代码和检查依赖；严禁修改、创建、删除被审核项目中的任何文件，禁止编译代码，禁止运行被审核程序。
本次唯一允许写入和编辑的文件是下方的“本次结果文件”；禁止读写其他临时目录，禁止读取其他审核模型的结论。
审核描述列的是入口，必须继续读取判断所需的调用方和依赖。不要自行把审核交给新的会话或子代理。
以下五类问题不输出：过度防御（针对不可能出现的输入或状态）；设计范围内的预期行为；
无实际影响的风格偏好；受框架、第三方库或历史接口约束而无法修复；现有调用链中无法触发。
每条发现必须说明触发条件、实际影响、涉及文件路径和具体行号。代码标识符使用反引号。'''

REVIEW_OUTPUT = '''审核结论用 Markdown 写入本次结果文件，不要输出 JSON，不要在结果文件中写“审核中”等进度占位文字：
1. 边审核边记录：每核实确认一条有效问题，立即按格式写入本次结果文件。
2. 随核实随修正：继续查调用链和依赖时，若发现已写入的问题不成立或重复，立即编辑结果文件删除；若严重程度、触发条件或行号有误，立即编辑修正。
3. 结束前通读复核：全部范围检查完毕后，若没有任何有效问题，在本次结果文件中写“未发现问题”，并说明检查了哪些文件、核对过哪些调用关系。若有问题，重新读取本次结果文件，剔除上述五类不输出条目。确认所有问题按严重程度（严重/一般/建议）排列，且每条写清标题、严重程度、触发条件、实际影响、涉及文件与行号。
确认本次结果文件保存定稿后即可结束，回复正文只需简短说明已完成，不必重复输出报告全文。'''

SUMMARY_RULES = '''你是代码审核汇总者。只合并、整理相同问题，不核实问题，不读取源码，不添加新问题，不删除任何原始发现。
严禁修改被审核项目文件、编译代码或运行被审核程序；本次唯一允许写入和编辑的文件是下方的“本次结果文件”。
合并后的每条发现必须保留完整触发条件、影响、文件行号，并注明提出该问题的模型名称。
有的模型可能中途超时，正文不完整，按已有内容处理并在报告里说明。
请将合并去重后的 Markdown 报告正文写入本次结果文件，不要输出 JSON，禁止写入运行目录或状态表。

写入初稿后，必须重新读取本次结果文件逐项自检，发现遗漏或偏差直接编辑本次结果文件修正后再结束：
1. 输入中每个有正文的模型是否都已处理；未完成模型是否已在报告中说明
2. 合并后的每条发现是否保留触发条件、影响、文件行号，并注明提出该问题的模型名称
3. 确认未添加新问题、未删除任何原始发现、未自行核实或改判
4. 相同问题已合并，不同问题仍为独立条目
确认本次结果文件修订完毕后，回复正文只需简短说明已完成，不必重复输出报告全文。'''


class ReviewError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def read_json(path):
    with Path(path).open(encoding='utf-8') as handle:
        return json.load(handle)


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, json_text(value) + '\n')


def duration(value):
    match = re.fullmatch(r'([0-9]+)([smh]?)', value)
    if not match:
        raise argparse.ArgumentTypeError('时间必须为正整数，可使用 `s`、`m`、`h`')
    try:
        number = int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]]
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError('时间值过大') from None
    if not 0 < number <= 2147483647:
        raise argparse.ArgumentTypeError('时间必须在 1 至 2147483647 秒之间')
    return number


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f'错误：{message}\n')


def parser():
    p = Parser(prog='agent-review.sh', description='所有模型独立审核相同范围，默认保存到独立临时目录。')
    p.add_argument('--review-models', help='审核模型短名称，空格分隔；默认全部')
    p.add_argument('--summarizer-model', help='汇总模型短名称')
    p.add_argument('--config', default=os.environ.get('AGENT_REVIEW_CONFIG', str(ROOT / 'models.conf')),
                   help='仅用户明确授权时指定配置；默认使用同目录 `models.conf`')
    p.add_argument('--list-models', action='store_true', help='列出可用模型及默认汇总模型后退出')
    p.add_argument('--review-timeout', type=duration, default=2000, help='每模型审核预算，默认 2000 秒，最多尝试两次')
    p.add_argument('--summary-timeout', type=duration, default=1200, help='每次汇总共用预算，默认 1200 秒，最多 3 次')
    p.add_argument('content', nargs='?', help='审核内容')
    p.add_argument('report', nargs='?', help='报告路径；默认本次临时目录/report.md')
    return p


def load_config(path):
    cfg = configparser.ConfigParser(interpolation=None, strict=False, comment_prefixes=('#',))
    with Path(path).expanduser().open(encoding='utf-8-sig') as handle:
        cfg.read_file(handle)
    models = {}
    for name in cfg.sections():
        if name == 'summarizer' or not cfg.get(name, 'model', fallback='').strip():
            continue
        backend = cfg.get(name, 'backend', fallback='opencode').strip()
        require(backend in BACKENDS, f'模型 {name} 的后端无效：{backend}')
        model_id = cfg.get(name, 'model').strip()
        effort = cfg.get(name, 'effort', fallback='').strip()
        error = effort_error(backend, model_id, effort)
        require(not error, f'模型 {name} 的配置无效：{error}')
        models[name] = dict(name=name, model_id=model_id, backend=backend, effort=effort)
    require(models, '配置文件未定义模型')
    default = cfg.get('summarizer', 'model', fallback=next(iter(models))).strip()
    require(default in models, '默认汇总模型未定义')
    return models, default


def normalize_report(path):
    """解析父目录的相对路径和符号链接，不跟随最终文件本身的符号链接。"""
    value = Path(path).expanduser().absolute()
    return value.parent.resolve() / value.name


class ReviewRun:
    def __init__(self, args, models, default):
        self.args = args
        self.stop = threading.Event()
        self.mutex = threading.RLock()
        self.old_handlers = {}
        require(args.content and args.content.strip(), '缺少审核内容')
        names = list(models)
        if args.review_models is not None:
            names = []
            for name in args.review_models.split():
                if name not in models:
                    print(f'警告：忽略未知模型 {name}', file=sys.stderr)
                elif name not in names:
                    names.append(name)
            require(names, '`--review-models` 未匹配到有效模型')
        summarizer = args.summarizer_model or default
        require(summarizer in models, '汇总模型未定义')
        parent = Path(os.environ.get('TMPDIR') or '/tmp').expanduser()
        self.directory = Path(tempfile.mkdtemp(prefix='agent-review-', dir=parent)).resolve()
        work_dir = str(Path.cwd().resolve())
        report = Path(args.report).expanduser().absolute() if args.report else self.directory / 'report.md'
        require(not report.is_dir(), '报告路径不能是目录')
        # 在开始之前创建父目录；报告由原子替换写入，不交给模型写入。
        report.parent.mkdir(parents=True, exist_ok=True)
        report = normalize_report(report)
        self.state = dict(version=2, run_id=uuid.uuid4().hex, run_dir=str(self.directory), work_dir=work_dir,
                          content=args.content, report_path=str(report), report_current=False,
                          status='running', created_at=time.time(),
                          models={f'm{i}': dict(models[name], status='pending', attempts=[], elapsed=0.0,
                                                error='', answer='') for i, name in enumerate(names, 1)},
                          summarizer=models[summarizer], history=[], summary_attempts=[])
        self.state['history'].append(dict(started_at=time.time(), review_timeout=args.review_timeout,
                                          summary_timeout=args.summary_timeout))
        self.log(f'临时目录：{self.directory}')
        self.log(f"报告路径：{self.state['report_path']}")
        self.save()

    def save(self):
        with self.mutex:
            write_json(self.directory / 'run.json', self.state)

    def log(self, message):
        with self.mutex:
            print(message, flush=True)
            with (self.directory / 'run.log').open('a', encoding='utf-8') as handle:
                handle.write(time.strftime('%Y-%m-%d %H:%M:%S ') + message + '\n')

    def review_prompt(self, result_file):
        return (REVIEW_RULES + '\n' + REVIEW_OUTPUT
                + f"\n\n审核内容：{self.state['content']}"
                + f"\n被审核项目目录（只读）：{self.state['work_dir']}"
                + f"\n本次结果文件（唯一可写路径）：{result_file}\n")

    def summary_prompt(self, answers, result_file):
        blocks = []
        for item in answers:
            state = '完成' if item['status'] == 'complete' else '未完成，正文可能中断'
            blocks.append(f"### 模型 {item['model']}（{state}）\n{item['text']}")
        return (SUMMARY_RULES
                + f"\n\n审核范围：{self.state['content']}"
                + f"\n被审核项目目录（只读）：{self.state['work_dir']}"
                + f"\n本次结果文件（唯一可写路径）：{result_file}"
                + '\n\n以下是各模型的审核结论：\n\n' + '\n\n'.join(blocks) + '\n')

    def run_cli(self, model, build_prompt, output_dir, deadline, summary=False, aggregate=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        result_dir = output_dir / 'result'
        result_dir.mkdir(parents=True, exist_ok=True)
        result_file = result_dir / 'result.md'
        input_dir = output_dir / 'input'
        input_dir.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(result_file)
        prompt_file = input_dir / 'prompt.txt'
        prompt_file.write_text(prompt, encoding='utf-8')
        # 避免超长命令行；模型读取同一份完整提示。
        use_prompt_file = len(prompt.encode()) > 48000
        cli_prompt = prompt if not use_prompt_file else \
            f'请读取文件 {json.dumps(str(prompt_file), ensure_ascii=False)}，严格执行其中的全部要求。该文件是本次完整提示词。'
        seconds = deadline - time.monotonic()
        if seconds <= 0 or self.stop.is_set():
            return ((130 if self.stop.is_set() else 124), 0.0,
                    ('运行被中断' if self.stop.is_set() else '时间预算已用完'), '', False)
        args, env = command(model['backend'], model['model_id'], cli_prompt, self.state['work_dir'],
                            result_dir, seconds, summary, effort=model.get('effort', ''),
                            input_dir=input_dir if use_prompt_file else None)
        # 只用于排障和测试识别，模型命令不依赖这两个变量。
        env['AGENT_REVIEW_MODEL'] = model['name']
        env['AGENT_REVIEW_ROLE'] = 'summary' if summary else 'review'
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            return 124, 0.0, '时间预算已用完', '', False
        rc, elapsed, reason = execute(args, env, self.state['work_dir'], output_dir, seconds, self.stop, aggregate)
        exists = result_file.is_file()
        text = result_file.read_text(encoding='utf-8', errors='replace').strip() if exists else ''
        return rc, elapsed, reason, text, exists

    def reviewer(self, key):
        model = self.state['models'][key]
        model_dir = self.directory / 'models' / key
        model_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        deadline = start + self.args.review_timeout
        with self.mutex:
            model['status'] = 'running'
            model['error'] = ''
            self.save()
        try:
            answered = False
            best_chars = 0
            for attempt in (1, 2):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stop.is_set():
                    break
                attempt_dir = model_dir / f'attempt-{attempt}'
                self.log(f"启动审核：{model['name']}，第 {attempt}/2 次，剩余 {math.ceil(remaining)} 秒")
                rc, elapsed, reason, text, exists = self.run_cli(
                    model, self.review_prompt, attempt_dir, deadline, aggregate=model_dir)
                valid = rc == 0 and bool(text)
                # 成功的正文一定覆盖；失败时只在比上次更长时覆盖，避免重试把更完整的结果冲掉。
                if text and (valid or len(text) > best_chars):
                    atomic_write(model_dir / 'answer.md', text + '\n')
                    model['answer'] = str(model_dir / 'answer.md')
                    best_chars = len(text)
                with self.mutex:
                    model['attempts'].append(dict(attempt=attempt, rc=rc, elapsed=elapsed,
                                                  chars=len(text), ok=valid, path=str(attempt_dir)))
                    if valid:
                        model['error'] = ''
                    elif reason:
                        model['error'] = reason
                    elif not exists:
                        model['error'] = f'退出码 {rc}，未生成结果文件' if rc else '未生成结果文件'
                    elif not text:
                        model['error'] = f'退出码 {rc}，结果文件为空' if rc else '结果文件为空'
                    else:
                        model['error'] = f'退出码 {rc}，正文可能不完整'
                    self.save()
                if valid:
                    answered = True
                    break
                if rc in (124, 130) or self.stop.is_set():
                    break
            with self.mutex:
                if answered:
                    model['status'] = 'complete'
                elif model['answer']:
                    model['status'] = 'partial'
                else:
                    model['status'] = 'failed'
                    if not model['error']:
                        model['error'] = '运行被中断' if self.stop.is_set() else '没有完成审核'
                self.save()
            self.log(f"审核结束：{model['name']}，{model['status']}")
        except Exception as exc:
            with self.mutex:
                model['status'] = 'partial' if model.get('answer') else 'failed'
                model['error'] = str(exc)
                self.save()
            self.log(f"审核模型异常：{model['name']}：{exc}")
        finally:
            with self.mutex:
                model['elapsed'] = time.monotonic() - start
                self.save()

    def model_answers(self):
        answers = []
        for model in self.state['models'].values():
            if not model.get('answer'):
                continue
            path = Path(model['answer'])
            if not path.is_file():
                continue
            text = path.read_text(encoding='utf-8', errors='replace').strip()
            if text:
                answers.append(dict(model=model['name'], status=model['status'], text=text))
        return answers

    def summarize(self, answers):
        summary_dir = self.directory / 'summary'
        summary_dir.mkdir(exist_ok=True)
        for attempt in range(1, 4):
            if self.stop.is_set():
                return None
            deadline = time.monotonic() + self.args.summary_timeout
            target = summary_dir / f'attempt-{attempt}'
            self.log(f'汇总第 {attempt}/3 次尝试')
            rc, elapsed, _reason, text, _exists = self.run_cli(
                self.state['summarizer'], lambda result_file: self.summary_prompt(answers, result_file),
                target, deadline, summary=True)
            ok = rc == 0 and bool(text)
            self.state['summary_attempts'].append(dict(attempt=attempt, rc=rc, elapsed=elapsed,
                                                       chars=len(text), complete=ok))
            self.save()
            if ok:
                atomic_write(summary_dir / 'report-body.md', text + '\n')
                return text
        return None

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.old_handlers[sig] = signal.signal(sig, lambda _sig, _frame: self.stop.set())
        try:
            with ThreadPoolExecutor(max_workers=len(self.state['models'])) as pool:
                list(pool.map(self.reviewer, list(self.state['models'])))
            if self.stop.is_set():
                self.state['status'] = 'interrupted'
                return 130
            answers = self.model_answers()
            completed = sum(model['status'] == 'complete' for model in self.state['models'].values())
            self.state['status'] = 'complete' if completed == len(self.state['models']) else (
                'partial' if completed else 'failed')
            self.save()
            # 一个模型都没有完整跑完时不制造新报告，原始输出留在运行目录。
            if not completed:
                self.log('没有模型完整完成；原始输出已保留。')
                return 1
            body = self.summarize(answers)
            if self.stop.is_set():
                self.state['status'] = 'interrupted'
                return 130
            if body is None:
                self.state['status'] = 'failed'
                self.log('汇总失败；原报告未替换，原始结果已保留。')
                return 1
            # 报告只有汇总正文；运行状态看标准输出和运行目录，不写进报告。
            atomic_write(self.state['report_path'], body.rstrip() + '\n')
            self.state['report_current'] = True
            self.log(f"审核状态：{self.state['status']}；报告：{self.state['report_path']}")
            return 0
        finally:
            self.state['history'][-1]['finished_at'] = time.time()
            self.state['history'][-1]['status'] = self.state['status']
            self.save()
            for sig, handler in self.old_handlers.items():
                signal.signal(sig, handler)


def main(argv=None):
    args = parser().parse_args(argv)
    run = None
    try:
        models, default = load_config(args.config)
        if args.list_models:
            print(f'{"name":<16} {"model_id":<36} {"backend":<10} effort')
            for model in models.values():
                print(f'{model["name"]:<16} {model["model_id"]:<36} {model["backend"]:<10} {model.get("effort") or "-"}')
            print(f'\ndefault_summarizer={default}')
            return 0
        run = ReviewRun(args, models, default)
        return run.run()
    except (ReviewError, OSError, ValueError, configparser.Error) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        if run is not None:
            run.state['status'] = 'failed'
            run.state['error'] = str(exc)
            run.save()
        return 1


if __name__ == '__main__':
    sys.exit(main())
