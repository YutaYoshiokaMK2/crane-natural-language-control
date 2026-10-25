"""LLM-free visibility check through MCP; this is not an LLM gesture tool."""
import asyncio
import json

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp_connection import server_parameters
from mcp_smoke import unpack


async def main():
    async with stdio_client(server_parameters(use_docker=True)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            state = unpack(await session.call_tool('get_joint_state', {}))
            if any(abs(v) > 0.08 for v in state['positions'].values()):
                raise RuntimeError('This check requires a near-upright starting pose (all angles within 0.08 rad).')
            joint = 'crane_plus_joint2'
            original = state['positions'][joint]
            for target in [0.35, original, -0.35, original]:
                print(f'Shoulder -> {target:.3f} rad; 3-second move, then 3-second hold', flush=True)
                result = unpack(await session.call_tool('move_joints', {'targets': {joint: target}, 'duration': 3}))
                print(json.dumps(result), flush=True)
                if not result['success']:
                    raise RuntimeError('Arrival failed; remaining moves skipped')
                await asyncio.sleep(3)


if __name__ == '__main__':
    asyncio.run(main())
