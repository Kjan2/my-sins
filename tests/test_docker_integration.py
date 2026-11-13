"""Real sandbox tests. Never run any of these C++ programs outside Docker."""
import os
import subprocess
import unittest
import uuid
from judge.docker_runner import DockerJudge, LABEL


@unittest.skipUnless(os.environ.get("RUN_DOCKER_TESTS")=="1", "Requires explicit RUN_DOCKER_TESTS=1 and a real Docker daemon")
class DockerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prefix="test-judge-"+uuid.uuid4().hex[:12]
        cls.judge=DockerJudge(os.environ.get("JUDGE_IMAGE","contest-judge:local"),cls.prefix)
        cls.judge.command(["info"])
        cls.judge.command(["image","inspect",cls.judge.image])

    def tearDown(self):
        containers=self.judge.command(["ps","-aq","--filter",f"label={LABEL}={self.prefix}"])
        volumes=self.judge.command(["volume","ls","-q","--filter",f"label={LABEL}={self.prefix}"])
        self.assertEqual(containers,"")
        self.assertEqual(volumes,"")

    def run_source(self,source,expected="42",kind="judge",tests=None):
        problem={"time_limit":.5,"memory_mb":64,"tests":tests or [{"label":"hidden","input":"","expected":expected,"is_sample":False}]}
        return self.judge.judge(source,problem,kind)

    def test_correct(self):
        self.assertEqual(self.run_source('#include <iostream>\nint main(){std::cout<<42;}')["status"],"accepted")

    def test_utf8_output_at_byte_limit(self):
        from lab.validators import validate_utf8_size
        from django.core.exceptions import ValidationError
        expected = "я" * 32768
        validate_utf8_size(expected)
        with self.assertRaises(ValidationError):
            validate_utf8_size(expected + "я")
        source = '#include <iostream>\nint main(){for(int i=0;i<32768;++i)std::cout<<"я";}'
        self.assertEqual(self.run_source(source, expected=expected)["status"], "accepted")

    def test_custom_input_stdout_stderr(self):
        problem={"time_limit":.5,"memory_mb":64,"tests":[]}
        source='#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b;std::cerr<<"note";}'
        result=self.judge.judge(source,problem,"custom",custom_input="7 8")
        self.assertEqual(result["status"],"accepted")
        self.assertEqual(result["result"]["samples"][0]["stdout"],"15")
        self.assertEqual(result["result"]["samples"][0]["stderr"],"note")

    def test_custom_timeout(self):
        result=self.judge.judge('int main(){for(;;){asm volatile("");}}',{"time_limit":.1,"memory_mb":64},"custom")
        self.assertEqual(result["status"],"limit_exceeded")

    def test_wrong(self):
        self.assertEqual(self.run_source('#include <iostream>\nint main(){std::cout<<0;}')["status"],"wrong_answer")

    def test_compile_error(self):
        self.assertEqual(self.run_source('this is not C++')["status"],"compile_error")

    def test_runtime_error(self):
        self.assertEqual(self.run_source('int main(){return 2;}')["status"],"runtime_error")

    def test_infinite_loop(self):
        self.assertEqual(self.run_source('int main(){for(;;){asm volatile("");}}')["status"],"limit_exceeded")

    def test_output_flood(self):
        self.assertEqual(self.run_source('#include <unistd.h>\nint main(){char b[4096]={};for(;;)write(1,b,sizeof(b));}')["status"],"limit_exceeded")

    def test_memory_exhaustion(self):
        source='#include <cstdlib>\n#include <cstring>\nint main(){for(;;){void* p=malloc(1048576);if(!p)return 9;memset(p,1,1048576);asm volatile(""::"r"(p):"memory");}}'
        self.assertIn(self.run_source(source)["status"],("limit_exceeded","runtime_error"))

    def test_child_process_limit(self):
        source='#include <unistd.h>\n#include <iostream>\nint main(){for(int i=0;i<100;i++){int p=fork();if(p==0){for(;;)pause();}if(p<0){std::cout<<"bounded";return 0;}}return 1;}'
        self.assertEqual(self.run_source(source,expected="bounded")["status"],"accepted")

    def test_no_network_root_write_or_docker_socket(self):
        source='''#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fstream>
#include <iostream>
int main(){if(getuid()==0)return 1;std::ofstream f("/forbidden");if(f)return 2;
if(access("/var/run/docker.sock",F_OK)==0)return 3;
int s=socket(AF_INET,SOCK_STREAM,0);sockaddr_in a{};a.sin_family=AF_INET;a.sin_port=htons(53);inet_pton(AF_INET,"1.1.1.1",&a.sin_addr);
if(connect(s,(sockaddr*)&a,sizeof(a))==0)return 4;std::cout<<42;}'''
        self.assertEqual(self.run_source(source)["status"],"accepted")

    def test_each_test_has_fresh_tmp_and_readonly_binary(self):
        source='''#include <fstream>
#include <iostream>
#include <unistd.h>
int main(){std::ifstream old("/tmp/marker");if(old)return 1;std::ofstream out("/tmp/marker");out<<1;
if(access("/work/solution",W_OK)==0)return 2;std::cout<<42;}'''
        cases=[{"label":"hidden","input":"","expected":"42","is_sample":False}]*2
        self.assertEqual(self.run_source(source,tests=cases)["status"],"accepted")

    def test_missing_image_reports_infra_and_cleanup(self):
        # An invalid image must fail closed; it cannot execute a local compiler.
        judge=DockerJudge("contest-image-does-not-exist:invalid",self.prefix)
        self.assertEqual(judge.judge("int main(){}",{"time_limit":1,"memory_mb":64,"tests":[{"label":"x","input":"","expected":"","is_sample":False}]},"judge")["status"],"infra_error")

    def policy_problem(self, **kwargs):
        return {"time_limit":.5, "memory_mb":128,
                "tests":[{"label":"SECRET_LABEL","input":"","expected":"42","is_sample":False}], **kwargs}

    def test_formatting_can_block_or_warn(self):
        source = '#include <iostream>\nint main(){std::cout<<42;}'
        for policy, verdict in [('required','wrong_answer'), ('warning','accepted')]:
            result = self.judge.judge(source, self.policy_problem(format_policy=policy, format_style='LLVM'), 'judge')
            self.assertEqual(result['status'], verdict, result)
            self.assertFalse(result['result']['checks'][0]['passed'])

    def test_static_analysis_can_block_or_warn(self):
        source = '#include <iostream>\nint main(){int unused;std::cout<<42;}'
        for policy, verdict in [('required','wrong_answer'), ('warning','accepted')]:
            result = self.judge.judge(source, self.policy_problem(static_policy=policy), 'judge')
            self.assertEqual(result['status'], verdict, result)
            self.assertEqual(result['result']['checks'][0]['tool'], 'static')

    def test_undefined_behavior_dynamic_analysis(self):
        source = '#include <iostream>\nint main(){int x;std::cin>>x;volatile int y=x+1;std::cout<<42;}'
        problem = self.policy_problem(dynamic_policy='required', tests=[{'label':'PRIVATE_LABEL','input':'2147483647','expected':'42','is_sample':False}])
        result = self.judge.judge(source, problem, 'judge')
        self.assertEqual(result['status'], 'wrong_answer', result)
        self.assertFalse(result['result']['checks'][0]['passed'])
        self.assertNotIn('2147483647', str(result))
        self.assertNotIn('PRIVATE_LABEL', str(result))

    def test_dynamic_analysis_accepts_correct_code(self):
        result = self.judge.judge('#include <iostream>\nint main(){std::cout<<42;}', self.policy_problem(dynamic_policy='required'), 'judge')
        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['result']['checks'][0]['passed'])

    def test_unit_tests_use_private_main_and_student_implementation(self):
        problem = self.policy_problem(check_mode='unit', task_name='sum',
            unit_tests='#include "sum.h"\nint main(){return add(20,22)==42 ? 0 : 1;}')
        files = {'main.cpp':'#include <iostream>\nint main(){std::cout<<99;}',
                 'sum.cpp':'#include "sum.h"\nint add(int a,int b){return a+b;}', 'sum.h':'int add(int,int);'}
        result = self.judge.judge(files['main.cpp'], problem, 'judge', files=files)
        self.assertEqual(result['status'], 'accepted', result)
        custom = self.judge.judge(files['main.cpp'], problem, 'custom', files=files)
        self.assertEqual(custom['result']['samples'][0]['stdout'], '99')
        files['sum.cpp'] = '#include "sum.h"\nint add(int,int){return 0;}'
        wrong = self.judge.judge(files['main.cpp'], problem, 'judge', files=files)
        self.assertEqual(wrong['status'], 'runtime_error')

    def test_private_unit_source_is_absent_at_runtime_and_not_in_errors(self):
        problem = self.policy_problem(check_mode='unit', task_name='task', unit_tests='#include "task.h"\nint PRIVATE_HARNESS_SECRET=7; int main(){return run();}')
        files = {'main.cpp':'int main(){}', 'task.h':'int run();',
                 'task.cpp':'#include <unistd.h>\nint run(){return access("/work/main.cpp",F_OK)==0 ? 1 : 0;}'}
        result = self.judge.judge(files['main.cpp'], problem, 'judge', files=files)
        self.assertEqual(result['status'], 'accepted', result)
        files['task.cpp'] = 'not C++'
        result = self.judge.judge(files['main.cpp'], problem, 'judge', files=files)
        self.assertEqual(result['status'], 'compile_error')
        self.assertNotIn('PRIVATE_HARNESS_SECRET', result['result']['compiler'])

    def test_build_does_not_execute_program(self):
        result = self.judge.judge('int main(){for(;;){}}', self.policy_problem(), 'build')
        self.assertEqual(result['status'], 'accepted', result)

    def test_static_diagnostic_override_can_make_one_error_advisory(self):
        source = '#include <iostream>\nint main(){int unused;std::cout<<42;}'
        problem = self.policy_problem(static_policy='required', diagnostic_policies={'static:unusedVariable':'warning'})
        result = self.judge.judge(source, problem, 'judge')
        self.assertEqual(result['status'], 'accepted', result)
        check = result['result']['checks'][0]
        self.assertFalse(check['blocking'])
        self.assertEqual(check['diagnostics'][0]['id'], 'unusedVariable')

    def test_dynamic_category_override_can_make_ub_advisory(self):
        source = '#include <iostream>\nint main(){int x;std::cin>>x;volatile int y=x+1;std::cout<<42;}'
        problem = self.policy_problem(dynamic_policy='required', diagnostic_policies={'dynamic:undefined':'warning'},
            tests=[{'label':'hidden','input':'2147483647','expected':'42','is_sample':False}])
        result = self.judge.judge(source, problem, 'judge')
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(result['result']['checks'][0]['diagnostics'][0]['id'], 'undefined')
