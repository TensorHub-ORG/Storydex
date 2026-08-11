[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$PreferredPort = 5173,

    [ValidateRange(1, 1000)]
    [int]$Attempts = 20
)

$ErrorActionPreference = "Stop"

for ($offset = 0; $offset -lt $Attempts; $offset++) {
    $port = $PreferredPort + $offset
    if ($port -gt 65535) {
        break
    }

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $port
    )
    try {
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        Write-Output $port
        exit 0
    }
    catch [System.Net.Sockets.SocketException] {
        continue
    }
    finally {
        $listener.Stop()
    }
}

Write-Error "No available loopback TCP port found from $PreferredPort across $Attempts attempts."
exit 1
