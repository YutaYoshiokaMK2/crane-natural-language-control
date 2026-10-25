"""Regression: rejected LLM output must not end an interactive session."""
from argparse import Namespace
from contextlib import asynccontextmanager, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jsonschema import validate, ValidationError
import local_client
from motion_policy import ARM, HAND, DEMO_LIMITS, MAX_STEP


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_speed_only_repeat_cannot_change_amplitude(self):
        request = {'explanation': 'faster repeat', 'repeat_previous': {
            'return_to_original': True, 'time_scale': 0.5, 'amplitude_scale': 0.5,
            'restore_duration_seconds': 3}}
        contract = {'operation': 'repeat_previous', 'stages': [], 'reason': 'half time',
                    'repeat': {'return_to_original': True, 'time_scale': 0.5, 'amplitude_scale': 1}}
        schema = local_client.output_schema(contract)
        with self.assertRaises(ValidationError):
            validate(request, schema)
        request['repeat_previous']['amplitude_scale'] = 1
        validate(request, schema)
        request['repeat_previous']['time_scale'] = 1
        with self.assertRaises(ValidationError):
            validate(request, schema)

    def test_schema_requires_separate_groups(self):
        mixed = {'explanation': 'invalid', 'steps': [{'reference': 'current', 'offsets': {ARM[0]: 0.1, HAND: 0}, 'duration_seconds': 2}]}
        with self.assertRaises(ValidationError):
            validate(mixed, local_client.SCHEMA)
        for offsets in [{ARM[0]: 0.1, ARM[1]: 0.1}, {HAND: -0.075}]:
            validate({'explanation': 'valid', 'steps': [{'reference': 'current', 'offsets': offsets, 'duration_seconds': 2}]}, local_client.SCHEMA)

    async def test_rejection_then_next_instruction_executes(self):
        state = {'positions': {j: 0 for j in ARM + [HAND]},
                 'limits': {j: {'lower': -1, 'upper': 1} for j in ARM + [HAND]},
                 'demo_limits': DEMO_LIMITS, 'max_step_radian': MAX_STEP}
        calls = []

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def initialize(self): pass
            async def call_tool(self, name, data):
                calls.append((name, data))
                payload = state if name == 'get_joint_state' else {'success': True}
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(payload))])

        @asynccontextmanager
        async def transport(params):
            yield None, None

        invalid = {'explanation': 'mixed', 'steps': [{'offsets': {ARM[0]: 0.1, HAND: 0.1}, 'duration_seconds': 2}]}
        valid = {'explanation': 'move', 'steps': [{'stage': 1, 'reference': 'current', 'offsets': {ARM[0]: 0.1}, 'duration_seconds': 2}]}
        contract = {'operation': 'new_action', 'reason': 'base only', 'repeat': None, 'stages': [
            {'joints': [ARM[0]], 'kind': 'move', 'evidence': 'base instruction', 'duration_seconds': None}]}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            args = Namespace(url='http://127.0.0.1:11434', model='qwen2.5:3b', docker=False,
                             prompt=None, think=False, execute=True, log=Path(directory) / 'runs.jsonl')
            with patch.object(local_client, 'stdio_client', transport), \
                 patch.object(local_client, 'ClientSession', lambda *args: Session()), \
                 patch.object(local_client, 'classify_instruction', return_value=contract), \
                 patch.object(local_client, 'audit_instruction', return_value={'accepted': True, 'reason': 'matches'}), \
                 patch.object(local_client, 'plan_request', side_effect=[(invalid, {})] * 3 + [(valid, {})]), \
                 patch('builtins.input', side_effect=['bad base instruction', 'valid base instruction', 'exit']), \
                 redirect_stdout(output):
                await local_client.main(args)
            records = [json.loads(line) for line in args.log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(records), 4)
        self.assertTrue(all(not r['executed'] for r in records[:3]))
        self.assertTrue(records[3]['results'][0]['success'])
        moves = [data for name, data in calls if name == 'move_joints']
        self.assertEqual(moves, [{'targets': {ARM[0]: 0.1}, 'duration': 2}])
        self.assertIn('No motion was executed', output.getvalue())

    async def test_interpretation_rejection_never_calls_motion_planner_or_motion_tools(self):
        calls = []
        state = {'positions': {j: 0 for j in ARM + [HAND]},
                 'limits': {j: {'lower': -1, 'upper': 1} for j in ARM + [HAND]}}

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def initialize(self): pass
            async def call_tool(self, name, data):
                calls.append(name)
                return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(state))])

        @asynccontextmanager
        async def transport(params):
            yield None, None

        good_shape_bad_meaning = {'operation': 'new_action', 'reason': 'wrong interpretation', 'repeat': None,
            'stages': [{'joints': [ARM[0]], 'kind': 'move', 'evidence': 'Move wrist; keep base still', 'duration_seconds': None}]}
        ambiguous = {'operation': 'clarify', 'reason': 'which joint?', 'stages': [], 'repeat': None}
        for contract in [good_shape_bad_meaning, ambiguous, None]:
            calls.clear()
            with self.subTest(contract=contract), tempfile.TemporaryDirectory() as directory:
                args = Namespace(url='http://127.0.0.1:11434', model='qwen2.5:3b', docker=False,
                    prompt='Move wrist; keep base still', think=False, execute=True, log=Path(directory) / 'runs.jsonl')
                with patch.object(local_client, 'stdio_client', transport), \
                     patch.object(local_client, 'ClientSession', lambda *args: Session()), \
                     patch.object(local_client, 'classify_instruction', return_value=contract) as interpret, \
                     patch.object(local_client, 'audit_instruction', return_value={'accepted': False, 'reason': 'Extra base movement'}) as audit, \
                     patch.object(local_client, 'plan_request') as planner, redirect_stdout(io.StringIO()):
                    result = await local_client.main(args)
                self.assertEqual(result, 1)
                self.assertEqual(calls, ['get_joint_state'])
                interpret.assert_called_once()
                planner.assert_not_called()
                self.assertEqual(audit.call_count, int(contract is not None and contract['operation'] != 'clarify'))
                records = [json.loads(line) for line in args.log.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(len(records), 1)
                self.assertFalse(records[0]['executed'])

    async def test_declined_review_or_changed_pose_never_moves(self):
        contract = {'operation': 'new_action', 'reason': 'base only', 'repeat': None, 'stages': [
            {'joints': [ARM[0]], 'kind': 'move', 'evidence': 'Move base', 'duration_seconds': None}]}
        relative = {'explanation': 'base', 'steps': [
            {'stage': 1, 'reference': 'current', 'offsets': {ARM[0]: 0.1}, 'duration_seconds': 3}]}
        for approve in [False, True]:
            calls = []
            state = {'positions': {j: 0 for j in ARM + [HAND]},
                     'limits': {j: {'lower': -1, 'upper': 1} for j in ARM + [HAND]}}

            class Session:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                async def initialize(self): pass
                async def call_tool(self, name, data):
                    calls.append(name)
                    if name != 'get_joint_state':
                        raise AssertionError('No motion tool may be called')
                    if len(calls) == 2:
                        state['positions'][ARM[1]] = 0.02
                    return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(state))])

            @asynccontextmanager
            async def transport(params):
                yield None, None

            with self.subTest(approve=approve), tempfile.TemporaryDirectory() as directory:
                args = Namespace(url='http://127.0.0.1:11434', model='qwen2.5:3b', docker=False,
                    prompt='Move base', think=False, execute=True, confirm_plan=True, log=Path(directory) / 'runs.jsonl')
                with patch.object(local_client, 'stdio_client', transport), \
                     patch.object(local_client, 'ClientSession', lambda *args: Session()), \
                     patch.object(local_client, 'classify_instruction', return_value=contract), \
                     patch.object(local_client, 'audit_instruction', return_value={'accepted': True, 'reason': 'matches'}), \
                     patch.object(local_client, 'plan_request', return_value=(relative, {})), \
                     patch('builtins.input', return_value='y' if approve else 'n'), redirect_stdout(io.StringIO()):
                    result = await local_client.main(args)
                self.assertEqual(result, 1)
                self.assertEqual(calls, ['get_joint_state'] * (2 if approve else 1))
                record = json.loads(args.log.read_text(encoding='utf-8'))
                self.assertFalse(record['executed'])

if __name__ == '__main__':
    unittest.main()
