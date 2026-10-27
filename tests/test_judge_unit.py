import copy
import json
import sys
import unittest
from unittest.mock import patch
from judge.docker_runner import DockerJudge, InfrastructureError, OUTPUT_LIMIT, bounded_process, tokens_match

PROBLEM={"time_limit":.2,"memory_mb":64,"tests":[{"label":"SECRET","input":"PRIVATE_INPUT","expected":"42","is_sample":False}]}


class ProcessTests(unittest.TestCase):
    def test_token_comparison(self):
        self.assertTrue(tokens_match(b" 42 \n 8\t", "42 8\n"))
        self.assertFalse(tokens_match(b"4\xff2", "42"))
        self.assertFalse(tokens_match(b"42.0", "42"))

    def test_bounded_stdout_and_stderr(self):
        result=bounded_process([sys.executable,"-c","import sys; sys.stderr.write('x'*1000000)"],output_limit=4096)
        self.assertEqual(result["limit"],"output")
        self.assertLessEqual(len(result["stdout"])+len(result["stderr"]),4096)

    def test_wall_timeout(self):
        result=bounded_process([sys.executable,"-c","import time; time.sleep(5)"],timeout=.1)
        self.assertEqual(result["limit"],"time")

    def test_stdin_and_stream_drain(self):
        result=bounded_process([sys.executable,"-c","import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],b"hello"*1000)
        self.assertEqual(result["stdout"],b"hello"*1000)
        self.assertEqual(result["returncode"],0)
        self.assertIsNone(result["limit"])

    def test_nonzero_exit(self):
        result=bounded_process([sys.executable,"-c","raise SystemExit(7)"])
        self.assertEqual(result["returncode"],7)


class FakeJudge(DockerJudge):
    def __init__(self, runs, fail_at=None):
        super().__init__()
        self.runs=iter(runs)
        self.calls=[]
        self.fail_at=fail_at

    def command(self,args,timeout=15):
        self.calls.append(args)
        if self.fail_at and args[0]==self.fail_at:
            raise InfrastructureError("unavailable")
        return ""

    def execute(self,name,input_text,timeout):
        self.calls.append(["execute",name,input_text])
        return next(self.runs)


def run_result(status="accepted",stdout=b"",stderr=b""):
    return {"status":status,"stdout":stdout,"stderr":stderr,"returncode":0,"limit":None}


class JudgeTests(unittest.TestCase):
    def test_custom_executes_only_user_input_without_expected_comparison(self):
        judge=FakeJudge([run_result(),run_result(stdout=b"anything",stderr=b"note")])
        outcome=judge.judge("source",PROBLEM,"custom",custom_input="user input")
        self.assertEqual(outcome["status"],"accepted")
        self.assertEqual(outcome["result"]["samples"][0]["stdout"],"anything")
        self.assertEqual(outcome["result"]["samples"][0]["stderr"],"note")
        executions=[c for c in judge.calls if c[0]=="execute"]
        self.assertEqual(len(executions),2)
        self.assertEqual(executions[1][2],"user input")
        self.assertNotIn("PRIVATE_INPUT",json.dumps(judge.calls))
        self.assertTrue(any(c[:2]==["volume","rm"] for c in judge.calls))

    def test_custom_runtime_and_limits_preserve_public_output(self):
        for verdict in ("runtime_error","limit_exceeded"):
            judge=FakeJudge([run_result(),run_result(verdict,stdout=b"partial")])
            outcome=judge.judge("source",PROBLEM,"custom",custom_input="")
            self.assertEqual(outcome["status"],verdict)
            self.assertEqual(outcome["result"]["samples"][0]["stdout"],"partial")

    def test_compile_once_fresh_container_per_test_and_hidden_output_not_persisted(self):
        problem=copy.deepcopy(PROBLEM)
        problem["tests"]*=2
        judge=FakeJudge([run_result(),run_result(stdout=b"42",stderr=b"SECRET_STDERR"),run_result(stdout=b"42")])
        result=judge.judge("source",problem,"judge")
        self.assertEqual(result,{"status":"accepted","result":{}})
        creates=[c for c in judge.calls if c[0]=="create"]
        self.assertEqual(len(creates),3)
        self.assertEqual(sum("g++" in c for c in creates),1)
        self.assertEqual(len({c[c.index("--name")+1] for c in creates}),3)
        self.assertTrue(any(c[:2]==["volume","rm"] for c in judge.calls))

    def test_sample_output_is_public(self):
        problem=copy.deepcopy(PROBLEM)
        problem["tests"][0]["is_sample"]=True
        judge=FakeJudge([run_result(),run_result(stdout=b"bad")])
        result=judge.judge("source",problem,"sample")
        self.assertEqual(result["status"],"wrong_answer")
        self.assertEqual(result["result"]["samples"][0]["stdout"],"bad")

    def test_compile_error_cleanup(self):
        judge=FakeJudge([run_result("runtime_error",stderr=b"syntax")])
        result=judge.judge("source",PROBLEM,"judge")
        self.assertEqual(result["status"],"compile_error")
        self.assertEqual(result["result"]["compiler"],"syntax")
        self.assertTrue(any(c[:2]==["rm","-f"] for c in judge.calls))
        self.assertTrue(any(c[:2]==["volume","rm"] for c in judge.calls))

    def test_infrastructure_failure_does_not_fallback_to_host(self):
        judge=FakeJudge([],fail_at="create")
        self.assertEqual(judge.judge("source",PROBLEM,"judge")["status"],"infra_error")
        self.assertFalse(any(c[0]=="execute" for c in judge.calls))
        self.assertTrue(any(c[:2]==["volume","rm"] for c in judge.calls))

    def test_infrastructure_failure_logs_correlation_without_source_or_tests(self):
        judge = FakeJudge([], fail_at="create")
        with self.assertLogs("judge.docker_runner", level="ERROR") as logs:
            result = judge.judge("PRIVATE_SOURCE", PROBLEM, "judge")
        self.assertEqual(result["status"], "infra_error")
        output = "\n".join(logs.output)
        self.assertIn("resource=", output)
        self.assertNotIn("PRIVATE_SOURCE", output)

    def test_command_failure_logs_bounded_diagnostic(self):
        judge = DockerJudge()
        output = {"returncode": 1, "stdout": b"", "stderr": b"daemon refused " + b"x" * 6000, "limit": None}
        with patch("judge.docker_runner.bounded_process", return_value=output), self.assertLogs("judge.docker_runner") as logs:
            with self.assertRaises(InfrastructureError):
                judge.command(["create", "name"])
        self.assertIn("daemon refused", logs.output[0])
        self.assertLess(len(logs.output[0]), 2300)

    def test_security_options_and_readonly_runtime(self):
        judge=DockerJudge()
        args=judge.create_args("name","volume",128)
        for value in ["--network","none","--read-only","65534:65534","--cap-drop","ALL","no-new-privileges:true","--pids-limit","--memory-swap","--cpus","--tmpfs","core=0:0","--log-driver"]:
            self.assertIn(value,args)
        self.assertIn("type=volume,src=volume,dst=/work,readonly",args)
        self.assertNotIn("--privileged",args)
        self.assertFalse(any("docker.sock" in a for a in args))
        self.assertIn("type=volume,src=volume,dst=/work",judge.create_args("n","v".replace("v","volume"),512,writable=True))

    def test_timeout_removes_running_container(self):
        judge=DockerJudge()
        with patch("judge.docker_runner.bounded_process",return_value={"limit":"time","returncode":-9,"stdout":b"","stderr":b""}), patch.object(judge,"command") as command:
            result=judge.execute("name","",.1)
        self.assertEqual(result["status"],"limit_exceeded")
        command.assert_called_once_with(["rm","-f","name"])

    def test_invalid_prefix_rejected(self):
        with self.assertRaises(ValueError): DockerJudge(prefix="../escape")

    def test_container_exit_and_oom_verdicts(self):
        for code,oom,expected in [(0,False,"accepted"),(2,False,"runtime_error"),(137,True,"limit_exceeded")]:
            with self.subTest(code=code):
                judge=DockerJudge()
                state=json.dumps({"Status":"exited","ExitCode":code,"OOMKilled":oom,"Error":""})
                output={"returncode":code,"stdout":b"","stderr":b"","limit":None}
                with patch("judge.docker_runner.bounded_process",return_value=output), patch.object(judge,"command",return_value=state):
                    self.assertEqual(judge.execute("name","",1)["status"],expected)

    def test_lost_docker_connection_is_not_student_runtime_error(self):
        state=json.dumps({"Status":"exited","ExitCode":0,"OOMKilled":False,"Error":""})
        output={"returncode":1,"stdout":b"","stderr":b"","limit":None}
        judge=DockerJudge()
        with patch("judge.docker_runner.bounded_process",return_value=output), patch.object(judge,"command",return_value=state):
            with self.assertRaises(InfrastructureError): judge.execute("name","",1)

    def test_daemon_failure_is_not_proof_of_cleanup(self):
        judge=DockerJudge()
        with patch.object(judge,"command",side_effect=InfrastructureError("unavailable")):
            with self.assertRaises(InfrastructureError): judge.remove_container("name")
            with self.assertRaises(InfrastructureError): judge.remove_volume("volume")
