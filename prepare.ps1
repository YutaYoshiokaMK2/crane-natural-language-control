$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    docker exec pai_ros2 mkdir -p /tmp/crane-nlc
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and pai_ros2 first.' }
    foreach ($file in @('crane_control.py', 'motion_policy.py', 'instruction_contract.py', 'mcp_server.py', 'mcp_smoke.py', 'mcp_connection.py', 'local_client.py', 'visible_demo.py', 'focus_camera.py', 'verify_timeout.py', 'requirements.txt')) {
        docker cp $file "pai_ros2:/tmp/crane-nlc/$file"
        if ($LASTEXITCODE -ne 0) { throw "Copy failed: $file" }
    }
    docker exec -u ubuntu pai_ros2 python3 -m pip install --target /tmp/crane-final-deps -r /tmp/crane-nlc/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'MCP dependency installation failed.' }
} finally {
    Pop-Location
}
