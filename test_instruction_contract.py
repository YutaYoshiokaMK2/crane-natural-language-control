"""Permissions, sequence and time are checked independently of the planner."""
import copy
import unittest
from jsonschema import ValidationError
from instruction_contract import (validate_contract, validate_plan_contract, validate_resolved_contract,
                                  planning_stages, source_candidates, InstructionRejected)
from local_client import JOINT_SCHEMA, REPEAT_SCHEMA, absolute_plan, validate_plan
from motion_policy import ARM, HAND


def contract_for(joints, evidence='move', kind='move', duration=None):
    return {'operation': 'new_action', 'reason': 'requested motion', 'repeat': None,
            'stages': [{'joints': joints, 'kind': kind, 'evidence': evidence, 'duration_seconds': duration}]}


def step(joints, stage=1, reference='current', duration=3, offset=0.1):
    return {'stage': stage, 'reference': reference, 'offsets': {j: offset for j in joints},
            'duration_seconds': duration}


class InstructionContractTests(unittest.TestCase):
    def test_absolute_angles_resolve_sign_in_python_and_accept_already_reached_joints(self):
        state = {'positions': dict(zip(ARM + [HAND], [-0.29, -0.31, 0, 0, 0.1])),
                 'limits': {j: {'lower': -1, 'upper': 1} for j in ARM + [HAND]}}
        text = 'Set the base, shoulder, elbow, and wrist joints to 0 radians over 4 seconds.'
        contract = contract_for(ARM, evidence=text, kind='set_absolute', duration=4)
        contract['stages'][0]['absolute_targets'] = {j: 0 for j in ARM}
        validate_contract(contract, text)
        relative = {'explanation': 'absolute zero', 'steps': [{
            'stage': 1, 'reference': 'absolute', 'targets': {j: 0 for j in ARM},
            'duration_seconds': 4}]}
        validate_plan_contract(relative, contract, JOINT_SCHEMA, REPEAT_SCHEMA, state)
        resolved = absolute_plan(relative, state)
        self.assertEqual(resolved['steps'][0]['targets'], {j: 0 for j in ARM})
        validate_resolved_contract(resolved, contract, state)
        validate_plan(resolved, state, allow_noop=True)
        bad = copy.deepcopy(relative)
        bad['steps'][0]['targets'][ARM[0]] = -0.58
        with self.assertRaises(ValidationError):
            validate_plan_contract(bad, contract, JOINT_SCHEMA, REPEAT_SCHEMA, state)
        contract['stages'][0]['kind'] = 'move'
        with self.assertRaises(InstructionRejected):
            validate_contract(contract, text)

    def test_nonzero_absolute_angle_is_not_a_zero_pose_special_case(self):
        text = 'Set base to -0.1 radians'
        contract = contract_for([ARM[0]], evidence=text, kind='set_absolute')
        contract['stages'][0]['absolute_targets'] = {ARM[0]: -0.1}
        validate_contract(contract, text)
        contract['stages'][0]['absolute_targets'][ARM[0]] = 0
        with self.assertRaises(InstructionRejected):
            validate_contract(contract, text)

    def check(self, plan, contract):
        validate_plan_contract({'explanation': 'test', 'steps': plan}, contract, JOINT_SCHEMA, REPEAT_SCHEMA)

    def test_all_joints_have_same_permissions_and_no_implicit_restore(self):
        for joint in ARM + [HAND]:
            with self.subTest(joint=joint):
                contract = contract_for([joint])
                contract['stages'][0]['evidence'] = joint
                validate_contract(contract, 'move ' + joint)
                self.check([step([joint])], contract)
                for other in set(ARM + [HAND]) - {joint}:
                    with self.assertRaises(ValidationError):
                        self.check([step([joint, other])], contract)
                with self.assertRaises(ValidationError):
                    self.check([step([joint], reference='original')], contract)
                with self.assertRaises(ValidationError):
                    self.check([step([joint]), step([joint], offset=0)], contract)

    def test_stage_order_completeness_and_time(self):
        contract = contract_for(ARM[:2], evidence='move base and shoulder', duration=5)
        contract['stages'].append({'joints': [HAND], 'kind': 'move', 'evidence': 'close hand', 'duration_seconds': 2})
        validate_contract(contract, 'move base and shoulder then close hand')
        good = [step(ARM[:2], duration=5), step([HAND], stage=2, duration=2)]
        self.check(good, contract)
        for bad in [good[::-1], good[:1], [good[0], good[0]],
                    [step(ARM[:2], duration=2), good[1]],
                    [step([ARM[0]], duration=5), good[1]]]:
            with self.subTest(plan=bad), self.assertRaises((ValueError, ValidationError)):
                self.check(bad, contract)

    def test_restore_is_an_explicit_stage_with_zero_offsets(self):
        contract = contract_for(ARM, evidence='return', kind='restore_original')
        self.check([step(ARM, reference='original', offset=0)], contract)
        with self.assertRaises(ValidationError):
            self.check([step(ARM, reference='original', offset=0.1)], contract)
        contract['stages'][0]['kind'] = 'return_start'
        self.check([step(ARM, offset=0)], contract)

    def test_repeat_cannot_add_restore_or_change_unrequested_parameters(self):
        contract = {'operation': 'repeat_previous', 'reason': 'half time', 'stages': [],
                    'repeat': {'return_to_original': False, 'time_scale': 0.5, 'amplitude_scale': 1}}
        validate_contract(contract, 'repeat in half time')
        plan = {'explanation': 'repeat', 'repeat_previous': {**contract['repeat'], 'restore_duration_seconds': 3}}
        validate_plan_contract(plan, contract, JOINT_SCHEMA, REPEAT_SCHEMA)
        for key, value in [('return_to_original', True), ('time_scale', 1), ('amplitude_scale', 0.5)]:
            bad = copy.deepcopy(plan)
            bad['repeat_previous'][key] = value
            with self.assertRaises(ValidationError):
                validate_plan_contract(bad, contract, JOINT_SCHEMA, REPEAT_SCHEMA)

    def test_ungrounded_ambiguous_and_mixed_controller_contracts_stop(self):
        for contract in [contract_for([ARM[0]], evidence='invented'), contract_for([ARM[0], HAND]),
                         {'operation': 'clarify', 'reason': 'which joint?', 'stages': [], 'repeat': None}]:
            with self.assertRaises(InstructionRejected):
                validate_contract(contract, 'move')

    def test_entity_grounding_rejects_invented_joints_for_every_joint(self):
        for name, joint in zip(['base', 'shoulder', 'elbow', 'wrist', 'gripper'], ARM + [HAND]):
            text = 'Move only the ' + name
            validate_contract(contract_for([joint], evidence=text), text)
            for other in set(ARM + [HAND]) - {joint}:
                with self.subTest(name=name, other=other), self.assertRaises(InstructionRejected):
                    validate_contract(contract_for([joint, other], evidence=text), text)

    def test_each_requested_joint_must_actually_move(self):
        contract = contract_for([ARM[0]])
        contract['stages'].append({'joints': [HAND], 'kind': 'move', 'evidence': 'close hand', 'duration_seconds': None})
        state = {'positions': {j: 0 for j in ARM + [HAND]}}
        plan = {'steps': [{'targets': {ARM[0]: 0.1}, 'duration': 3},
                          {'targets': {HAND: 0}, 'duration': 2}]}
        with self.assertRaisesRegex(ValueError, 'Stage 2'):
            validate_resolved_contract(plan, contract, state)
        plan['steps'][1]['targets'][HAND] = 0.05
        validate_resolved_contract(plan, contract, state)

    def test_return_and_repeat_cannot_be_invented_without_source_wording(self):
        contract = contract_for([ARM[2]], evidence='From current posture bend elbow', kind='restore_original')
        with self.assertRaises(InstructionRejected):
            validate_contract(contract, 'From current posture bend elbow')
        contract = {'operation': 'repeat_previous', 'reason': 'invented', 'stages': [],
                    'repeat': {'return_to_original': False, 'time_scale': 1, 'amplitude_scale': 1}}
        with self.assertRaises(InstructionRejected):
            validate_contract(contract, 'Move wrist')

    def test_explicit_whole_posture_restore_expands_without_new_permissions(self):
        text = 'Return to original posture'
        contract = contract_for(ARM + [HAND], evidence=text, kind='restore_original')
        validate_contract(contract, text)
        self.assertEqual([stage['joints'] for stage in planning_stages(contract)], [ARM, [HAND]])
        self.check([step(ARM, reference='original', offset=0),
                    step([HAND], stage=2, reference='original', offset=0)], contract)
        contract['stages'][0]['duration_seconds'] = 5
        with self.assertRaises(InstructionRejected):
            validate_contract(contract, text + ' in 5 seconds')

    def test_generation_candidates_are_grounded_for_all_joints(self):
        for name, joint in zip(['base', 'shoulder', 'elbow', 'wrist', 'gripper'], ARM + [HAND]):
            joints, operations = source_candidates('Move ' + name)
            self.assertEqual(joints, {joint})
            self.assertIn('new_action', operations)
            self.assertNotIn('repeat_previous', operations)
        joints, operations = source_candidates('Repeat the same action in half the time')
        self.assertFalse(joints)
        self.assertIn('repeat_previous', operations)
        self.assertNotIn('new_action', operations)
        self.assertEqual(source_candidates('Do something impressive.')[1], ['clarify', 'unsupported'])


if __name__ == '__main__':
    unittest.main()
