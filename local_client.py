"""Local Ollama planner -> stdio MCP -> ROS 2. No cloud API or API key."""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from jsonschema.exceptions import ValidationError
from mcp_smoke import unpack
from mcp_connection import server_parameters
from motion_policy import ARM, HAND, DEMO_LIMITS, MAX_STEP
from instruction_contract import (CONTRACT_SCHEMA, INTERPRETER_SCHEMA, JOINT_NAMES, readable_contract,
    INTERPRETER_SYSTEM, AUDITOR_SYSTEM, AUDIT_SCHEMA, AUDIT_CHECKS,
    InstructionRejected, validate_contract, constrained_schema, validate_plan_contract, validate_resolved_contract,
    planning_stages, source_candidates, evidence_candidates, contract_summary)

SYSTEM = '''Plan small CRANE+ robot motions. Choose joint target angles, durations and step order.
For "repeat" or "same action", DO NOT rewrite the old steps. Return ONLY this JSON instead:
{"explanation":"one sentence","repeat_previous":{"return_to_original":true,"time_scale":0.5,"amplitude_scale":1,"restore_duration_seconds":3}}.
Choose these values from the user's request. Half the time = time_scale 0.5, half amplitude = amplitude_scale 0.5.
This reuses the previous LLM-generated action, not a preset gesture. Include all its arm AND gripper steps.
For a NEW action use the joint-step JSON below.
Reply ONLY {"explanation":"one sentence","steps":[{"stage":1,"reference":"current","offsets":{"joint_name":0.05},"duration_seconds":2}]}.
Joint names: crane_plus_joint1 (base about +Z: viewed from above positive=counterclockwise, negative=clockwise), crane_plus_joint2 (shoulder),
crane_plus_joint3 (elbow), crane_plus_joint4 (wrist), crane_plus_joint_hand (gripper).
Choose a reference for EVERY step. Offsets are radians added to that reference pose.
"current" means this request's measured starting pose. "original" means the saved original posture.
"previous_step_1", "previous_step_2", etc. mean the poses reached by the previous action's steps.
For restoring or replaying a reference pose, use offset 0 for the joints you want to restore or replay.
To move a joint a little, choose an offset such as +0.05 or -0.05 rad.
With reference "current", offset 0 holds this request's starting angle, not the saved original posture.
ONLY if explicitly asked to return to the original posture, restore using "original".
Restoring the whole posture requires an arm step and a separate gripper step.
For a new action from the current posture, use "current" and move ONLY the requested joints.
Follow the accepted instruction contract exactly. Each stage produces exactly one step with its stage number.
Do not add, remove, reorder or duplicate stages. Do not move joints absent from a stage's joint list.
For set_absolute use reference "absolute" and targets containing the specified absolute angles.
Do not use offsets or calculate a signed change for set_absolute.
Half the time means divide REPLAY durations by 2; half_duration_seconds is provided for each previous step.
Offsets are NOT increments from the previous step. Do not copy absolute angles into offsets.
For a clearly visible large arm motion, an offset of about 0.30..0.35 rad is suitable near the upright pose.
Final absolute angles and step changes must obey the limits supplied with the current state.
Omitted joints stay still. Separate arm and gripper steps.
An arm step MUST NOT include crane_plus_joint_hand, even with offset zero.
A gripper step MUST contain ONLY crane_plus_joint_hand. Never add arm joints with zero offsets.
For wide moves prefer 0.30..0.35 rad and leave a margin inside the allowed target range.
duration_seconds is the TIME taken, 1..15 seconds. At most 8 steps.
Slower means MORE seconds, never fewer. Example: making a 2-second step slower could use 4 seconds.
For follow-ups, use the previous plan: half amplitude means half the change FROM its initial angle.
The plan must include an excursion of at least 0.02 rad; never replace a rejected plan with tiny fake motion.
No IK, collision avoidance, vision or grasping. Unsupported request: empty steps and explanation.
'''

MIN_EXCURSION = 0.02


