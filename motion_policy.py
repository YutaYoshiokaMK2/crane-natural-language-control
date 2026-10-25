"""Conservative demonstration limits, separate from the robot's URDF limits."""
ARM = [f'crane_plus_joint{i}' for i in range(1, 5)]
HAND = 'crane_plus_joint_hand'
DEMO_LIMITS = {**{joint: 0.4 for joint in ARM}, HAND: 0.15}
MAX_STEP = 0.4
