"""Dedicated stdio MCP server; stdout is reserved for MCP messages."""
import threading

import rclpy
from mcp.server.fastmcp import FastMCP

from crane_control import CraneControl, HAND

mcp = FastMCP('crane-gazebo')
lock = threading.Lock()
node = None


@mcp.tool()
def get_joint_state() -> dict:
    """Read fresh joint positions and URDF limits in radians from Gazebo."""
    with lock:
        return node.get_joint_state()


@mcp.tool()
def move_joints(targets: dict[str, float], duration: float) -> dict:
    """Move named arm joints to absolute radians in duration seconds, then verify arrival.

    joint1: base about +Z; joint2: shoulder about +Y;
    joint3: elbow about -Y; joint4: wrist about -Y.
    Arm demo envelope +/-0.4 rad, step <=0.4 rad, duration 1..15 s.
    Omitted joints hold current position. No collision avoidance or IK.
    Stop the sequence if success is false. Use set_gripper for the hand.
    """
    if HAND in targets:
        raise ValueError('Use set_gripper for the hand')
    with lock:
        return node.move_joints(targets, duration)


@mcp.tool()
def set_gripper(position: float, duration: float) -> dict:
    """Move the gripper joint to absolute radians and verify arrival; demo +/-0.15 rad.

    Opening direction has not yet been visually calibrated. No object grasping.
    """
    with lock:
        return node.move_joints({HAND: position}, duration)


if __name__ == '__main__':
    rclpy.init()
    node = CraneControl()
    try:
        mcp.run(transport='stdio')
    finally:
        node.destroy_node()
        rclpy.shutdown()