class InfeasibleTimeScale(ValueError):
    """A requested replay speed cannot satisfy the motion duration limits."""

JOINT_SCHEMA = {'type': 'object', 'properties': {
    'explanation': {'type': 'string'},
    'steps': {'type': 'array', 'maxItems': 8, 'items': {'type': 'object', 'properties': {
        'offsets': {'oneOf': [
            {'type': 'object', 'properties': {j: {'type': 'number'} for j in ARM},
             'minProperties': 1, 'additionalProperties': False},
            {'type': 'object', 'properties': {HAND: {'type': 'number'}},
             'required': [HAND], 'additionalProperties': False}]},
        'reference': {'type': 'string', 'enum': ['current', 'original'] + [f'previous_step_{i}' for i in range(1, 9)]},
        'duration_seconds': {'type': 'number', 'minimum': 1, 'maximum': 15}},
        'required': ['reference', 'offsets', 'duration_seconds'], 'additionalProperties': False}}},
    'required': ['explanation', 'steps'], 'additionalProperties': False}

REPEAT_SCHEMA = {'type': 'object', 'properties': {
    'explanation': {'type': 'string'}, 'repeat_previous': {'type': 'object', 'properties': {
        'return_to_original': {'type': 'boolean'},
        'time_scale': {'type': 'number', 'minimum': 0.1, 'maximum': 4},
        'amplitude_scale': {'type': 'number', 'minimum': 0.1, 'maximum': 2},
        'restore_duration_seconds': {'type': 'number', 'minimum': 1, 'maximum': 15}},
        'required': ['return_to_original', 'time_scale', 'amplitude_scale', 'restore_duration_seconds'],
        'additionalProperties': False}},
    'required': ['explanation', 'repeat_previous'], 'additionalProperties': False}
SCHEMA = {'oneOf': [JOINT_SCHEMA, REPEAT_SCHEMA]}


