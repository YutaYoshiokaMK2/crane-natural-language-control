"""Instruction permissions, separate from numeric trajectory planning.

The interpreter defines an ordered contract. The planner cannot widen it.
Natural-language interpretation is still fallible; this is not a semantic proof.
"""
import copy
import re
from jsonschema import validate
from motion_policy import ARM, HAND, DEMO_LIMITS

JOINTS = ARM + [HAND]
JOINT_NAMES = dict(zip(['base', 'shoulder', 'elbow', 'wrist', 'gripper'], JOINTS))
JOINT_ALIASES = {
    'base': r'\bbase\b|根元|台座',
    'shoulder': r'\bshoulders?\b|肩',
    'elbow': r'\belbows?\b|肘|ひじ',
    'wrist': r'\bwrists?\b|手首',
    'gripper': r'\b(grippers?|hands?)\b|グリッパ|ハンド',
}


def mentioned_joints(text):
    """Ground entity names for every joint; this never chooses angles or motions."""
    text = text.lower()
    joints = {JOINT_NAMES[name] for name, pattern in JOINT_ALIASES.items() if re.search(pattern, text)}
    joints.update(joint for joint in JOINTS if re.search(r'(?<!\w)' + re.escape(joint) + r'(?!\w)', text))
    if re.search(r'\barms?\b|アーム', text):
        joints.update(ARM)
    return joints


def references_previous(text):
    return bool(re.search(r'\b(repeat|same|again|previous)\b|同じ|もう一度|前回|さっき|繰り返', text.lower()))


def evidence_candidates(instruction):
    """Offer verbatim sentence spans rather than letting the model splice quotes.

    Keep the whole instruction as a fallback for cross-sentence references.
    Punctuation is preserved, and decimal points do not split a sentence.
    """
    candidates = [instruction]
    sentences = re.split(r'(?<=[.!?。．！？])\s+', instruction)
    candidates.extend(sentence.strip() for sentence in sentences if sentence.strip())
    return list(dict.fromkeys(candidates))


def source_candidates(text):
    """Upper bound only: mentioned keep-still joints still need semantic review."""
    joints = mentioned_joints(text)
    if re.search(r'\b(return|restore|reset)\b|戻|復帰', text.lower()) and re.search(r'\bposture\b|姿勢', text.lower()):
        joints.update(JOINTS)
    operations = []
    if joints:
        operations.append('new_action')
    if references_previous(text):
        operations.append('repeat_previous')
    return joints, operations + ['clarify', 'unsupported']
CONTRACT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'operation': {'enum': ['new_action', 'repeat_previous', 'clarify', 'unsupported']},
        'reason': {'type': 'string'},
        'stages': {'type': 'array', 'maxItems': 8, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'joints': {'type': 'array', 'items': {'enum': JOINTS}, 'minItems': 1, 'uniqueItems': True},
                'kind': {'enum': ['move', 'set_absolute', 'return_start', 'restore_original']},
                'absolute_targets': {'type': 'object', 'properties': {j: {'type': 'number'} for j in JOINTS},
                                     'additionalProperties': False},
                'evidence': {'type': 'string', 'minLength': 1},
                'duration_seconds': {'type': ['number', 'null'], 'minimum': 1, 'maximum': 15}},
            'required': ['joints', 'kind', 'evidence', 'duration_seconds']}},
        'repeat': {'type': ['object', 'null'], 'additionalProperties': False,
            'properties': {
                'return_to_original': {'type': 'boolean'},
                'time_scale': {'type': 'number', 'minimum': 0.1, 'maximum': 4},
                'amplitude_scale': {'type': 'number', 'minimum': 0.1, 'maximum': 2}},
            'required': ['return_to_original', 'time_scale', 'amplitude_scale']}},
    'required': ['operation', 'reason', 'stages', 'repeat']}

INTERPRETER_SCHEMA = copy.deepcopy(CONTRACT_SCHEMA)
INTERPRETER_SCHEMA['properties']['stages']['items']['properties']['joints']['items'] = {'enum': list(JOINT_NAMES)}
INTERPRETER_SCHEMA['properties']['stages']['items']['properties']['absolute_targets']['properties'] = {
    name: {'type': 'number'} for name in JOINT_NAMES}
INTERPRETER_SCHEMA['properties']['stages']['items']['required'].append('absolute_targets')


