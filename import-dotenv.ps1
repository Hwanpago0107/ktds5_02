<#
.SYNOPSIS
  Load a .env file (KEY=VALUE lines) into PowerShell environment variables.

.DESCRIPTION
  - 기본값: 현재 세션($env:)에만 적용
  - -Persist 스위치 사용 시: setx를 통해 사용자 환경변수로 영구 등록(새 세션부터 반영)
  - 주석(#) / 빈 줄 / export PREFIX / 따옴표 제거("...",'...') 지원
  - BOM/UTF-8 파일 지원

.PARAMETER Path
  .env 파일 경로 (기본: .\.env)

.PARAMETER Persist
  setx로 영구 등록 (관리자 권한 불필요, 새 창에서 반영됨)

.EXAMPLE
  . .\import-dotenv.ps1
  Import-DotEnv                 # .\.env 로드(세션 한정)

.EXAMPLE
  . .\import-dotenv.ps1
  Import-DotEnv -Path .\.env -Persist   # 영구 등록(새 세션부터 유효)
#>

function Import-DotEnv {
  [CmdletBinding()]
  param(
    [string]$Path = ".\.env",
    [switch]$Persist
  )

  if (-not (Test-Path -LiteralPath $Path)) {
    throw "DotEnv file not found: $Path"
  }

  $lines = Get-Content -LiteralPath $Path -Encoding UTF8
  $varsSet = @{}

  foreach ($raw in $lines) {
    $line = $raw.Trim()
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    if ($line.StartsWith("#")) { continue }
    if ($line -match '^\s*export\s+') {
      $line = $line -replace '^\s*export\s+', ''
    }

    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { continue }

    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()

    if (($val.StartsWith('"') -and $val.EndsWith('"')) -or
        ($val.StartsWith("'") -and $val.EndsWith("'"))) {
      $val = $val.Substring(1, $val.Length - 2)
    }
    $val = $val.Trim()

    # ✅ 현재 세션 환경변수 반영
    Set-Item -Path Env:$key -Value $val
    $varsSet[$key] = $val

    # ✅ 영구 등록 (선택)
    if ($Persist) {
      setx $key $val | Out-Null
    }
  }

  Write-Host ("Loaded {0} variables from {1}{2}" -f $varsSet.Count, (Resolve-Path $Path), ($(if($Persist){" (persisted)"} else {""})))
  return $varsSet
}


# 편의: 지정 키 몇 개만 보기
function Show-DotEnv {
  param([string[]]$Keys)
  if (-not $Keys) { $Keys = (Get-ChildItem Env: | Select-Object -ExpandProperty Name) }
  foreach ($k in $Keys) {
    "{0}={1}" -f $k, (Get-Item -Path Env:\$k -ErrorAction SilentlyContinue).Value
  }
}
