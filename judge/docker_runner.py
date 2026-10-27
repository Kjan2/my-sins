"""Only Docker executes untrusted code. This module uses the Python standard library."""
import json
import logging
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time
import uuid

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

    def judge(self, source, problem, kind, custom_input=""):
        token = f"{self.prefix}-{uuid.uuid4().hex}"
        volume = token + "-files"
        containers = set()
        volume_created = False
        cleanup_failed = False
        outcome = None
        try:
            volume_created = True
            self.command(["volume", "create", "--label", f"{LABEL}={self.prefix}", volume])
            compiler = token + "-compile"
            # Track names BEFORE create: a timed-out docker create may still have succeeded.
            containers.add(compiler)
            self.command([*self.create_args(compiler, volume, 512, writable=True), "g++", "-std=c++17", "-O2", "-pipe", "-fdiagnostics-color=never", "main.cpp", "-o", "solution"])
            with tempfile.TemporaryDirectory(prefix="contest-source-") as directory:
                path = Path(directory) / "main.cpp"
                path.write_text(source, encoding="utf-8")
                path.chmod(0o644)
                self.command(["cp", str(path), compiler + ":/work/main.cpp"])
            compile_result = self.execute(compiler, "", 30)
            if compile_result["status"] != "accepted":
                outcome = {"status": "limit_exceeded" if compile_result["status"] == "limit_exceeded" else "compile_error",
                           "result": {"compiler": (compile_result["stderr"] + compile_result["stdout"]).decode("utf-8", "replace")[:16000]}}
            else:
                samples = []
                status = "accepted"
                tests = ([{"label": "Свой ввод", "input": custom_input}] if kind == "custom" else
                         [t for t in problem["tests"] if kind != "sample" or t["is_sample"]])
                if not tests:
                    raise InfrastructureError("Submission contains no tests")
                for index, test in enumerate(tests):
                    name = f"{token}-test-{index}"
                    containers.add(name)
                    self.command([*self.create_args(name, volume, problem["memory_mb"]), "./solution"])
                    run = self.execute(name, test["input"], problem["time_limit"] + .75)
                    verdict = run["status"]
                    if kind != "custom" and verdict == "accepted" and not tokens_match(run["stdout"], test["expected"]):
                        verdict = "wrong_answer"
                    # No hidden output, input, label or diagnostic is persisted.
                    if kind in ("sample", "custom"):
                        samples.append({"label": test["label"], "status": verdict,
                                        "stdout": run["stdout"].decode("utf-8", "replace"),
                                        "stderr": run["stderr"].decode("utf-8", "replace")})
                    self.remove_container(name)
                    containers.discard(name)
                    if verdict != "accepted":
                        status = verdict
                        break
                outcome = {"status": status, "result": {"samples": samples} if kind in ("sample", "custom") else {}}
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
