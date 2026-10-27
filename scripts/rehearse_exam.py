"""Real HTTP + Docker rehearsal on a disposable database; never uses working data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from http.cookiejar import CookieJar
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
SOURCE = '#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}'


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "name" in attrs:
            self.values[attrs["name"]] = attrs.get("value", "")


class Session:
    def __init__(self, base):
        self.base = base
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.timings = []

    def request(self, path, data=None, form=False):
        headers = {}
        if data is not None:
            headers["X-CSRFToken"] = next(c.value for c in self.cookies if c.name == "csrftoken")
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            data = (urlencode(data) if form else json.dumps(data)).encode()
        started = time.monotonic()
        with self.opener.open(Request(self.base + path, data=data, headers=headers), timeout=40) as response:
            body = response.read().decode()
            content_type = response.headers.get("Content-Type", "")
            result = json.loads(body) if "application/json" in content_type else body
            url = response.url
        self.timings.append(time.monotonic() - started)
        return result, url

    def login(self, username, password):
        self.request("/accounts/login/")
        _, url = self.request("/accounts/login/", {"username": username, "password": password}, form=True)
        if "/accounts/login/" in url:
            raise RuntimeError("Rehearsal login failed")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--students", type=int, default=30, choices=range(1, 31), metavar="1..30")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum queue drain time in seconds")
    parser.add_argument("--browser", action="store_true", help="Also run the real student/teacher Chromium scenario")
    parser.add_argument("--restart-worker", action="store_true", help="Stop a busy worker and requeue its interrupted submission over HTTP")
    parser.add_argument("--report", type=Path, help="Write timings and acceptance results as JSON")
    parser.add_argument("--problem-json", type=Path, help="Private UTF-8 JSON: title, statement, reference_source, tests; optional time_limit, memory_mb")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.problem_json and args.browser:
        parser.error("--browser uses the built-in sum exercise; use --problem-json for HTTP/Docker load rehearsal")
    workload = json.loads(args.problem_json.read_text(encoding="utf-8")) if args.problem_json else {
        "title": "Rehearsal sum", "statement": "Сложите два целых числа.", "reference_source": SOURCE,
        "tests": [{"label": f"test-{index}", "input": stdin, "expected": expected, "is_sample": index == 0}
                  for index, (stdin, expected) in enumerate((("2 3", "5"), ("-8 3", "-5"), ("1000000000 1000000000", "2000000000")))]}
    if not isinstance(workload, dict) or not all(key in workload for key in ("title", "statement", "reference_source", "tests")):
        parser.error("Workload needs title, statement, reference_source and tests")
    if not isinstance(workload["tests"], list) or not 1 <= len(workload["tests"]) <= 100:
        parser.error("Workload needs 1..100 tests")
    source = workload["reference_source"]
    sys.path.insert(0, str(ROOT))
    from judge.docker_runner import DockerJudge
    judge = DockerJudge(os.environ.get("JUDGE_IMAGE", "contest-judge:local"), "rehearsal-" + uuid4().hex[:12])
    # Fail before creating data or starting services if real execution is unavailable.
    judge.command(["info"])
    judge.command(["image", "inspect", judge.image])
    with tempfile.TemporaryDirectory(prefix="contest-rehearsal-") as directory, ExitStack() as stack:
        directory = Path(directory)
        env = {**os.environ, "DEBUG": "1", "HTTPS": "0", "SECRET_KEY": secrets.token_urlsafe(64),
               "DATABASE_PATH": str(directory / "db.sqlite3"), "WORKER_LOCK": str(directory / "worker.lock"),
               "ALLOWED_HOSTS": "127.0.0.1,localhost", "JUDGE_PREFIX": judge.prefix, "JUDGE_IMAGE": judge.image,
               "DJANGO_SETTINGS_MODULE": "config.settings"}
        env.pop("TEST_DATABASE_PATH", None)
        os.environ.update(env)
        os.environ.pop("TEST_DATABASE_PATH", None)
        for command in ("migrate", "configure_sqlite"):
            subprocess.run([sys.executable, "manage.py", command], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
        import django
        django.setup()
        from django.contrib.auth.models import Group, User
        from django.core.management import call_command
        from io import StringIO
        from django.utils import timezone
        from lab import services
        from lab.models import Course, Assessment, AssessmentProblem, Attempt, Problem, Submission, TestCase, TheoryAnswer, TheoryQuestion
        password = secrets.token_urlsafe(24)
        teacher = User.objects.create_user("rehearsal-teacher", password=password, is_staff=True)
        group = Group.objects.create(name="Rehearsal")
        course = Course.objects.create(title="Rehearsal course")
        course.teachers.add(teacher)
        course.groups.add(group)
        usernames = [f"rehearsal-{index}" for index in range(args.students + int(args.browser))]
        for username in usernames:
            user = User.objects.create_user(username, password=password)
            user.groups.add(group)
        problem = Problem(course=course, title=workload["title"], statement=workload["statement"], starter_code="int main(){}",
            reference_source=source, time_limit=workload.get("time_limit", 2), memory_mb=workload.get("memory_mb", 128))
        problem.full_clean()
        problem.save()
        for index, test in enumerate(workload["tests"]):
            item = TestCase(problem=problem, label=test.get("label", f"test-{index}"), input=test["input"],
                            expected=test["expected"], is_sample=test.get("is_sample", False))
            item.full_clean()
            item.save()
        from lab.reference_checks import queue_reference_check
        reference = queue_reference_check(teacher, problem.pk)
        call_command("judge_worker", once=True, stdout=StringIO())
        reference.refresh_from_db()
        if reference.status != "accepted":
            raise RuntimeError(f"Reference solution failed: {reference.status}")
        now = timezone.now()
        work = Assessment.objects.create(course=course, title="Disposable rehearsal", kind="exam", opens_at=now-timedelta(minutes=1),
                                         closes_at=now+timedelta(hours=2), duration_minutes=60, pass_score=12)
        work.groups.add(group)
        AssessmentProblem.objects.create(assessment=work, problem=problem, max_score=10)
        question = TheoryQuestion.objects.create(assessment=work, problem=problem, text="Почему выбран long long?", max_score=5)
        services.publish_assessment(teacher, work.pk)
        with socket.socket() as address:
            address.bind(("127.0.0.1", 0))
            port = address.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        log = stack.enter_context((directory / "processes.log").open("w+"))
        application = "tests.rehearsal_wsgi:application" if args.browser else "config.wsgi:application"
        web = subprocess.Popen([sys.executable, "-m", "gunicorn", application, "--bind", f"127.0.0.1:{port}",
                                "--workers=2", "--threads=4", "--timeout=60"], cwd=ROOT, env=env, stdout=log, stderr=log)
        stack.callback(stop, web)
        deadline = time.monotonic() + 20
        while True:
            try:
                Session(base).request("/accounts/login/")
                break
            except (URLError, ConnectionError):
                if web.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Rehearsal web process failed to start")
                time.sleep(.1)
        worker = subprocess.Popen([sys.executable, "manage.py", "judge_worker"], cwd=ROOT, env=env, stdout=log, stderr=log)
        stack.callback(judge.cleanup_stale)
        stack.callback(stop, worker)
        def student(username):
            session = Session(base)
            session.login(username, password)
            _, url = session.request(f"/assessments/{work.pk}/start/", {})
            attempt_id = int(url.rstrip("/").split("/")[-1])
            prefix = f"/attempts/{attempt_id}/"
            code = {"type": "code", "problem": problem.pk, "source": source, "expected_revision": 0}
            saved, _ = session.request(prefix + "save/", code)
            code["expected_revision"] = saved["revision"]
            payload = {**code, "kind": "judge", "request_id": str(uuid4())}
            submitted, _ = session.request(prefix + "submit/", payload)
            repeated, _ = session.request(prefix + "submit/", payload)
            if submitted["submission"]["id"] != repeated["submission"]["id"]:
                raise RuntimeError("Repeated request created a second submission")
            theory = {"type": "theory", "question": question.pk, "text": "long long вмещает сумму.",
                      "submission": submitted["submission"]["id"], "expected_revision": 0}
            saved, _ = session.request(prefix + "save/", theory)
            theory["expected_revision"] = saved["revision"]
            finish = {"confirm": True, "drafts": [code], "answers": [theory]}
            session.request(prefix + "finish/", finish)
            session.request(prefix + "finish/", finish)
            return session, attempt_id
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.students) as executor:
            completed = list(executor.map(student, usernames[:args.students]))
        teacher_session = Session(base)
        teacher_session.login(teacher.username, password)
        if args.restart_worker:
            interrupted = Submission.objects.filter(status="running").first()
            if interrupted is None:
                raise RuntimeError("No running submission to interrupt; repeat with --students 30")
            stop(worker)
            interrupted.refresh_from_db()
            if interrupted.status != "infra_error":
                raise RuntimeError("Interrupted submission was not marked as an infrastructure error")
            worker = subprocess.Popen([sys.executable, "manage.py", "judge_worker"], cwd=ROOT, env=env, stdout=log, stderr=log)
            stack.callback(stop, worker)
            teacher_session.request(f"/teacher/submissions/{interrupted.pk}/requeue/", {}, form=True)
        queued_until = time.monotonic() + args.timeout
        while Submission.objects.filter(status__in=["queued", "running"]).exists():
            if worker.poll() is not None or time.monotonic() >= queued_until:
                raise RuntimeError("Worker stopped or queue drain timed out")
            time.sleep(.5)
        if Submission.objects.exclude(status="accepted").exists() or Submission.objects.count() != args.students:
            raise RuntimeError("Some real Docker submissions failed or were duplicated")
        for session, attempt_id in completed:
            path = f"/teacher/attempts/{attempt_id}/"
            html, _ = teacher_session.request(path)
            inputs = Inputs(html).values
            teacher_session.request(path, {"action": "grade", "question": question.pk,
                                           f"q{question.pk}-score": 5, f"q{question.pk}-feedback": "Rehearsal checked",
                                           f"q{question.pk}-expected_updated_at": inputs[f"q{question.pk}-expected_updated_at"]}, form=True)
            html, _ = session.request(f"/attempts/{attempt_id}/results/")
            if "Зачтено" not in html or "Предварительный результат" in html:
                raise RuntimeError("Final result was not confirmed")
        if args.browser:
            browser_env = {**env, "REHEARSAL_BASE": base, "REHEARSAL_USERNAME": usernames[-1], "REHEARSAL_PASSWORD": password,
                           "REHEARSAL_ASSESSMENT": str(work.pk), "REHEARSAL_PROBLEM": str(problem.pk), "REHEARSAL_QUESTION": str(question.pk)}
            subprocess.run(["node", "tests/exam.browser.mjs"], cwd=ROOT, env=browser_env, check=True)
        stop(worker)
        judge.cleanup_stale()
        backup = directory / "backup.sqlite3"
        subprocess.run([sys.executable, "manage.py", "backup_database", str(backup)], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
        from scripts.restore_database import restore
        restored = directory / "restore" / "db.sqlite3"
        restored.parent.mkdir()
        with backup.open("rb") as stream:
            restore(restored, stream)
        with sqlite3.connect(restored) as connection:
            if connection.execute("SELECT count(*) FROM lab_attempt").fetchone()[0] != Attempt.objects.count():
                raise RuntimeError("Attempt count changed after restoration")
            if connection.execute("SELECT count(*) FROM lab_theoryanswer WHERE score=5").fetchone()[0] != TheoryAnswer.objects.filter(score=5).count():
                raise RuntimeError("Grades changed after restoration")
        timings = sorted(timing for session, _ in completed for timing in session.timings)
        jobs = list(Submission.objects.all())
        report = {"students": args.students, "browser": args.browser, "worker_restart": args.restart_worker,
                  "workload": {"title": problem.title, "tests": len(workload["tests"]), "time_limit": problem.time_limit, "memory_mb": problem.memory_mb},
                  "reference_check": "passed", "seconds": round(time.monotonic()-started, 3),
                  "http_p95_seconds": round(timings[min(len(timings)-1, int(len(timings)*.95))], 3),
                  "max_queue_wait_seconds": round(max((s.started_at-s.created_at).total_seconds() for s in jobs), 3),
                  "max_judge_seconds": round(max((s.finished_at-s.started_at).total_seconds() for s in jobs), 3),
                  "submissions": len(jobs), "duplicate_requests": "passed", "final_results": "passed", "backup_restore": "passed"}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Rehearsal failed: {exc}") from exc