def readable_contract(contract):
    result = copy.deepcopy(contract)
    names = {value: key for key, value in JOINT_NAMES.items()}
    for stage in result['stages']:
        stage['joints'] = [names[joint] for joint in stage['joints']]
        if 'absolute_targets' in stage:
            stage['absolute_targets'] = {names[j]: value for j, value in stage['absolute_targets'].items()}
    return result


INTERPRETER_SYSTEM = '''Interpret robot instructions into permissions, NOT joint angles or a motion plan.
Use ONLY the user's current instruction. Never add setup, neutral posture, restoration, a demonstration,
return motion, repetition or movement of neighbouring joints unless explicitly requested.
Use joint NAMES only: base, shoulder, elbow, wrist, gripper. Hand means gripper. Elbows means elbow.
Japanese equivalents apply. Do not output ROS joint numbers.
"Keep X still" and "do not move X" grant NO permission to move X. Omitted joints must stay still.
new_action: stages in requested order, one stage per requested motion. Simultaneous arm joints share
one stage; sequential motions use separate stages. Each stage's joints are exactly those to move.
Each evidence is an EXACT verbatim substring of the instruction supporting that stage, including its action.
Copy the words exactly, including articles such as "the"; do not paraphrase evidence.
Select evidence from the supplied schema's enum. Never combine or shorten the offered quotations.
kind move: from the measured current pose (default). return_start: ONLY explicitly asked to return to
this request's starting pose. restore_original: ONLY explicitly asked to return to saved original/initial posture.
kind set_absolute: an explicit absolute angle, e.g. "Set base and shoulder to 0 radians".
For set_absolute, absolute_targets MUST contain every requested joint and its absolute angle in radians.
Do NOT turn absolute targets into offsets. For other kinds use absolute_targets={}.
Restore a whole posture with ONE logical stage containing base, shoulder, elbow, wrist, gripper.
Python will split an untimed posture restoration across the controllers; do not do this yourself.
duration_seconds: the explicitly requested time for that stage, otherwise null; do not invent times.
Do not invent a second movement after a single "bend" or "move" request. A new elbow move is one stage.
repeat_previous: ONLY when repeating/modifying the previous action, stages=[], repeat has requested
return_to_original (default false), time_scale and amplitude_scale (both default 1).
Half the time: time_scale=0.5, amplitude_scale=1. Half amplitude: amplitude_scale=0.5, time_scale=1.
For new_action repeat=null. Never carry over changes from an earlier instruction.
If joints, ordering, or timing cannot be determined without inventing actions, use clarify, stages=[], repeat=null.
For vision, grasping, IK or obstacle avoidance use unsupported. Simultaneous arm AND hand is unsupported
(separate controllers currently execute sequentially). Do not silently serialize a simultaneous request.
Output only the contract JSON. Do not generate an explanation or repeat the routing reason.'''

AUDITOR_SYSTEM = '''Independently compare a robot instruction with an extracted permission contract.
Do not plan robot motions. Reject any extra/missing joint, invented setup or restoration, invented return,
invented repetition, wrong order, wrong specified duration, or unintended amplitude/time scaling.
Joint names in the contract are base, shoulder, elbow, wrist, gripper. Hand means gripper.
An explicit "set ... to X radians" MUST use set_absolute and absolute_targets=X for the named joints.
Reject move stages that discard an explicit absolute target, even if the reason mentions the right number.
Keep-still joints must NEVER appear in moving joints. Other joints stay still unless explicitly requested.
Only stage.moving_joints change angles. stage.held_joints hold their current angles during that stage.
Holding a joint does not mean it belongs in moving_joints. The original extraction was checked separately;
review only the concrete moving/held sets against the instruction.
move stages let the later planner choose angles for qualitative requests such as large/clearly visible;
do not reject a qualitative request merely because its numeric angle or unspecified duration is not chosen yet.
Defaults: current posture, no return, no repeat, no time/amplitude change. A single motion must not become a cycle.
Whole original posture restoration includes all four arm joints and the gripper in one logical stage;
the backend splits its execution across controllers. This grouping is correct for a restoration.
Separate sequential actions MUST have separate stages. For "first base, then gripper", two stages are correct;
do not reject them for being separate. Simultaneous shoulder and elbow share one stage.
For repeat_previous, stages MUST be empty: the backend reuses the previous action's stored steps.
"same action" and "previous action" refer to the last action. Do not demand its full steps in the contract.
The backend separately checks that a previous action exists. Review only the requested repeat parameters.
Compare the action requested for each joint, not merely whether its name is mentioned. Reject ambiguous contracts.
Return ONLY five boolean checks, no explanation:
moving_joints_match: the stages move exactly the joints the instruction asks to move.
held_joints_match: held_joints implements the instruction's unchanged joints.
sequence_matches: stages match the requested order and simultaneous grouping.
references_match: current/start/original and repeat/restore choices match the instruction.
parameters_match: explicitly specified numeric targets, times and repeat scales match; unspecified values
may remain unchosen for later planning. Qualitative large/visible requests are passed to the planner.
Set a check to false for a mismatch or ambiguity. Do not require a moving joint to also appear in held_joints.
For clarify/unsupported, accept only if the instruction truly needs clarification or unsupported capabilities.
Simultaneous arm AND gripper movement is NOT supported by this backend (sequential controllers only).
Never approve an unsupported request by claiming the backend has capabilities not stated here.
This is an independent review: the contract may be wrong; do not trust its reason.'''