def route_instruction(url, model, instruction):
    _, operations = source_candidates(instruction)
    schema = {'type': 'object', 'properties': {
        'operation': {'type': 'string', 'enum': operations},
        'reason': {'type': 'string'}}, 'required': ['operation', 'reason'], 'additionalProperties': False}
    payload = {'model': model, 'stream': False, 'messages': [
        {'role': 'system', 'content': 'Classify ONLY the current robot instruction. '
            'repeat_previous: asks to repeat, reuse or modify the same/previous action (speed/amplitude changes included). '
            'new_action: describes a new joint motion or posture restoration. '
            'clarify: no identifiable joints or actions, such as "do something impressive". '
            'unsupported: vision, grasping, IK, obstacle avoidance, or simultaneous arm and gripper. '
            'Return operation and one short reason. Do not generate steps.'},
        {'role': 'user', 'content': instruction}], 'format': schema,
        'options': {'temperature': 0, 'num_predict': 160, 'num_ctx': 2048}}
    if model.startswith('qwen3'):
        payload['think'] = False
    req = urllib.request.Request(url + '/api/chat', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        result = json.load(response)
    if not result.get('done') or result.get('done_reason') == 'length':
        raise InstructionRejected('Instruction routing incomplete; no motion is permitted.')
    route = json.loads(result['message']['content'])
    from jsonschema import validate
    validate(route, schema)
    return route


def classify_instruction(url, model, instruction):
    route = route_instruction(url, model, instruction)
    if route['operation'] in ('clarify', 'unsupported'):
        return {**route, 'stages': [], 'repeat': None}
    schema = copy.deepcopy(INTERPRETER_SCHEMA)
    # The router already provides a reason. Generate permissions here, not another narrative.
    schema['properties'].pop('reason')
    schema['required'].remove('reason')
    schema['properties']['stages']['items']['properties']['evidence'] = {
        'type': 'string', 'enum': evidence_candidates(instruction)}
    candidates, _ = source_candidates(instruction)
    names = [name for name, joint in JOINT_NAMES.items() if joint in candidates]
    if names:
        schema['properties']['stages']['items']['properties']['joints']['items'] = {'enum': names}
    schema['properties']['operation'] = {'enum': [route['operation']]}
    if route['operation'] == 'repeat_previous':
        schema['properties']['stages'].update(maxItems=0)
        schema['properties']['repeat']['type'] = 'object'
    else:
        schema['properties']['stages']['minItems'] = 1
        schema['properties']['repeat'] = {'enum': [None]}
    messages = [{'role': 'system', 'content': INTERPRETER_SYSTEM},
                {'role': 'user', 'content': 'Operation: ' + route['operation'] + '\nInstruction: ' + instruction}]
    payload = {'model': model, 'stream': False, 'messages': messages,
        'format': schema,
        'options': {'temperature': 0, 'num_predict': 1000, 'num_ctx': 4096}}
    if model.startswith('qwen3'):
        payload['think'] = False
    req = urllib.request.Request(url + '/api/chat', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        result = json.load(response)
    if not result.get('done') or result.get('done_reason') == 'length':
        raise InstructionRejected('Instruction interpretation incomplete; no motion is permitted.')
    intent = json.loads(result['message']['content'])
    from jsonschema import validate
    validate(intent, payload['format'])
    intent['reason'] = route['reason']
    for stage in intent['stages']:
        stage['joints'] = [JOINT_NAMES[name] for name in stage['joints']]
        if 'absolute_targets' in stage:
            stage['absolute_targets'] = {JOINT_NAMES[name]: value for name, value in stage['absolute_targets'].items()}
    return intent


def audit_instruction(url, model, instruction, contract):
    readable = readable_contract(contract)
    readable.pop('reason', None)
    if contract['operation'] == 'new_action':
        # Compare the instruction to execution semantics, without the first model's narrative or quotes.
        for stage in readable['stages']:
            moving = stage.pop('joints')
            stage.pop('evidence', None)
            stage['moving_joints'] = moving
            stage['held_joints'] = [name for name in JOINT_NAMES if name not in moving]
    payload = {'model': model, 'stream': False, 'messages': [
        {'role': 'system', 'content': AUDITOR_SYSTEM},
        {'role': 'user', 'content': json.dumps({'instruction': instruction, 'contract': readable}, ensure_ascii=False)}],
        'format': AUDIT_SCHEMA, 'options': {'temperature': 0, 'num_predict': 256, 'num_ctx': 4096}}
    if model.startswith('qwen3'):
        payload['think'] = False
    req = urllib.request.Request(url + '/api/chat', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        result = json.load(response)
    if not result.get('done') or result.get('done_reason') == 'length':
        raise InstructionRejected('Instruction audit did not complete; no motion is permitted.')
    audit = json.loads(result['message']['content'])
    from jsonschema import validate
    validate(audit, AUDIT_SCHEMA)
    failures = [key for key in AUDIT_CHECKS if not audit[key]]
    audit['accepted'] = not failures
    audit['reason'] = ('Instruction audit accepted the extracted permissions.' if audit['accepted']
                       else 'Instruction audit found a mismatch or ambiguity: ' + ', '.join(failures))
    return audit


def output_schema(contract, state=None):
    return constrained_schema(contract, JOINT_SCHEMA, REPEAT_SCHEMA, state)


def plan_request(url, model, messages, think=False, *, state, contract):
    schema = output_schema(contract, state)
    planner_contract = {**contract, 'stages': planning_stages(contract)}
    messages = messages + [{'role': 'user', 'content':
            'Accepted instruction contract, expanded for controllers (do not change these permissions):\n' + json.dumps(planner_contract, ensure_ascii=False) +
            '\nRequired output schema including permitted offsets:\n' + json.dumps(schema) +
            '\nChoose angles and unspecified durations within this contract. For new actions, output one step per stage '
            'in order, with stage numbers 1,2,... . No extra setup, return, or neighbouring joint motion.'}]
    payload = {'model': model, 'messages': messages, 'stream': False,
               'format': schema,
               'options': {'temperature': 0.6 if think else 0.7,
                                            'top_p': 0.95 if think else 0.8, 'top_k': 20,
                                            'min_p': 0, 'num_ctx': 4096, 'num_predict': 2000 if think else 1200}}
    if think or model.startswith('qwen3'):
        payload['think'] = think
    req = urllib.request.Request(url + '/api/chat', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=300) as response:
        result = json.load(response)
    if not result.get('done') or result.get('done_reason') == 'length':
        raise RuntimeError('LLM output incomplete')
    plan = json.loads(result['message']['content'])
    from jsonschema import validate
    validate(plan, payload['format'])
    stats = {k: result.get(k) for k in ['model', 'total_duration', 'eval_count', 'eval_duration']}
    stats.update({'think': think, 'options': payload['options'], 'operation': contract['operation']})
    return plan, stats


def validate_plan(plan, state, allow_noop=False):
    """Validate the entire sequence before issuing its first command."""
    import math
    positions = dict(state['positions'])
    has_motion = False
    for index, step in enumerate(plan['steps'], 1):
        if not math.isfinite(step['duration']) or not 1 <= step['duration'] <= 15:
            raise ValueError('Every resolved duration must be between 1 and 15 seconds; change time_scale if needed.')
        targets = step['targets']
        if not targets or set(targets) - set(positions):
            raise ValueError('Unknown or empty targets')
        if 'crane_plus_joint_hand' in targets and len(targets) > 1:
            raise ValueError('Separate arm and gripper steps required')
        for joint, value in targets.items():
            limits = state['limits'][joint]
            demo_limit = state.get('demo_limits', DEMO_LIMITS)[joint]
            if not math.isfinite(value) or not limits['lower'] <= value <= limits['upper'] or abs(value) > demo_limit:
                raise ValueError(f'{joint}: target {value:.5f} outside allowed range '
                    f'[{max(-demo_limit, limits["lower"]):.5f}, {min(demo_limit, limits["upper"]):.5f}]. '
                    'Reduce the offset; absolute target = initial angle + offset.')
            if abs(value - positions[joint]) > state.get('max_step_radian', MAX_STEP):
                maximum = state.get('max_step_radian', MAX_STEP)
                lower = max(-demo_limit, limits['lower'], positions[joint] - maximum)
                upper = min(demo_limit, limits['upper'], positions[joint] + maximum)
                raise ValueError(f'Step {index}, {joint}: target {value:.5f} from preceding angle '
                    f'{positions[joint]:.5f} changes {abs(value - positions[joint]):.5f} rad, '
                    f'exceeding maximum {maximum:g} rad. Choose an absolute target in '
                    f'[{lower:.5f}, {upper:.5f}]. For a current-posture request use reference current; '
                    'move only the requested joints.')
            has_motion = has_motion or abs(value - state['positions'][joint]) >= MIN_EXCURSION
        positions.update(targets)
    if plan['steps'] and not has_motion and not allow_noop:
        raise ValueError('Plan has no meaningful motion: include an excursion of at least 0.02 rad from the current pose. '
                         'For original posture use reference original with zero offsets; do not invent tiny offsets to pass validation.')


def action_reference(record):
    # A replay's restoration is setup, not part of the action to repeat next.
    # Derive the prefix for older logs as well, so --resume needs no migration.
    steps = record['plan']['steps']
    start = record['state']['positions']
    repeat = (record.get('relative_plan') or {}).get('repeat_previous')
    if repeat and repeat['return_to_original']:
        original = record['original_pose']
        prefix = sum(any(abs(start[j] - original[j]) > 0.005 for j in group)
                     for group in [ARM, [HAND]])
        steps = steps[prefix:]
        start = original
    return {'instruction': record['instruction'], 'executed': record['executed'],
            'contract_verified': record.get('contract_version') == 2 and (record.get('audit') or {}).get('accepted') is True,
            'start_positions': dict(start),
            'plan': {'explanation': record['plan'].get('explanation', ''), 'steps': copy.deepcopy(steps)}}


def motion_preview(plan, state):
    names = {joint: name for name, joint in JOINT_NAMES.items()}
    pose = dict(state['positions'])
    lines = []
    moving = set()
    for index, step in enumerate(plan['steps'], 1):
        changes = ', '.join(f'{names[j]} {pose[j]:.3f} -> {value:.3f} rad '
                            f'({value - pose[j]:+.3f})' for j, value in step['targets'].items())
        lines.append(f'  {index}. {changes}; {step["duration"]:g} s')
        moving.update(step['targets'])
        pose.update(step['targets'])
    lines.append('  Keep still: ' + (', '.join(names[j] for j in ARM + [HAND] if j not in moving) or '(none)'))
    return '\n'.join(lines)


def resume_reference(path):
    """Recover the latest reached, nontrivial action; ignore previews and rejected/no-op attempts."""
    selected = None
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                record = json.loads(line)
                if not record.get('executed') or record.get('error') or not record.get('results'):
                    continue
                if not all(result.get('success') for result in record['results']):
                    continue
                if len(record['results']) != len(record['plan']['steps']):
                    continue
                excursion = max((abs(value - record['state']['positions'][joint])
                    for step in record['plan']['steps'] for joint, value in step['targets'].items()), default=0)
                if excursion >= MIN_EXCURSION:
                    selected = record
            except (ValueError, KeyError, TypeError):
                continue
    if selected is None:
        raise ValueError('No reached action with meaningful motion was found in the log to resume.')
    return selected.get('original_pose', selected['state']['positions']), action_reference(selected)


def reference_context(state, original, previous):
    context = {'available_references': ['current', 'original'],
               'original': 'Saved original posture; restore selected joints with zero offsets.'}
    if previous is not None:
        context['available_references'].extend(f'previous_step_{i}' for i in range(1, len(previous['plan']['steps']) + 1))
        context['previous_action'] = {
            'instruction': previous['instruction'], 'executed': previous['executed'],
            'steps': [
                {'reference': f'previous_step_{i}', 'joints': list(step['targets']),
                 'duration_seconds': step['duration'], 'half_duration_seconds': step['duration'] / 2}
                for i, step in enumerate(previous['plan']['steps'], 1)]}
    def compact(value):
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return round(value, 6) if isinstance(value, float) else value
    return compact(context)


def absolute_plan(relative_plan, state, original_pose=None, previous_action=None):
    if 'repeat_previous' in relative_plan:
        if previous_action is None:
            raise ValueError('No previous action is available to repeat. Describe a new action first.')
        repeat = relative_plan['repeat_previous']
        for i, step in enumerate(previous_action['plan']['steps'], 1):
            duration = step['duration'] * repeat['time_scale']
            if not 1 <= duration <= 15:
                raise InfeasibleTimeScale(
                    f'Replay step {i}: {step["duration"]:g} s x {repeat["time_scale"]:g} = '
                    f'{duration:g} s; allowed duration is 1..15 s. '
                    'The requested speed cannot be executed. Use the same speed (time_scale 1) '
                    'or describe a new action with explicit durations. Timing was not changed automatically.')
        steps = []
        if repeat['return_to_original']:
            if original_pose is None:
                raise ValueError('No saved original posture is available.')
            for group in [ARM, [HAND]]:
                if any(abs(state['positions'][j] - original_pose[j]) > 0.005 for j in group):
                    steps.append({'targets': {j: original_pose[j] for j in group},
                                  'duration': repeat['restore_duration_seconds']})
        for step in previous_action['plan']['steps']:
            steps.append({'targets': {j: previous_action['start_positions'][j] +
                (value - previous_action['start_positions'][j]) * repeat['amplitude_scale']
                for j, value in step['targets'].items()},
                'duration': step['duration'] * repeat['time_scale']})
        if len(steps) > 8:
            raise ValueError('Restoration and replay exceed the maximum of 8 steps.')
        return {'explanation': relative_plan['explanation'], 'steps': steps}
    references = {'current': state['positions']}
    if original_pose is not None:
        references['original'] = original_pose
    if previous_action is not None:
        pose = dict(previous_action['start_positions'])
        for i, step in enumerate(previous_action['plan']['steps'], 1):
            pose.update(step['targets'])
            references[f'previous_step_{i}'] = dict(pose)
    steps = []
    for step in relative_plan['steps']:
        if step.get('reference') == 'absolute':
            if set(step['targets']) - set(state['positions']):
                raise ValueError('Unknown joint in absolute targets')
            steps.append({'targets': dict(step['targets']), 'duration': step['duration_seconds']})
            continue
        if set(step['offsets']) - set(state['positions']):
            raise ValueError('Unknown joint in offsets')
        reference = step.get('reference', 'current')
        if reference not in references:
            raise ValueError(f'Reference {reference} is unavailable; available references: {list(references)}')
        steps.append({'targets': {j: references[reference][j] + value for j, value in step['offsets'].items()},
                      'duration': step['duration_seconds']})
    return {'explanation': relative_plan['explanation'], 'steps': steps}


async def main(args):
    host = urlparse(args.url).hostname
    if host not in ('localhost', '127.0.0.1', 'host.docker.internal') or urlparse(args.url).scheme != 'http':
        raise ValueError('Only local Ollama HTTP endpoints are permitted')
    if args.model.endswith('-cloud') or '/' in args.model:
        raise ValueError('Use a downloaded local model name')
    params = server_parameters(args.docker)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    messages = [{'role': 'system', 'content': SYSTEM}]
    original_pose, previous_action = None, None
    if getattr(args, 'resume', False):
        original_pose, previous_action = resume_reference(args.log)
        if previous_action['contract_verified']:
            print('Resumed reference action: ' + previous_action['instruction'], flush=True)
        else:
            print('Restored original-posture reference. Previous action predates contract verification; '
                  'it will not be replayed. Describe a new motion first.', flush=True)
            previous_action = None
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            prompt = args.prompt
            while True:
                if prompt is None:
                    try:
                        prompt = input('instruction (exit to quit)> ').strip()
                    except EOFError:
                        return
                    if prompt == 'exit':
                        return
                state = unpack(await session.call_tool('get_joint_state', {}))
                if original_pose is None:
                    original_pose = dict(state['positions'])
                context = reference_context(state, original_pose, previous_action)
                # Explicit references replace an ever-growing and potentially truncated history.
                messages = [messages[0]]
                model_state = '\n'.join(f'{j} = {v:.4f}' for j, v in state['positions'].items())
                allowed = '\n'.join(f'{j}: absolute target between {max(-bound, state["limits"][j]["lower"]):.3f} and '
                    f'{min(bound, state["limits"][j]["upper"]):.3f} rad' for j, bound in state.get('demo_limits', DEMO_LIMITS).items())
                history_start = len(messages)
                messages.append({'role': 'user', 'content': 'Current measured angles in radians:\n' +
                    model_state + '\nAllowed demo targets:\n' + allowed +
                    '\nReference context:\n' + json.dumps(context) +
                    f'\nMaximum change per step: {state.get("max_step_radian", MAX_STEP)} rad.\nUser instruction: ' + prompt +
                    '\nGenerate a new motion plan for this instruction. Output explanation and steps only.'})
                planning_failed = False
                operation = None
                intent = None
                audit = None
                for attempt in range(3):
                    plan = None
                    relative = None
                    try:
                        if operation is None:
                            try:
                                intent = await asyncio.to_thread(classify_instruction, args.url, args.model, prompt)
                                validate_contract(intent, prompt)
                            except (ValueError, RuntimeError, ValidationError) as exc:
                                raise InstructionRejected(str(exc)) from exc
                            if intent['operation'] == 'repeat_previous' and previous_action is None:
                                raise InstructionRejected('No verified previous action is available. Describe a new motion first, '
                                                          'or resume a verified run log.')
                            try:
                                audit = await asyncio.to_thread(audit_instruction, args.url, args.model, prompt, intent)
                            except (ValueError, RuntimeError, ValidationError) as exc:
                                raise InstructionRejected('Instruction audit failed: ' + str(exc)) from exc
                            if not audit['accepted']:
                                raise InstructionRejected('Instruction interpretation rejected: ' + audit['reason'])
                            operation = intent['operation']
                            print('Instruction contract: ' + json.dumps(contract_summary(intent), ensure_ascii=False), flush=True)
                            if operation == 'new_action':
                                # Previous half-speed/amplitude instructions must not leak into a new motion.
                                fresh_context = reference_context(state, original_pose, None)
                                messages[-1]['content'] = messages[-1]['content'].replace(
                                    '\nReference context:\n' + json.dumps(context),
                                    '\nReference context:\n' + json.dumps(fresh_context))
                        if operation == 'repeat_previous' and previous_action is None:
                            raise InstructionRejected('No previous action is available. Describe a new motion first, or restart with --resume.')
                        relative, stats = await asyncio.to_thread(plan_request, args.url, args.model, messages,
                            getattr(args, 'think', False), state=state, contract=intent)
                        validate_plan_contract(relative, intent, JOINT_SCHEMA, REPEAT_SCHEMA, state)
                        plan = absolute_plan(relative, state, original_pose, previous_action)
                        validate_resolved_contract(plan, intent, state)
                        validate_plan(plan, state, allow_noop=all(s['kind'] == 'set_absolute' for s in intent['stages'])
                                      and intent['operation'] == 'new_action')
                        break
                    except Exception as exc:
                        if isinstance(exc, (TimeoutError, urllib.error.URLError)):
                            exc = InstructionRejected('Local LLM request failed or timed out: ' + str(exc))
                        with args.log.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                                'instruction': prompt, 'intent': intent, 'audit': audit, 'state': state, 'original_pose': original_pose,
                                'plan': plan, 'relative_plan': relative, 'attempt': attempt + 1,
                                'executed': False, 'error': str(exc)}, ensure_ascii=False) + '\n')
                        if not isinstance(exc, (ValueError, RuntimeError, ValidationError)):
                            raise
                        if isinstance(exc, (InfeasibleTimeScale, InstructionRejected)):
                            print(f'Plan rejected: {exc}\nNo motion was executed.', flush=True)
                            planning_failed = True
                            del messages[history_start:]
                            break
                        if attempt == 2:
                            print(f'Plan rejected after 3 attempts: {str(exc)[:500]}\n'
                                  'No motion was executed. Please rephrase the instruction.', flush=True)
                            planning_failed = True
                            del messages[history_start:]
                            break
                        if relative is not None:
                            messages.append({'role': 'assistant', 'content': json.dumps(relative)})
                        messages.append({'role': 'user', 'content': 'The validator rejected that plan: ' + str(exc)[:1000] +
                            '\nNo motion was executed. Correct the plan to fulfill the original instruction: ' + prompt})
                if planning_failed:
                    if args.prompt is not None:
                        return 1
                    prompt = None
                    continue
                print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
                print('Motion preview (not executed yet):\n' + motion_preview(plan, state), flush=True)
                if args.execute and getattr(args, 'confirm_plan', False):
                    try:
                        approved = input('Execute exactly this plan? [y/N]> ').strip().lower() == 'y'
                    except EOFError:
                        approved = False
                    if not approved:
                        print('Plan cancelled. No motion was executed.', flush=True)
                        with args.log.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                                'instruction': prompt, 'intent': intent, 'audit': audit, 'state': state,
                                'relative_plan': relative, 'plan': plan, 'executed': False,
                                'error': 'Plan not confirmed by user'}, ensure_ascii=False) + '\n')
                        if args.prompt is not None:
                            return 1
                        prompt = None
                        continue
                if args.execute:
                    # Planning/review can take time. Never silently rebase a reviewed plan onto a different pose.
                    fresh_state = unpack(await session.call_tool('get_joint_state', {}))
                    if any(abs(fresh_state['positions'][j] - state['positions'][j]) > 0.01 for j in ARM + [HAND]):
                        print('Current posture changed during planning. No motion was executed; submit the instruction again.', flush=True)
                        with args.log.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                                'instruction': prompt, 'intent': intent, 'audit': audit, 'state': state,
                                'fresh_state': fresh_state, 'plan': plan, 'relative_plan': relative,
                                'executed': False, 'error': 'Posture changed during planning'}, ensure_ascii=False) + '\n')
                        if args.prompt is not None:
                            return 1
                        prompt = None
                        continue
                    try:
                        validate_plan_contract(relative, intent, JOINT_SCHEMA, REPEAT_SCHEMA, state)
                        validate_resolved_contract(plan, intent, fresh_state)
                        validate_plan(plan, fresh_state, allow_noop=all(s['kind'] == 'set_absolute' for s in intent['stages'])
                                      and intent['operation'] == 'new_action')
                    except (ValueError, ValidationError) as exc:
                        print('Final validation rejected the plan: ' + str(exc)[:500] + '\nNo motion was executed.', flush=True)
                        with args.log.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                                'instruction': prompt, 'intent': intent, 'audit': audit, 'state': state,
                                'fresh_state': fresh_state, 'plan': plan, 'relative_plan': relative,
                                'executed': False, 'error': str(exc)}, ensure_ascii=False) + '\n')
                        if args.prompt is not None:
                            return 1
                        prompt = None
                        continue
                record = {'time': datetime.now(timezone.utc).isoformat(), 'instruction': prompt,
                          'contract_version': 2,
                          'state': state, 'plan': plan, 'relative_plan': relative, 'llm': stats, 'attempt': attempt + 1,
                          'original_pose': original_pose, 'reference_context': context,
                          'intent': intent, 'audit': audit,
                          'executed': args.execute, 'results': []}
                try:
                    if args.execute:
                        for step in plan['steps']:
                            targets = step['targets']
                            if 'crane_plus_joint_hand' in targets:
                                tool, data = 'set_gripper', {'position': targets['crane_plus_joint_hand'], 'duration': step['duration']}
                            else:
                                tool, data = 'move_joints', step
                            result = unpack(await session.call_tool(tool, data))
                            record['results'].append(result)
                            print(json.dumps(result), flush=True)
                            if not result['success']:
                                raise RuntimeError('Arrival failed; sequence stopped')
                except Exception as exc:
                    record['error'] = str(exc)
                    raise
                finally:
                    with args.log.open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                messages.extend([{'role': 'assistant', 'content': json.dumps(relative, ensure_ascii=False)},
                                 {'role': 'user', 'content': json.dumps({'executed': args.execute,
                                     'success': [r['success'] for r in record['results']]})}])
                previous_action = action_reference(record)
                if args.prompt is not None:
                    return
                prompt = None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:11434')
    parser.add_argument('--docker', action='store_true', help='Run MCP inside the existing pai_ros2 container')
    parser.add_argument('--model', default='qwen3:4b')
    parser.add_argument('--think', action='store_true', help='Enable Qwen3 thinking (much slower on CPU)')
    parser.add_argument('--prompt', help='Omit for interactive conversation with follow-up instructions')
    parser.add_argument('--execute', action='store_true', help='Execute through MCP; otherwise display plan only')
    parser.add_argument('--confirm-plan', action='store_true', help='Require y after displaying the exact contract and plan before executing')
    parser.add_argument('--resume', action='store_true', help='Restore original posture and previous reached action references from --log')
    parser.add_argument('--log', type=Path, default=Path('artifacts/runs.jsonl'))
    raise SystemExit(asyncio.run(main(parser.parse_args())) or 0)
