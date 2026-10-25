"""Exercise the real stdio MCP connection without an LLM."""
import asyncio
import argparse
import json
import math

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp_connection import server_parameters


def unpack(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    return json.loads(next(c.text for c in result.content if c.type == 'text'))


async def main(use_docker=False, state_only=False, hold_seconds=0):
    params = server_parameters(use_docker)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print('tools:', [t.name for t in (await session.list_tools()).tools], flush=True)
            state = unpack(await session.call_tool('get_joint_state', {}))
            print(json.dumps(state), flush=True)
            if state_only:
                return
            for bad in ({'unknown': 0.0}, {'crane_plus_joint1': 9.0}):
                result = await session.call_tool('move_joints', {'targets': bad, 'duration': 2})
                assert result.isError, 'Invalid command was not rejected'
            print('invalid commands rejected', flush=True)
            for joint in [f'crane_plus_joint{i}' for i in range(1, 5)] + ['crane_plus_joint_hand']:
                original = state['positions'][joint]
                for target in [original + 0.05, original]:
                    print(f'moving {joint} -> {target:.5f} rad', flush=True)
                    name = 'set_gripper' if joint.endswith('hand') else 'move_joints'
                    args = {'position': target, 'duration': 2} if name == 'set_gripper' else {'targets': {joint: target}, 'duration': 2}
                    result = unpack(await session.call_tool(name, args))
                    print(json.dumps(result), flush=True)
                    if not result['success']:
                        raise RuntimeError('Arrival check failed; stopping')
                    await asyncio.sleep(hold_seconds)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker', action='store_true')
    parser.add_argument('--state-only', action='store_true')
    parser.add_argument('--hold', type=float, default=0, help='Hold each reached pose for 0..10 seconds to make motion easier to see')
    args = parser.parse_args()
    if not math.isfinite(args.hold) or not 0 <= args.hold <= 10:
        parser.error('--hold must be a finite number between 0 and 10')
    asyncio.run(main(args.docker, args.state_only, args.hold))