AUDIT_CHECKS = ('moving_joints_match', 'held_joints_match', 'sequence_matches', 'references_match', 'parameters_match')
AUDIT_SCHEMA = {'type': 'object', 'properties': {key: {'type': 'boolean'} for key in AUDIT_CHECKS},
                'required': list(AUDIT_CHECKS), 'additionalProperties': False}

class InstructionRejected(ValueError):
    """Do not retry motion planning by broadening instruction permissions."""


def absolute_angles(text):
    """Ground explicit 'to N radians' values; time values cannot match."""
    return [float(value) for value in re.findall(
        r'\bto\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:radians?|rad)\b', text.lower())]


def validate_contract(contract, instruction):
    validate(contract, CONTRACT_SCHEMA)
    operation = contract['operation']
    if operation in ('clarify', 'unsupported'):
        raise InstructionRejected(contract['reason'])
    if operation == 'repeat_previous':
        if contract['stages'] or contract['repeat'] is None:
            raise InstructionRejected('Repeat contract must specify repeat parameters, without new stages.')
        if not references_previous(instruction):
            raise InstructionRejected('No source wording supports reusing the previous action.')
        return
    if not contract['stages'] or contract['repeat'] is not None:
        raise InstructionRejected('New action needs explicit motion stages and no repeat parameters.')
    earlier_joints = set()
    for stage in contract['stages']:
        if stage['evidence'] not in instruction:
            raise InstructionRejected('Instruction evidence is not a verbatim part of the user instruction.')
        values = absolute_angles(stage['evidence'])
        if values and stage['kind'] != 'set_absolute':
            raise InstructionRejected('An explicit absolute angle must be kept as set_absolute, not converted to a free offset.')
        if stage['kind'] == 'set_absolute':
            targets = stage.get('absolute_targets', {})
            if set(targets) != set(stage['joints']):
                raise InstructionRejected('Every absolute-angle joint needs an explicit absolute target.')
            if len(set(values)) == 1 and any(value != values[0] for value in targets.values()):
                raise InstructionRejected('Absolute targets disagree with the angle quoted from the instruction.')
        elif stage.get('absolute_targets'):
            raise InstructionRejected('Absolute targets require kind set_absolute.')
        if stage['kind'] in ('return_start', 'restore_original') and not re.search(r'\b(return|restore|reset)\b|戻|復帰', stage['evidence'].lower()):
            raise InstructionRejected('A return stage needs explicit return wording in the instruction evidence.')
        if HAND in stage['joints'] and len(stage['joints']) > 1:
            if stage['kind'] in ('move', 'set_absolute'):
                raise InstructionRejected('Simultaneous arm and gripper motion is not supported.')
            if stage['duration_seconds'] is not None:
                raise InstructionRejected('A timed whole-posture restoration needs explicit separate arm and gripper times.')
        grounded = mentioned_joints(stage['evidence'])
        if stage['kind'] == 'restore_original' and re.search(r'\bposture\b|姿勢', stage['evidence'].lower()):
            grounded.update(JOINTS)
        if stage['kind'] == 'return_start' and not grounded:
            grounded.update(earlier_joints)
        if set(stage['joints']) - grounded:
            raise InstructionRejected('A stage names joints not supported by its instruction evidence. '
                                      'Specify the joints or provide the exact supporting clause.')
        earlier_joints.update(stage['joints'])
    if len(planning_stages(contract)) > 8:
        raise InstructionRejected('Controller expansion would exceed the maximum of 8 steps.')


