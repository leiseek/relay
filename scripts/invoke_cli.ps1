param([Parameter(Mandatory = $true)][string]$RequestFile)

$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$request = Get-Content -LiteralPath $RequestFile -Raw -Encoding utf8 | ConvertFrom-Json
$cliArguments = @($request.arguments | ForEach-Object { [string]$_ })
& ([string]$request.executable) @cliArguments
if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }
