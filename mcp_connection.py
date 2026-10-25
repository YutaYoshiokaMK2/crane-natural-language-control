"""Build a stdio server command for Windows host or sourced ROS Linux shell."""
import os
from pathlib import Path
import sys
from mcp import StdioServerParameters


def server_parameters(use_docker=False):
    if use_docker:
        shell = ('source /opt/ros/humble/setup.bash; '
                 'source /home/ubuntu/airobot_ws/install/setup.bash; '
                 'export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; '
                 'export PYTHONPATH=/tmp/crane-final-deps:$PYTHONPATH; '
                 'exec python3 /tmp/crane-nlc/mcp_server.py')
        return StdioServerParameters(command='docker', args=['exec', '-i', '-u', 'ubuntu', 'pai_ros2', 'bash', '-lc', shell], env=dict(os.environ))
    return StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('mcp_server.py'))], env=dict(os.environ))