def planning_stages(contract):
    """Split explicitly requested untimed restorations; never invent a reset."""
    stages = []
    for stage in contract['stages']:
        joints = stage['joints']
        if stage['kind'] in ('return_start', 'restore_original') and HAND in joints and len(joints) > 1:
            stages.extend([{**stage, 'joints': [j for j in ARM if j in joints]}, {**stage, 'joints': [HAND]}])
        else:
            stages.append(copy.deepcopy(stage))
    return stages


def constrained_schema(contract, joint_schema, repeat_schema, state=None):
    """All planner output is bound to the accepted contract, across all joints."""
    if contract['operation'] == 'repeat_previous':
        schema = copy.deepcopy(repeat_schema)
        properties = schema['properties']['repeat_previous']['properties']
        for key, value in contract['repeat'].items():
            properties[key] = {'enum': [value]}
        return schema
    variants = []
    for index, stage in enumerate(planning_stages(contract)):
        offsets = {}
        for joint in stage['joints']:
            rule = {'type': 'number'}
            if stage['kind'] == 'set_absolute':
                rule = {'enum': [stage['absolute_targets'][joint]]}
            elif stage['kind'] != 'move':
                rule = {'enum': [0]}
            elif state is not None:
                current = state['positions'][joint]
                bound = state.get('demo_limits', DEMO_LIMITS)[joint]
                rule.update(minimum=max(-bound, state['limits'][joint]['lower']) - current,
                            maximum=min(bound, state['limits'][joint]['upper']) - current)
            offsets[joint] = rule
        variants.append({'type': 'object', 'properties': {
            'stage': {'enum': [index + 1]},
            'reference': {'enum': ['original' if stage['kind'] == 'restore_original' else 'current']},
            'offsets': {'type': 'object', 'properties': offsets,
                        'required': stage['joints'], 'additionalProperties': False},
            'duration_seconds': ({'enum': [stage['duration_seconds']]} if stage['duration_seconds'] is not None
                                 else {'type': 'number', 'minimum': 1, 'maximum': 15})},
            'required': ['stage', 'reference', 'offsets', 'duration_seconds'], 'additionalProperties': False})
        if stage['kind'] == 'set_absolute':
            properties = variants[-1]['properties']
            properties.pop('offsets')
            properties['reference'] = {'enum': ['absolute']}
            properties['targets'] = {'type': 'object',
                'properties': {j: {'enum': [v]} for j, v in stage['absolute_targets'].items()},
                'required': stage['joints'], 'additionalProperties': False}
            variants[-1]['required'] = ['stage', 'reference', 'targets', 'duration_seconds']
    schema = copy.deepcopy(joint_schema)
    schema['properties']['steps'].update(minItems=len(variants), maxItems=len(variants), items={'oneOf': variants})
    return schema


def validate_plan_contract(relative, contract, joint_schema, repeat_schema, state=None):
    """Called even if the model client or its schema enforcement is bypassed."""
    validate(relative, constrained_schema(contract, joint_schema, repeat_schema, state))
    if contract['operation'] == 'new_action':
        if [step['stage'] for step in relative['steps']] != list(range(1, len(planning_stages(contract)) + 1)):
            raise ValueError('Plan stages must match the instruction exactly once, in the requested order.')


def validate_resolved_contract(plan, contract, state):
    """A listed joint with an unchanged target does not fulfill a requested move."""
    if contract['operation'] != 'new_action':
        return
    pose = dict(state['positions'])
    for index, (step, stage) in enumerate(zip(plan['steps'], planning_stages(contract)), 1):
        if stage['kind'] == 'move':
            for joint in stage['joints']:
                if abs(step['targets'][joint] - pose[joint]) < 0.005:
                    raise ValueError(f'Stage {index}: {joint} was requested to move but the target barely changes '
                                     '(<0.005 rad). Choose an actual motion within the SAME instruction contract.')
        elif stage['kind'] == 'set_absolute':
            for joint, target in stage['absolute_targets'].items():
                if abs(step['targets'][joint] - target) > 1e-9:
                    raise ValueError(f'Stage {index}: {joint} must reach the requested absolute angle {target:g}.')
        pose.update(step['targets'])


def contract_summary(contract):
    contract = {**contract, 'stages': planning_stages(contract)}
    contract = readable_contract(contract)
    if contract['operation'] == 'repeat_previous':
        return {'operation': 'repeat_previous', **contract['repeat']}
    moving = {j for stage in contract['stages'] for j in stage['joints']}
    return {'operation': contract['operation'], 'stages': contract['stages'],
            'keep_still': [name for name in JOINT_NAMES if name not in moving]}
