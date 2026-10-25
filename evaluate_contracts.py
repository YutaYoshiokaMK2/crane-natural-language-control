"""Real local-model permission evaluation. Never connects to MCP or moves a robot."""
import argparse
import json
from jsonschema import ValidationError
from pathlib import Path
from local_client import (classify_instruction, audit_instruction, plan_request, SYSTEM,
                          absolute_plan, validate_plan, JOINT_SCHEMA, REPEAT_SCHEMA)
from instruction_contract import validate_contract, validate_plan_contract, validate_resolved_contract, InstructionRejected
from motion_policy import ARM, HAND, DEMO_LIMITS, MAX_STEP

CASES = [
    ('elbow', 'From your current posture, bend only the elbow. Keep all other joints still.',
     [('move', [ARM[2]], None)]),
    ('wrist', 'Move only the wrist from its current position over 3 seconds. Keep the base and shoulder still.',
     [('move', [ARM[3]], 3)]),
    ('together', 'Move the shoulder and elbow together over 4 seconds. Keep the base, wrist and gripper still.',
     [('move', ARM[1:3], 4)]),
    ('sequence', 'First rotate the base over 5 seconds, then close the gripper over 2 seconds.',
     [('move', [ARM[0]], 5), ('move', [HAND], 2)]),
    ('repeat', 'Perform the same action in half the time. Do not return to the original posture.',
     {'return_to_original': False, 'time_scale': 0.5, 'amplitude_scale': 1}),
    ('restore', 'Return to your original posture, then bend only the elbow.',
     [('restore_original', ARM + [HAND], None), ('move', [ARM[2]], None)]),
    ('ambiguous', 'Do something impressive.', 'clarify'),
    ('unsupported', 'Move the base and close the gripper simultaneously.', 'unsupported'),
]


def run(args):
    args.log.parent.mkdir(parents=True, exist_ok=True)
    state = {'positions': dict(zip(ARM + [HAND], [-0.15, -0.15, 0.30, 0.15, 0.10])),
             'limits': {j: {'lower': -1, 'upper': 1} for j in ARM + [HAND]},
             'demo_limits': DEMO_LIMITS, 'max_step_radian': MAX_STEP}
    failed = 0
    for name, instruction, expected in CASES:
        if args.case and name not in args.case:
            continue
        record = {'case': name, 'instruction': instruction, 'model': args.model, 'executed': False}
        try:
            contract = classify_instruction(args.url, args.model, instruction)
            record['contract'] = contract
            if isinstance(expected, str):
                assert contract['operation'] == expected, contract
                try:
                    validate_contract(contract, instruction)
                except InstructionRejected as exc:
                    record.update(blocked=True, stop_reason=str(exc))
                else:
                    raise AssertionError('Ambiguous/unsupported instruction was not blocked')
            else:
                validate_contract(contract, instruction)
                if isinstance(expected, dict):
                    assert contract['operation'] == 'repeat_previous' and contract['repeat'] == expected, contract
                else:
                    assert contract['operation'] == 'new_action', contract
                    actual = [(s['kind'], sorted(s['joints']), s['duration_seconds']) for s in contract['stages']]
                    assert actual == [(kind, sorted(joints), duration) for kind, joints, duration in expected], actual
            if not isinstance(expected, str):
                audit = audit_instruction(args.url, args.model, instruction, contract)
                record['audit'] = audit
                assert audit['accepted'], audit
            if args.plans and isinstance(expected, list):
                messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content':
                    json.dumps({'instruction': instruction, 'state': state})}]
                for attempt in range(3):
                    relative = None
                    try:
                        relative, _ = plan_request(args.url, args.model, messages,
                            state=state, contract=contract)
                        validate_plan_contract(relative, contract, JOINT_SCHEMA, REPEAT_SCHEMA, state)
                        plan = absolute_plan(relative, state, {j: 0 for j in ARM + [HAND]})
                        validate_resolved_contract(plan, contract, state)
                        validate_plan(plan, state)
                        record.update(plan=plan, relative_plan=relative, attempt=attempt + 1)
                        break
                    except (ValueError, ValidationError) as exc:
                        if attempt == 2:
                            raise
                        if relative is not None:
                            messages.append({'role': 'assistant', 'content': json.dumps(relative)})
                        messages.append({'role': 'user', 'content': 'Correct the numeric plan within the SAME contract: ' + str(exc)[:1000]})
            record['passed'] = True
        except Exception as exc:
            failed += 1
            record.update(passed=False, error=str(exc))
        with args.log.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        print(json.dumps(record, ensure_ascii=False), flush=True)
    return int(bool(failed))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen3:4b')
    parser.add_argument('--case', action='append')
    parser.add_argument('--plans', action='store_true')
    parser.add_argument('--log', type=Path, default=Path('artifacts/contract-evaluation.jsonl'))
    raise SystemExit(run(parser.parse_args()))
