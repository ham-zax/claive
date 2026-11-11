import hermetic  # noqa: F401
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from claivelib.engines.pi import PiEngine


class PiReliabilityChecks(unittest.TestCase):
    def start(self, engine, command, tool_id='call', tool='bash'):
        event = {'type': 'tool_execution_start', 'toolCallId': tool_id,
                 'toolName': tool, 'args': {'command': command}}
        before = copy.deepcopy(event)
        normalized = engine.normalize_event(event)[0]
        self.assertEqual(event, before)
        self.assertEqual(normalized['command'], command)
        return event

    def end(self, engine, reason=None, tool_id='call', **extra):
        event = {'type': 'tool_execution_end', 'toolCallId': tool_id,
                 'isError': reason is not None,
                 'result': {'content': [{'type': 'text', 'text': reason or 'ok'}]}, **extra}
        before = copy.deepcopy(event)
        normalized = engine.normalize_event(event)[0]
        self.assertEqual(event, before)
        return normalized

    def test_interleaved_calls_keep_commands_on_success_and_error(self):
        engine = PiEngine()
        self.start(engine, 'rg needle file', 'search')
        self.start(engine, 'echo done', 'write')
        completed = self.end(engine, tool_id='write')
        self.assertEqual((completed['type'], completed['command'], completed['tool']),
                         ('tool_completed', 'echo done', 'bash'))
        warning = self.end(engine, 'permission denied', 'search')
        self.assertEqual((warning['type'], warning['command'], warning['tool_id']),
                         ('task_warning', 'rg needle file', 'search'))
        self.assertIsNone(self.end(engine, tool_id='search')['command'])

    def test_call_metadata_is_snapshotted_and_reset_at_turn_and_session(self):
        for reset in ({'type': 'agent_start'}, {'type': 'session'},
                      {'type': 'session', 'id': 'new-session'}):
            with self.subTest(reset=reset):
                engine = PiEngine()
                start = self.start(engine, 'echo original')
                start['args']['command'] = 'echo mutated'
                self.assertEqual(self.end(engine)['command'], 'echo original')
                self.start(engine, 'echo stale')
                engine.normalize_event(reset)
                self.assertIsNone(self.end(engine)['command'])

    def test_missing_ids_are_not_correlated_and_end_args_work(self):
        engine = PiEngine()
        self.start(engine, 'echo stale', None)
        self.assertIsNone(self.end(engine, tool_id=None)['command'])
        completed = self.end(engine, args={'command': 'echo explicit'}, toolName='bash')
        self.assertEqual(completed['command'], 'echo explicit')

    def test_exact_truncation_phrase_keeps_error_metadata(self):
        for tool in ('write', 'edit', 'bash'):
            with self.subTest(tool=tool):
                engine = PiEngine()
                self.start(engine, 'command context', tool=tool)
                reason = 'Error: tool hit the output token limit while generating output'
                warning = self.end(engine, reason)
                self.assertEqual(warning, {
                    'type': 'task_warning', 'native_kind': 'tool_output_truncated',
                    'tool': tool, 'tool_id': 'call', 'command': 'command context',
                    'reason': reason})
        for reason in ('output token limit', 'hit the output token limits', 'output truncated'):
            with self.subTest(reason=reason):
                self.assertEqual(self.end(PiEngine(), reason)['native_kind'], 'tool_execution_end')

    def test_silent_exit_one_probes(self):
        commands = [
            'rg -n "pattern" test/*.mjs',
            'cd /tmp/pi-project && rg -n "pattern" test/*.mjs',
            'grep -n pattern file', 'grep -Eq "a b" file',
            'rg --fixed-strings -- needle file',
            'git diff --quiet', 'git diff --cached --quiet',
            'git diff --quiet --exit-code', 'test -f missing', '[ -d missing ]',
            'test -z nonempty', '[ 1 -eq 2 ]', 'test foo = bar', 'test ""',
        ]
        for command in commands:
            for reason in ('(no output)\n\nCommand exited with code 1', 'Command exited with code 1'):
                with self.subTest(command=command, reason=reason):
                    engine = PiEngine()
                    self.start(engine, command)
                    probe = self.end(engine, reason)
                    self.assertEqual(probe['type'], 'tool_probe')
                    self.assertEqual((probe['command'], probe['reason']), (command, reason))
                    self.assertEqual(probe['native_kind'], 'tool_execution_end')

    def test_ambiguous_commands_and_real_failures_remain_warnings(self):
        commands = [
            'false', 'exit 1', 'bash -c "rg needle file"',
            'rg needle file && false', 'false && rg needle file',
            'cd /tmp && false && rg needle file', 'cd - && rg needle file',
            'rg needle file || false', 'rg needle file; false',
            'rg needle file | cat', 'rg needle file > output',
            'rg "$(false)" file', 'rg `false` file', 'rg needle file &',
            'rg needle file\nfalse', 'rg --pre false needle file',
            'rg --unknown needle file', 'grep -L needle file',
            'git diff', 'git diff --quiet; false', 'git -C missing diff --quiet',
            'test 1 -eq nope', 'test -f missing -a -f other',
            '[ -f missing ] && false', 'test -f *', 'rg "unterminated',
        ]
        for command in commands:
            with self.subTest(command=command):
                engine = PiEngine()
                self.start(engine, command)
                self.assertEqual(self.end(engine, '(no output)\n\nCommand exited with code 1')['type'],
                                 'task_warning')
        for reason in ('permission denied\nCommand exited with code 1',
                       '(no output)\n\nCommand exited with code 2',
                       '(no output)\n\nCommand exited with code 10',
                       '(no output)\n\nCommand exited with code 1\nother error'):
            with self.subTest(reason=reason):
                engine = PiEngine()
                self.start(engine, 'rg needle file')
                self.assertEqual(self.end(engine, reason)['type'], 'task_warning')
        engine = PiEngine()
        self.start(engine, 'rg needle file', tool='custom')
        self.assertEqual(self.end(engine, 'Command exited with code 1')['type'], 'task_warning')


if __name__ == '__main__':
    unittest.main()
