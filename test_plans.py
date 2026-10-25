"""Sequence validation tests; these never connect to ROS or a model."""
import unittest
from local_client import validate_plan, absolute_plan, reference_context, resume_reference, action_reference, InfeasibleTimeScale
import json
from pathlib import Path
import tempfile
from motion_policy import DEMO_LIMITS, MAX_STEP


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        names = [f'crane_plus_joint{i}' for i in range(1, 5)] + ['crane_plus_joint_hand']
        self.state = {'positions': {j: 0 for j in names},
                      'demo_limits': {j: 0.15 for j in names}, 'max_step_radian': 0.15,
                      'limits': {j: {'lower': -1, 'upper': 1} for j in names}}

    def test_expanded_arm_range_keeps_gripper_and_step_limits(self):
        self.state.update(demo_limits=dict(DEMO_LIMITS), max_step_radian=MAX_STEP)
        validate_plan(self.plan({'crane_plus_joint2': 0.35}, {'crane_plus_joint2': 0}), self.state)
        for targets in [({'crane_plus_joint2': 0.45},), ({'crane_plus_joint_hand': 0.2},),
                        ({'crane_plus_joint2': -0.35}, {'crane_plus_joint2': 0.35})]:
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                validate_plan(self.plan(*targets), self.state)

    def plan(self, *targets):
        return {'steps': [{'targets': t, 'duration': 2} for t in targets]}

    def test_small_sequence(self):
        validate_plan(self.plan({'crane_plus_joint1': 0.1}, {'crane_plus_joint1': 0}), self.state)

    def test_bent_pose_elbow_move_and_unrequested_base_reversal(self):
        self.state.update(demo_limits=dict(DEMO_LIMITS), max_step_radian=MAX_STEP)
        self.state['positions'].update(crane_plus_joint1=-0.293611, crane_plus_joint2=-0.303433)
        validate_plan(self.plan({'crane_plus_joint3': 0.3}), self.state)
        with self.assertRaisesRegex(ValueError, r'Step 1, crane_plus_joint1:.*maximum 0.4 rad'):
            validate_plan(self.plan({'crane_plus_joint1': 0.156389}), self.state)

    def test_cumulative_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan(self.plan({'crane_plus_joint1': 0.1}, {'crane_plus_joint1': 0.2}), self.state)

    def test_large_reversal_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan(self.plan({'crane_plus_joint1': 0.1}, {'crane_plus_joint1': -0.1}), self.state)

    def test_stationary_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan(self.plan({'crane_plus_joint1': 0}, {'crane_plus_joint1': 0}), self.state)

    def test_tiny_fake_motion_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan(self.plan({'crane_plus_joint1': 0.005000000000000004}), self.state)

    def test_original_pose_and_replay_have_distinct_reference_frames(self):
        original = dict(self.state['positions'])
        previous = {'instruction': 'move', 'executed': True, 'start_positions': original,
                    'plan': self.plan({'crane_plus_joint1': -0.3})}
        previous['plan']['steps'][0]['duration'] = 5
        self.state['positions']['crane_plus_joint1'] = -0.3
        context = reference_context(self.state, original, previous)
        self.assertIn('original', context['available_references'])
        replay = context['previous_action']['steps'][0]
        self.assertEqual(replay['reference'], 'previous_step_1')
        self.assertEqual(replay['duration_seconds'], 5)
        self.assertEqual(replay['half_duration_seconds'], 2.5)
        relative = {'explanation': 'restore and replay', 'steps': [
            {'reference': 'original', 'offsets': {'crane_plus_joint1': 0}, 'duration_seconds': 3},
            {'reference': 'previous_step_1', 'offsets': {'crane_plus_joint1': 0}, 'duration_seconds': 2.5}]}
        resolved = absolute_plan(relative, self.state, original, previous)
        self.assertEqual(resolved['steps'][0]['targets']['crane_plus_joint1'], 0)
        self.assertEqual(resolved['steps'][1]['targets']['crane_plus_joint1'], -0.3)
        self.assertEqual(original['crane_plus_joint1'], 0)

    def test_resume_ignores_later_tiny_motion_and_preview(self):
        good = {'instruction': 'wide action', 'executed': True, 'state': self.state,
                'plan': self.plan({'crane_plus_joint1': 0.1}), 'results': [{'success': True}]}
        tiny = {**good, 'instruction': 'fake return', 'plan': self.plan({'crane_plus_joint1': 0.005})}
        preview = {**good, 'instruction': 'preview', 'executed': False}
        partial = {**good, 'instruction': 'incomplete', 'plan': self.plan({'crane_plus_joint1': 0.1}, {'crane_plus_joint1': 0})}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runs.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in [good, tiny, preview, partial]), encoding='utf-8')
            original, action = resume_reference(path)
        self.assertEqual(action['instruction'], 'wide action')
        self.assertEqual(original, self.state['positions'])

    def test_legacy_actions_are_not_marked_contract_verified(self):
        record = {'instruction': 'old action', 'executed': True, 'state': self.state,
                  'plan': self.plan({'crane_plus_joint1': 0.1}), 'audit': None}
        self.assertFalse(action_reference(record)['contract_verified'])
        record.update(contract_version=2, audit={'accepted': True})
        self.assertTrue(action_reference(record)['contract_verified'])
        record['audit']['accepted'] = False
        self.assertFalse(action_reference(record)['contract_verified'])

    def test_return_then_repeat_preserves_targets_and_halves_time(self):
        original = dict(self.state['positions'])
        previous = {'instruction': 'wide arm then gripper', 'executed': True, 'start_positions': original,
                    'plan': {'steps': [
                        {'targets': {'crane_plus_joint1': -0.3, 'crane_plus_joint2': -0.3}, 'duration': 5},
                        {'targets': {'crane_plus_joint_hand': 0.1}, 'duration': 2}]}}
        self.state['positions'].update(crane_plus_joint1=-0.3, crane_plus_joint2=-0.3, crane_plus_joint_hand=0.1)
        self.state.update(demo_limits=dict(DEMO_LIMITS), max_step_radian=MAX_STEP)
        request = {'explanation': 'restore and repeat faster', 'repeat_previous': {
            'return_to_original': True, 'time_scale': 0.5, 'amplitude_scale': 1, 'restore_duration_seconds': 3}}
        resolved = absolute_plan(request, self.state, original, previous)
        self.assertEqual(len(resolved['steps']), 4)
        self.assertEqual(resolved['steps'][0]['targets']['crane_plus_joint1'], 0)
        self.assertEqual(resolved['steps'][1]['targets'], {'crane_plus_joint_hand': 0})
        self.assertEqual(resolved['steps'][2]['targets'], previous['plan']['steps'][0]['targets'])
        self.assertEqual(resolved['steps'][3]['targets'], previous['plan']['steps'][1]['targets'])
        self.assertEqual([step['duration'] for step in resolved['steps']], [3, 3, 2.5, 1])
        validate_plan(resolved, self.state)
        record = {'instruction': 'repeat faster', 'executed': True, 'state': self.state,
                  'original_pose': original, 'plan': resolved, 'relative_plan': request,
                  'results': [{'success': True}] * 4}
        # Existing logs must resume only the two motion steps, excluding restoration.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runs.jsonl'
            path.write_text(json.dumps(record), encoding='utf-8')
            _, replay = resume_reference(path)
        self.assertEqual(len(replay['plan']['steps']), 2)
        self.assertEqual(replay['start_positions'], original)
        with self.assertRaisesRegex(InfeasibleTimeScale, r'1 s x 0.5 = 0.5 s'):
            absolute_plan(request, self.state, original, replay)
        request['repeat_previous']['time_scale'] = 1
        same_speed = absolute_plan(request, self.state, original, replay)
        self.assertEqual([s['duration'] for s in same_speed['steps']], [3, 3, 2.5, 1])
        self.assertEqual(len(action_reference({**record, 'plan': same_speed})['plan']['steps']), 2)
        request['repeat_previous']['time_scale'] = 0.1
        with self.assertRaises(ValueError):
            validate_plan(absolute_plan(request, self.state, original, previous), self.state)

    def test_offsets_use_initial_pose_for_every_step(self):
        self.state['positions']['crane_plus_joint1'] = 0.03
        relative = {'explanation': 'move and return', 'steps': [
            {'offsets': {'crane_plus_joint1': 0.05}, 'duration_seconds': 2},
            {'offsets': {'crane_plus_joint1': 0}, 'duration_seconds': 2}]}
        resolved = absolute_plan(relative, self.state)
        self.assertAlmostEqual(resolved['steps'][0]['targets']['crane_plus_joint1'], 0.08)
        self.assertEqual(resolved['steps'][1]['targets']['crane_plus_joint1'], 0.03)
        self.assertEqual(self.state['positions']['crane_plus_joint1'], 0.03)
        validate_plan(resolved, self.state)

    def test_nonfinite_unknown_and_mixed_are_rejected(self):
        for targets in [{'crane_plus_joint1': float('nan')}, {'typo': 0},
                        {'crane_plus_joint1': 0.01, 'crane_plus_joint_hand': 0.01}]:
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                validate_plan(self.plan(targets), self.state)


if __name__ == '__main__':
    unittest.main()
