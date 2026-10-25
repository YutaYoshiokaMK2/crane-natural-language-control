$ErrorActionPreference = 'Stop'
docker start pai_ros2
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop first.' }
# Closing a GUI does not necessarily stop the ROS launch or simulator server.
$existing = docker exec pai_ros2 pgrep -f crane_plus_with_table.launch.py
if ($LASTEXITCODE -eq 0) {
    Write-Host 'CRANE+ Gazebo launch is already running. Skipping duplicate launch.'
    Write-Host 'View the existing desktop at http://localhost:6080 .'
    return
}
if ($LASTEXITCODE -ne 1) { throw 'Could not check the existing Gazebo launch.' }
$existingGazebo = docker exec pai_ros2 pgrep -f 'ign gazebo'
if ($LASTEXITCODE -eq 0) {
    throw 'A Gazebo process is still running without the CRANE+ launch. Stop the remaining simulator before launching again.'
}
if ($LASTEXITCODE -ne 1) { throw 'Could not check the existing Gazebo processes.' }
docker exec -d -u ubuntu -e DISPLAY=:0 pai_ros2 bash -lc 'source /opt/ros/humble/setup.bash; source /home/ubuntu/airobot_ws/install/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; ros2 launch crane_plus_gazebo crane_plus_with_table.launch.py > /tmp/crane-final-gazebo.log 2>&1'
if ($LASTEXITCODE -ne 0) { throw 'Gazebo launch failed.' }
Write-Host 'View the desktop at http://localhost:6080 . Check /tmp/crane-final-gazebo.log and controller states before moving.'
