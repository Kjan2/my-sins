"""Only Docker executes untrusted code. This module uses the Python standard library."""
import json
import logging
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import tempfile
import time
import uuid
from xml.etree import ElementTree

OUTPUT_LIMIT = 65536
LABEL = "department.contest.judge"
logger = logging.getLogger(__name__)


class InfrastructureError(RuntimeError):
    pass


def bounded_process(command, input_bytes=b"", timeout=5, output_limit=OUTPUT_LIMIT):
    """Bound both streams together; never buffer arbitrary student output in RAM."""
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    streams = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending = memoryview(input_bytes)
    start = time.monotonic()
    limited = None
    try:
        for pipe, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            streams.register(pipe, selectors.EVENT_READ, name)
        if pending:
            os.set_blocking(proc.stdin.fileno(), False)
            streams.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            proc.stdin.close()
        total = 0
        while streams.get_map():
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                limited = "time"
                break
            for key, _ in streams.select(min(.05, remaining)):
                if key.data == "stdin":
                    try:
                        count = os.write(key.fileobj.fileno(), pending[:8192])
                        pending = pending[count:]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        streams.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        streams.unregister(key.fileobj)
                        continue
                    available = max(0, output_limit - total)
                    buffers[key.data].extend(chunk[:available])
                    total += len(chunk)
                    if total > output_limit:
                        limited = "output"
                        break
            if limited:
                break
        if limited:
            proc.kill()
        try:
            proc.wait(timeout=max(.1, timeout - (time.monotonic() - start)))
        except subprocess.TimeoutExpired:
            limited = "time"
            proc.kill()
            proc.wait(timeout=2)
        return {"returncode": proc.returncode, "stdout": bytes(buffers["stdout"]), "stderr": bytes(buffers["stderr"]), "limit": limited}
    finally:
        streams.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if not pipe.closed:
                pipe.close()


def tokens_match(actual, expected):
    # Compare bytes: invalid UTF-8 must never disappear through replacement/ignore.
    return actual.split() == expected.encode("utf-8").split()


