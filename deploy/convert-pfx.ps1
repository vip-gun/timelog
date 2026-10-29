<#
.SYNOPSIS
  Convert the PFX file issued by the PKI into certs\server.crt + certs\server.key with the application image.
.EXAMPLE
  .\convert-pfx.ps1 C:\temp\server.pfx      # asks for the PFX password
#>
param([Parameter(Mandatory = $true)][string]$Pfx)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$Version = (Get-Content VERSION -Raw).Trim()
New-Item -ItemType Directory -Force -Path certs | Out-Null
Copy-Item $Pfx certs\.import.pfx
try {
  & docker run --rm -it --user 0:0 -v "$PWD\certs:/work" --entrypoint python "timelog-app:$Version" -m timelog.certtool pfx2pem /work/.import.pfx /work
  if ($LASTEXITCODE -ne 0) { throw "conversion failed" }
} finally { Remove-Item certs\.import.pfx -Force -ErrorAction SilentlyContinue }
Write-Host "Now apply it:  docker compose restart proxy"
