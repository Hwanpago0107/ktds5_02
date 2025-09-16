Param(
  [Parameter(Mandatory=$true)][string]$ResourceGroup,
  [Parameter(Mandatory=$true)][string]$AcrName,
  [Parameter(Mandatory=$true)][string]$AppName,
  [Parameter(Mandatory=$true)][string]$ImageName,
  [string]$ImageTag = 'v2',
  [string]$BuildContext = '.\\KTDS5_02_PROJECT',
  [switch]$SetAppSettings,
  [string]$AoaiChatDeployment = 'gpt-4-1',
  [string]$AoaiEmbedDeployment = 'text-embedding-3-small',
  [switch]$TailLogs,
  # Optional: Infobip notify configs
  [string]$InfobipHost,
  [string]$InfobipKey,
  [string]$InfobipSender = 'InfoSMS',
  [string]$NotifyRecipient
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function RunAz {
  param([Parameter(Mandatory=$true)][string[]]$CmdArgs)
  if (-not $CmdArgs -or $CmdArgs.Count -eq 0) { throw 'BUG: RunAz requires args' }
  Write-Host (">> az " + ($CmdArgs -join ' ')) -ForegroundColor DarkCyan
  # Stream output directly so long-running commands show progress
  & az @CmdArgs
  $code = $LASTEXITCODE
  if ($code -ne 0) { throw "Azure CLI failed (exit $code) for: az $($CmdArgs -join ' ')" }
}

Write-Host '== Build image to ACR ==' -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $BuildContext)) { throw "Build context not found: $BuildContext" }
RunAz -CmdArgs @('acr','build','-r',$AcrName,'-t',"${ImageName}:$ImageTag",$BuildContext)

Write-Host '== Resolve ACR login server ==' -ForegroundColor Cyan
$AcrLogin = (& az acr show -n $AcrName -g $ResourceGroup --query loginServer -o tsv).Trim()
if ([string]::IsNullOrWhiteSpace($AcrLogin)) { throw "Failed to get ACR login server for $AcrName" }
Write-Host ("ACR login: $AcrLogin")

Write-Host '== Verify image tag exists in ACR ==' -ForegroundColor Cyan
try {
  $tags = (& az acr repository show-tags -n $AcrName --repository $ImageName -o tsv)
  if (-not $tags -or -not ($tags -split "`n" | Where-Object { $_.Trim() -eq $ImageTag })) {
    Write-Host "WARN: Tag '$ImageTag' not found under repository '$ImageName' in ACR '$AcrName'." -ForegroundColor Yellow
    Write-Host "      The build may have failed or pushed to a different repo."
  }
} catch {
  Write-Host "WARN: Unable to list ACR tags for ${ImageName}: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host '== Ensure Web App managed identity and AcrPull role ==' -ForegroundColor Cyan
try { RunAz -CmdArgs @('webapp','identity','assign','-g',$ResourceGroup,'-n',$AppName) } catch { }
$PrincipalId = (& az webapp identity show -g $ResourceGroup -n $AppName --query principalId -o tsv).Trim()
$AcrId       = (& az acr show -n $AcrName -g $ResourceGroup --query id -o tsv).Trim()
if ($PrincipalId -and $AcrId) {
  try { RunAz -CmdArgs @('role','assignment','create','--assignee',$PrincipalId,'--role','AcrPull','--scope',$AcrId) } catch { Write-Host "AcrPull role may already exist." -ForegroundColor Yellow }
} else {
  Write-Host 'WARN: missing principalId or ACR id; skip role assignment' -ForegroundColor Yellow
}

Write-Host '== Remove legacy registry app settings (if any) ==' -ForegroundColor Cyan
try {
  RunAz -CmdArgs @('webapp','config','appsettings','delete','-g',$ResourceGroup,'-n',$AppName,'--setting-names',
    'DOCKER_REGISTRY_SERVER_URL','DOCKER_REGISTRY_SERVER_USERNAME','DOCKER_REGISTRY_SERVER_PASSWORD','DOCKER_CUSTOM_IMAGE_NAME')
} catch {
  Write-Host 'Skipping cleanup of legacy DOCKER_* settings.' -ForegroundColor Yellow
}

Write-Host '== Update Web App container image ==' -ForegroundColor Cyan
$ImageFull = "$AcrLogin/${ImageName}:$ImageTag"
RunAz -CmdArgs @('webapp','config','container','set','-g',$ResourceGroup,'-n',$AppName,'--container-image-name',$ImageFull,'--container-registry-url',"https://$AcrLogin")
RunAz -CmdArgs @('webapp','config','set','-g',$ResourceGroup,'-n',$AppName,'--acr-use-identity','true','--acr-identity','[system]')

if ($SetAppSettings.IsPresent) {
  Write-Host '== Ensure essential app settings ==' -ForegroundColor Cyan
  $settings = @(
    'WEBSITES_PORT=8080',
    'AOAI_API_VERSION=2025-01-01-preview',
    'AOAI_API_EMBED_VERSION=2024-10-21',
    'SEARCH_INDEX=kb-playbook',
    'STORAGE_BACKEND=sqlite',
    'SQLITE_PATH=/home/data/app.db',
    ("AOAI_DEPLOYMENT_CHAT=$AoaiChatDeployment"),
    ("AOAI_DEPLOYMENT_EMBED=$AoaiEmbedDeployment")
  )
  if ($InfobipHost)    { $settings += "INFOBIP_API_HOST=$InfobipHost" }
  if ($InfobipKey)     { $settings += "INFOBIP_API_KEY=$InfobipKey" }
  if ($InfobipSender)  { $settings += "INFOBIP_SENDER=$InfobipSender" }
  if ($NotifyRecipient) { $settings += "NOTIFY_RECIPIENT=$NotifyRecipient" }
  $cmd = @('webapp','config','appsettings','set','-g',$ResourceGroup,'-n',$AppName,'--settings') + $settings
  RunAz -CmdArgs $cmd
  # Ensure WEBHOOK_API_BASE is not set in production so Streamlit calls same-origin '/api/...'
  try { RunAz -CmdArgs @('webapp','config','appsettings','delete','-g',$ResourceGroup,'-n',$AppName,'--setting-names','WEBHOOK_API_BASE') } catch { }
}

Write-Host '== Enable logging and restart ==' -ForegroundColor Cyan
RunAz -CmdArgs @('webapp','log','config','-g',$ResourceGroup,'-n',$AppName,'--docker-container-logging','filesystem')
RunAz -CmdArgs @('webapp','restart','-g',$ResourceGroup,'-n',$AppName)

Write-Host ("Health: https://$AppName.azurewebsites.net/healthz")
Write-Host ("App   : https://$AppName.azurewebsites.net/")

if ($TailLogs.IsPresent) {
  Write-Host '== Tailing logs (Ctrl+C to stop) ==' -ForegroundColor Cyan
  & az webapp log tail -g $ResourceGroup -n $AppName
}