class DockerJudge:
    def __init__(self, image="contest-judge:local", prefix="department-judge"):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,60}", prefix):
            raise ValueError("Invalid judge prefix")
        self.image, self.prefix = image, prefix

    def command(self, args, timeout=15):
        try:
            result = bounded_process(["docker", *args], timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InfrastructureError("Docker CLI unavailable") from exc
        if result["returncode"] or result["limit"]:
            logger.error("Docker operation=%s returncode=%s limit=%s stderr=%r", args[0],
                         result["returncode"], result["limit"], result["stderr"][:2000].decode("utf-8", "replace"))
            raise InfrastructureError(f"Docker command failed: {args[0]}")
        return result["stdout"].decode("utf-8", "replace").strip()

    def remove_container(self, name):
        try:
            self.command(["rm", "-f", name])
        except InfrastructureError:
            # A missing container is harmless; an unavailable daemon is not proof of cleanup.
            remaining = self.command(["ps", "-aq", "--filter", f"name=^/{re.escape(name)}$"])
            if remaining:
                raise InfrastructureError("Container could not be removed")

    def remove_volume(self, name):
        try:
            self.command(["volume", "rm", name])
        except InfrastructureError:
            remaining = self.command(["volume", "ls", "-q", "--filter", f"label={LABEL}={self.prefix}"]).splitlines()
            if name in remaining:
                raise InfrastructureError("Volume could not be removed")

    def cleanup_stale(self):
        # Called with the exclusive worker lock: never removes another installation.
        errors = []
        for resource, list_args in (("container", ["ps", "-a", "--format", "{{.Names}}"]), ("volume", ["volume", "ls", "-q"])):
            names = self.command([*list_args, "--filter", f"label={LABEL}={self.prefix}"]).splitlines()
            for name in names:
                try:
                    (self.remove_container if resource == "container" else self.remove_volume)(name)
                except InfrastructureError as exc:
                    errors.append(exc)
        if errors:
            raise InfrastructureError("Stale judge resources could not be removed")

    def create_args(self, name, volume, memory, writable=False):
        return ["create", "--name", name, "--label", f"{LABEL}={self.prefix}",
                "--network", "none", "--read-only", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--cpus", "1", "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
                "--pids-limit", "32", "--ulimit", "nproc=32:32", "--ulimit", "nofile=64:64",
                "--ulimit", "fsize=16777216:16777216", "--ulimit", "core=0:0",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=134217728,mode=1777",
                "--mount", f"type=volume,src={volume},dst=/work" + ("" if writable else ",readonly"),
                "--workdir", "/work", "--log-driver", "none", "--interactive", self.image]

    def execute(self, name, input_text, timeout):
        try:
            result = bounded_process(["docker", "start", "-a", "-i", name], input_text.encode("utf-8"), timeout)
            if result["limit"]:
                # Killing docker CLI does not kill the container: remove it explicitly.
                self.remove_container(name)
                return {**result, "status": "limit_exceeded"}
            state = json.loads(self.command(["inspect", "--format", "{{json .State}}", name]))
            if state.get("Error") or state.get("Status") != "exited":
                raise InfrastructureError("Container did not finish normally")
            if state.get("OOMKilled"):
                status = "limit_exceeded"
            elif state.get("ExitCode") == 0 and result["returncode"] == 0:
                status = "accepted"
            elif result["returncode"] != state.get("ExitCode"):
                raise InfrastructureError("Docker CLI did not report the container exit code")
            else:
                status = "runtime_error"
            return {**result, "status": status}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise InfrastructureError("Container execution failed") from exc

    def judge(self, source, problem, kind, custom_input="", *, files=None):
        token = f"{self.prefix}-{uuid.uuid4().hex}"
        volume = token + "-files"
        containers = set()
        volume_created = False
        cleanup_failed = False
        outcome = None
        checks = []
        def diagnostic(tool, code, message):
            policy = problem.get('diagnostic_policies', {}).get(tool + ':' + code, problem.get(tool + '_policy', 'warning'))
            return {'id': code, 'policy': policy, 'message': message}
        try:
            volume_created = True
            self.command(["volume", "create", "--label", f"{LABEL}={self.prefix}", volume])
            compiler = token + "-compile"
            # Track names BEFORE create: a timed-out docker create may still have succeeded.
            containers.add(compiler)
            files = dict(files or {"main.cpp": source})
            task_name = problem.get("task_name", "task")
            allowed = {"main.cpp"}
            if problem.get("check_mode") == "unit":
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,49}", task_name) or task_name == "main":
                    raise InfrastructureError("Invalid task filename")
                allowed |= {task_name + ".cpp", task_name + ".h"}
            if set(files) - allowed or "main.cpp" not in files:
                raise InfrastructureError("Invalid source file set")
            unit = problem.get("check_mode") == "unit" and kind == "judge"
            if unit:
                files["main.cpp"] = problem["unit_tests"]
            sources = ["main.cpp"] + ([task_name + ".cpp"] if problem.get("check_mode") == "unit" else [])
            compile_args = ["g++", "-std=c++17", "-O2", "-pipe", "-fdiagnostics-color=never", *sources, "-o", "solution"]
            # The private unit-test source is deleted before any student program runs.
            command = (["sh", "-c", shlex.join(compile_args) + " && rm -f main.cpp"] if unit else compile_args)
            self.command([*self.create_args(compiler, volume, 512, writable=True), *command])
            with tempfile.TemporaryDirectory(prefix="contest-source-") as directory:
                for filename, content in files.items():
                    path = Path(directory) / filename
                    path.write_text(content, encoding="utf-8")
                    path.chmod(0o644)
                    self.command(["cp", str(path), compiler + ":/work/" + filename])
            compile_result = self.execute(compiler, "", 30)
            if compile_result["status"] != "accepted":
                outcome = {"status": "limit_exceeded" if compile_result["status"] == "limit_exceeded" else "compile_error",
                           "result": {"compiler": "Ошибка сборки unit-тестов. Проверьте интерфейс решения; подробности доступны преподавателю." if unit else (compile_result["stderr"] + compile_result["stdout"]).decode("utf-8", "replace")[:16000]}}
                if unit:
                    outcome['result']['private_compiler'] = (compile_result['stderr'] + compile_result['stdout']).decode('utf-8', 'replace')[:16000]
            elif kind == "build":
                outcome = {"status": "accepted", "result": {"compiler": "Сборка завершена успешно."}}
            else:
                # Checks execute only inside the same resource-limited sandbox.
                if kind == "judge":
                    student_sources = [f for f in files if not (unit and f == "main.cpp")]
                    if problem.get("format_policy", "off") != "off":
                        name = token + "-format"
                        containers.add(name)
                        self.command([*self.create_args(name, volume, 256), "clang-format", "--dry-run", "--Werror",
                                      "--style=" + problem.get("format_style", "LLVM"), *student_sources])
                        run = self.execute(name, "", 15)
                        checks.append({"tool": "format", "policy": problem["format_policy"], "passed": run["status"] == "accepted",
                                       "blocking": run["status"] != "accepted" and diagnostic("format", "style", "")["policy"] == "required",
                                       "message": "Форматирование соответствует стилю." if run["status"] == "accepted" else "Форматирование не соответствует " + problem.get("format_style", "LLVM") + "."})
                    if problem.get("static_policy", "off") != "off":
                        name = token + "-static"
                        containers.add(name)
                        self.command([*self.create_args(name, volume, 256), "cppcheck", "--enable=warning,style,performance,portability",
                                      "--error-exitcode=1", "--std=c++17", "--xml", "--xml-version=2", *student_sources])
                        run = self.execute(name, "", 20)
                        diagnostics = []
                        try:
                            xml = ElementTree.fromstring(run['stderr'].decode('utf-8', 'replace'))
                            for error in xml.findall('./errors/error'):
                                locations = error.findall('location')
                                where = ' '.join(f"{loc.get('file', '')}:{loc.get('line', '')}" for loc in locations)
                                diagnostics.append(diagnostic('static', error.get('id', 'unknown'), where + ' ' + error.get('msg', '')))
                        except ElementTree.ParseError:
                            if run['status'] != 'accepted':
                                diagnostics.append(diagnostic('static', 'analysis_failed', 'Статический анализ не завершён.'))
                        if run['status'] != 'accepted' and not diagnostics:
                            diagnostics.append(diagnostic('static', 'analysis_failed', 'Статический анализ не завершён.'))
                        checks.append({"tool": "static", "policy": problem["static_policy"], "passed": not diagnostics,
                                       "blocking": any(d['policy'] == 'required' for d in diagnostics), "diagnostics": diagnostics,
                                       "message": '\n'.join(d['id'] + ': ' + d['message'] for d in diagnostics) or 'Замечаний нет.'})
                samples = []
                status = "accepted"
                tests = ([{"label": "Unit-тесты", "input": "", "expected": "", "is_sample": False}] if unit else
                         [{"label": "Свой ввод", "input": custom_input}] if kind == "custom" else
                         [t for t in problem["tests"] if kind != "sample" or t["is_sample"]])
                if not tests:
                    raise InfrastructureError("Submission contains no tests")
                passed, executed, test_report = 0, 0, []
                for index, test in enumerate(tests):
                    name = f"{token}-test-{index}"
                    containers.add(name)
                    self.command([*self.create_args(name, volume, problem["memory_mb"]), "./solution"])
                    run = self.execute(name, test["input"], problem["time_limit"] + .75)
                    verdict = run["status"]
                    if not unit and kind != "custom" and verdict == "accepted" and not tokens_match(run["stdout"], test["expected"]):
                        verdict = "wrong_answer"
                    # No hidden output, input, label or diagnostic is persisted.
                    if kind in ("sample", "custom"):
                        samples.append({"label": test["label"], "status": verdict,
                                        "stdout": run["stdout"].decode("utf-8", "replace"),
                                        "stderr": run["stderr"].decode("utf-8", "replace")})
                    executed += 1
                    passed += verdict == "accepted"
                    test_report.append({"number": index + 1, "status": verdict})
                    self.remove_container(name)
                    containers.discard(name)
                    if verdict != "accepted":
                        status = verdict
                        break
                if kind == "judge" and problem.get("dynamic_policy", "off") != "off":
                    # Restore the harness for compilation only, then remove it before running.
                    name = token + "-sanitize-compile"
                    containers.add(name)
                    sanitizer_args = ["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", *sources, "-o", "sanitized"]
                    self.command([*self.create_args(name, volume, 512, writable=True), "sh", "-c",
                                  shlex.join(sanitizer_args) + (" && rm -f main.cpp" if unit else "")])
                    if unit:
                        with tempfile.TemporaryDirectory(prefix="contest-unit-") as directory:
                            path = Path(directory) / "main.cpp"
                            path.write_text(problem["unit_tests"], encoding="utf-8")
                            path.chmod(0o644)
                            self.command(["cp", str(path), name + ":/work/main.cpp"])
                    compiled = self.execute(name, "", 30)
                    dynamic_ok = compiled["status"] == "accepted"
                    dynamic_code = "compile_sanitizer"
                    if dynamic_ok:
                        for index, test in enumerate(tests):
                            name = f"{token}-sanitize-{index}"
                            containers.add(name)
                            # ASan reserves virtual address space; the Docker physical memory cap still applies.
                            self.command([*self.create_args(name, volume, max(256, problem["memory_mb"])),
                                          "env", "ASAN_OPTIONS=detect_leaks=1:halt_on_error=1", "UBSAN_OPTIONS=halt_on_error=1", "./sanitized"])
                            run = self.execute(name, test["input"], max(2, problem["time_limit"] * 3) + .75)
                            dynamic_ok = run["status"] == "accepted"
                            diagnostic_text = run['stderr'].decode('utf-8', 'replace')
                            dynamic_code = 'address' if 'AddressSanitizer' in diagnostic_text or 'LeakSanitizer' in diagnostic_text else 'undefined' if 'runtime error:' in diagnostic_text else 'execution_failed'
                            self.remove_container(name)
                            containers.discard(name)
                            if not dynamic_ok:
                                break
                    diagnostics = [] if dynamic_ok else [diagnostic('dynamic', dynamic_code, 'Динамический анализ обнаружил ошибку: ' + dynamic_code + '.')]
                    checks.append({"diagnostics": diagnostics, "blocking": any(d['policy'] == 'required' for d in diagnostics), "tool": "dynamic", "policy": problem["dynamic_policy"], "passed": dynamic_ok,
                                   "message": "Ошибок памяти и неопределённого поведения не обнаружено." if dynamic_ok else "Динамический анализ завершился с ошибкой. Проверьте память и неопределённое поведение."})
                if status == "accepted" and any(c.get("blocking", c["policy"] == "required" and not c["passed"]) for c in checks):
                    status = "wrong_answer"
                result = {"samples": samples} if kind in ("sample", "custom") else {"tests": {"passed": passed, "executed": executed, "total": len(tests)}}
                if kind == "judge":
                    result["test_report"] = test_report
                if checks:
                    result["checks"] = checks
                outcome = {"status": status, "result": result}
        except InfrastructureError:
            logger.exception("Judge infrastructure failure resource=%s kind=%s", token, kind)
            outcome = {"status": "infra_error", "result": {"message": "Проверка временно недоступна. Преподаватель может повторить её."}}
        finally:
            for name in containers:
                try:
                    self.remove_container(name)
                except InfrastructureError:
                    cleanup_failed = True
            if volume_created:
                try:
                    self.remove_volume(volume)
                except InfrastructureError:
                    cleanup_failed = True
        if cleanup_failed:
            # Stop the worker until its next startup can confirm resource cleanup.
            raise InfrastructureError("Judge cleanup failed")
        return outcome
