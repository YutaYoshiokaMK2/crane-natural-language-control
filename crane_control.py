"""Gazebo-only joint control. Run with the lecture ROS 2 environment sourced."""
import argparse
import json
import math
import time
import xml.etree.ElementTree as ET

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from motion_policy import ARM, HAND, DEMO_LIMITS, MAX_STEP


class CraneControl(Node):
    def __init__(self):
        super().__init__('crane_natural_language_control')
        self.positions = {}
        self.received = 0.0
        self.limits = {}
        self.samples = 0
        self.create_subscription(JointState, '/joint_states', self.on_state, 10)
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(String, '/robot_description', self.on_description, qos)
        self.publishers_by_group = {
            'arm': self.create_publisher(JointTrajectory, '/crane_plus_arm_controller/joint_trajectory', 10),
            'hand': self.create_publisher(JointTrajectory, '/crane_plus_gripper_controller/joint_trajectory', 10),
        }

    def on_state(self, msg):
        values = dict(zip(msg.name, msg.position))
        if all(j in values and math.isfinite(values[j]) for j in ARM + [HAND]):
            self.positions = {j: values[j] for j in ARM + [HAND]}
            self.received = time.monotonic()
            self.samples += 1

    def on_description(self, msg):
        root = ET.fromstring(msg.data)
        if root.find('.//ros2_control/hardware/plugin') is None:
            return
        plugins = [p.text or '' for p in root.findall('.//ros2_control/hardware/plugin')]
        if not any('Gazebo' in p or 'Ignition' in p for p in plugins):
            return  # Do not enable this demonstration against physical hardware.
        self.limits = {j.attrib['name']: {k: float(v) for k, v in j.find('limit').attrib.items()}
                       for j in root.findall('joint') if j.find('limit') is not None}

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
            if predicate():
                return True
        return False

    def get_joint_state(self):
        sample = self.samples
        if not self.wait_for(lambda: self.samples > sample and all(j in self.limits for j in ARM + [HAND]), 5):
            raise RuntimeError('Fresh complete joint state or Gazebo URDF unavailable')
        return {'positions': dict(self.positions), 'limits': {j: self.limits[j] for j in ARM + [HAND]},
                'unit': 'radian', 'max_step_radian': MAX_STEP, 'demo_limits': dict(DEMO_LIMITS),
                'duration_seconds': [1, 15]}

    def move_joints(self, targets, duration):
        if not isinstance(targets, dict) or not targets or set(targets) - set(ARM + [HAND]):
            raise ValueError('Nonempty mapping of known joint names required')
        if type(duration) not in (float, int) or not math.isfinite(duration) or not 1 <= duration <= 15:
            raise ValueError('Duration must be finite and between 1 and 15 seconds')
        state = self.get_joint_state()['positions']
        groups = {('hand' if j == HAND else 'arm') for j in targets}
        if len(groups) != 1:
            raise ValueError('Move arm and gripper in separate operations')
        group = groups.pop()
        names = ARM if group == 'arm' else [HAND]
        for joint, value in targets.items():
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError('Target must be a finite number')
            limit = self.limits[joint]
            if not limit['lower'] <= value <= limit['upper']:
                raise ValueError(f'{joint}: outside URDF limits')
            if abs(value) > DEMO_LIMITS[joint]:
                raise ValueError(f'{joint}: outside +/-{DEMO_LIMITS[joint]} rad demonstration envelope')
            if abs(value - state[joint]) > MAX_STEP:
                raise ValueError(f'{joint}: exceeds {MAX_STEP} rad step limit')
        publisher = self.publishers_by_group[group]
        if not self.wait_for(lambda: publisher.get_subscription_count() > 0, 3):
            raise RuntimeError('No controller subscriber')
        goal = {j: targets.get(j, state[j]) for j in names}
        msg = JointTrajectory()
        msg.joint_names = names
        point = JointTrajectoryPoint()
        point.positions = [float(goal[j]) for j in names]
        nanos = round(duration * 1e9)
        point.time_from_start.sec, point.time_from_start.nanosec = divmod(nanos, 10**9)
        msg.points = [point]
        start = time.monotonic()
        publisher.publish(msg)
        stable = [0]
        last_sample = [self.samples]

        def reached():
            if self.samples == last_sample[0]:
                return False
            last_sample[0] = self.samples
            if time.monotonic() - self.received > 0.5:
                stable[0] = 0
                return False
            if time.monotonic() - start < duration:
                return False
            stable[0] = stable[0] + 1 if all(abs(self.positions[j] - goal[j]) <= 0.005 for j in names) else 0
            return stable[0] >= 5

        success = self.wait_for(reached, duration * 4 + 5)
        return {'success': success, 'reason': 'reached' if success else 'arrival_timeout',
                'targets': goal, 'positions': dict(self.positions),
                'max_error': max(abs(self.positions[j] - goal[j]) for j in names),
                'elapsed_seconds': time.monotonic() - start}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['state', 'smoke'])
    args = parser.parse_args()
    rclpy.init()
    node = CraneControl()
    try:
        initial = node.get_joint_state()
        print(json.dumps(initial), flush=True)
        if args.command == 'smoke':
            joint = ARM[0]
            original = initial['positions'][joint]
            result = node.move_joints({joint: original + 0.05}, 2.0)
            print(json.dumps(result), flush=True)
            if not result['success']:
                raise RuntimeError('Outbound motion failed; return motion skipped')
            result = node.move_joints({joint: original}, 2.0)
            print(json.dumps(result), flush=True)
            if not result['success']:
                raise RuntimeError('Return motion failed')
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
