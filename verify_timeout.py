"""Gazebo failure-path check. Pauses this simulator; run with no other clients."""
import json
import subprocess
import threading

import rclpy
from crane_control import CraneControl


def pause(value):
    subprocess.run(['ign', 'service', '-s', '/world/default/control', '--reqtype', 'ignition.msgs.WorldControl',
                    '--reptype', 'ignition.msgs.Boolean', '--timeout', '2000', '--req', f'pause: {value}'],
                   check=True, timeout=5)


def main():
    rclpy.init()
    node = CraneControl()
    timer = None
    original = None
    try:
        original = node.get_joint_state()['positions']['crane_plus_joint1']
        timer = threading.Timer(0.7, pause, args=('true',))
        timer.start()
        result = node.move_joints({'crane_plus_joint1': original + 0.03}, 1)
        print(json.dumps({'paused_motion': result}), flush=True)
        assert not result['success'] and result['reason'] == 'arrival_timeout'
        try:
            node.get_joint_state()
        except RuntimeError:
            print('Paused simulator: fresh state request correctly failed', flush=True)
        else:
            raise AssertionError('Stale state accepted')
    finally:
        if timer:
            timer.cancel()
            timer.join()
        pause('false')
        try:
            if original is not None:
                result = node.move_joints({'crane_plus_joint1': original}, 2)
                print(json.dumps({'restored_motion': result}), flush=True)
                assert result['success']
        finally:
            node.destroy_node()
            rclpy.shutdown()


if __name__ == '__main__':
    main()
