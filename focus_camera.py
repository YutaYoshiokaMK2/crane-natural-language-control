"""Move only the Gazebo GUI camera closer to the tabletop CRANE+."""
import math
import subprocess
import time


def main():
    yaw = -3 * math.pi / 4
    pitch = math.atan2(0.12, math.hypot(0.22, 0.22))
    sy, cy = math.sin(yaw / 2), math.cos(yaw / 2)
    sp, cp = math.sin(pitch / 2), math.cos(pitch / 2)
    request = ('pose { position { x: 0.22 y: 0.22 z: 1.3 } orientation { '
               f'x: {-sy * sp} y: {cy * sp} z: {sy * cp} w: {cy * cp}' + ' } }')
    subprocess.run(['ign', 'service', '-s', '/gui/move_to/pose', '--reqtype', 'ignition.msgs.GUICamera',
                    '--reptype', 'ignition.msgs.Boolean', '--timeout', '3000', '--req', request],
                   check=True, timeout=6)
    time.sleep(3)  # Allow the GUI camera animation to finish.


if __name__ == '__main__':
    main()
