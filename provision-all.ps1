<#
Provision Azure resources for this app end-to-end:
  - Resource Group
  - Azure OpenAI (Cognitive Services, kind=OpenAI)
  - Azure AI Search
  - Azure Container Registry (ACR)
  - App Service Plan (Linux)
  - Web App for Containers (pulls from ACR via Managed Identity)
  - App Settings wired from created endpoints/keys

Usage examples:
  pwsh ./project_ktds/provision-all.ps1 -SubscriptionId <SUB> -Location koreacentral \
    -ResourceGroup rg-ktds -BaseName ktdsapp -AutoFallbackRegion \
    -ContextPath ./project_ktds

  # Custom names (must be globally unique for ACR/App):
  pwsh ./project_ktds/provision-all.ps1 -SubscriptionId <SUB> -Location koreacentral \
    -ResourceGroup rg-ktds -BaseName ktdsapp -AcrName acrktds1234 -AppName app-ktds1234 \
    -PlanName plan-ktds -ImageName ktds -ImageTag v1 -ContextPath ./project_ktds

Notes:
  - Requires Azure CLI (az) logged in and access to the subscription.
  - Azure OpenAI and model availability varies per region.
  - The script builds Docker image from -ContextPath (expects Dockerfile inside).
#>

param(
  [Parameter(Mandatory=$true)][string]$SubscriptionId,
  [Parameter(Mandatory=$true)][string]$Location,
  [Parameter(Mandatory=$true)][string]$ResourceGroup,
  [string]$BaseName = "ktdsapp",

  # Container / App Service
  [string]$AcrName,
  [string]$PlanName,
  [string]$AppName,
  [string]$ImageName = "ktds-image",
  [string]$ImageTag = "latest",
  [int]$ContainerPort = 8080,
  [string]$PlanSku = "B1",
  [switch]$SkipBuild,
  [switch]$AutoFallbackRegion,
  [string[]]$FallbackRegions = @("koreacentral","koreasouth","eastasia","southeastasia","japaneast","japanwest","eastus","eastus2","westus2"),

  # AI services
  [string]$OpenAIName,
  [string]$SearchName,
  [string]$SearchSku = "basic",
  [string]$AoaiDeploymentChat = "gpt-4o-mini",
  [string]$AoaiDeploymentEmbed = "text-embedding-3-small",
  [string]$AoaiApiVersion = "2024-10-21",
  [switch]$CreateAoaiDeployments,
  [string]$AoaiChatModel = "gpt-4o-mini",
  [string]$AoaiChatVersion = "2024-10-21",
  [string]$AoaiEmbedModel = "text-embedding-3-small",
  [string]$AoaiEmbedVersion = "1",
  [string]$AoaiChatSku = "GlobalStandard",
  [string]$AoaiEmbedSku = "Standard",
  [int]$AoaiChatCapacity = 1,
  [int]$AoaiEmbedCapacity = 1,

  # Build context for ACR build
  [string]$ContextPath = "."
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Az {
  [CmdletBinding()]
  param([Parameter(Mandatory=$true)][string[]]$Args)
  if (-not $Args -or $Args.Count -eq 0) { throw "BUG: Invoke-Az called with no arguments" }
  $cmdline = 'az ' + ($Args -join ' ')
  Write-Host ">> $cmdline" -ForegroundColor DarkCyan

  # Use Start-Process to prevent stderr lines from becoming PowerShell errors
  $tmpOut = [System.IO.Path]::GetTempFileName()
  $tmpErr = [System.IO.Path]::GetTempFileName()
  try {
    $p = Start-Process -FilePath "az" -ArgumentList $Args -NoNewWindow -PassThru -Wait -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
    $code = $p.ExitCode
    $stdout = Get-Content -LiteralPath $tmpOut -ErrorAction SilentlyContinue
    $stderr = Get-Content -LiteralPath $tmpErr -ErrorAction SilentlyContinue
    if ($stdout) { $stdout | ForEach-Object { Write-Host $_ } }
    if ($stderr) { $stderr | ForEach-Object { Write-Host $_ } }
    if ($code -ne 0) { throw ("Azure CLI failed (exit {0}) for: {1}" -f $code, $cmdline) }
    return $stdout
  } finally {
    Remove-Item -LiteralPath $tmpOut, $tmpErr -ErrorAction SilentlyContinue
  }
}

function Ensure-LoggedIn {
  try { Invoke-Az @('account','show','-o','none') } catch { Invoke-Az @('login','-o','none') }
}

function Ensure-Subscription {
  param([string]$SubId)
  Invoke-Az @('account','set','--subscription',$SubId)
}

function Ensure-ResourceGroup {
  param([string]$Name,[string]$Loc)
  Invoke-Az @('group','create','-n',$Name,'-l',$Loc,'--only-show-errors') | Out-Null
}

function Sanitize-NameLower {
  param([string]$Base,[int]$MinLen=5,[int]$MaxLen=40)
  $s = ($Base.ToLower() -replace '[^a-z0-9]', '')
  if ($s.Length -lt $MinLen) { $s = ($s + (Get-Random -Maximum 99999)) }
  if ($s.Length -gt $MaxLen) { $s = $s.Substring(0,$MaxLen) }
  return $s
}

function Ensure-OpenAI {
  param([string]$Name,[string]$Rg,[string]$Loc)
  $exists = $false
  try { Invoke-Az @('cognitiveservices','account','show','-n',$Name,'-g',$Rg,'-o','none'); $exists = $true } catch { $exists = $false }
  if (-not $exists) {
    Invoke-Az @('cognitiveservices','account','create','-n',$Name,'-g',$Rg,'-l',$Loc,'--kind','OpenAI','--sku','S0','--yes','--custom-domain',$Name,'--only-show-errors')
  }
  $endpoint = (& az cognitiveservices account show -n $Name -g $Rg --query "properties.endpoint" -o tsv).Trim()
  $key = (& az cognitiveservices account keys list -n $Name -g $Rg --query "key1" -o tsv).Trim()
  return @{ endpoint=$endpoint; key=$key }
}

function Ensure-Aoai-Deployment {
  param(
    [string]$Rg,[string]$Account,
    [string]$DeploymentName,[string]$ModelName,[string]$ModelVersion,[string]$SkuName,[Nullable[int]]$SkuCapacity
  )
  if (-not $DeploymentName -or -not $ModelName -or -not $ModelVersion) { return }
  $exists = $false
  try {
    $names = (& az cognitiveservices account deployment list -g $Rg -n $Account -o tsv --query "[].name")
    if ($names -and ($names -join ' ') -match ("\b" + [regex]::Escape($DeploymentName) + "\b")) { $exists = $true }
  } catch { $exists = $false }
  if ($exists) { return }
  $args = @('cognitiveservices','account','deployment','create','-g',$Rg,'-n',$Account,
    '--deployment-name',$DeploymentName,'--model-format','OpenAI','--model-name',$ModelName,'--model-version',$ModelVersion)
  if ($SkuName) { $args += @('--sku-name',$SkuName) }
  if ($SkuCapacity -ne $null) { $args += @('--sku-capacity',([string]$SkuCapacity)) }
  Invoke-Az -Args $args | Out-Null
}

function Ensure-Search {
  param([string]$Name,[string]$Rg,[string]$Loc,[string]$Sku)
  $exists = $false
  try { Invoke-Az @('search','service','show','--name',$Name,'-g',$Rg,'-o','none'); $exists = $true } catch { $exists = $false }
  if (-not $exists) {
    Invoke-Az @('search','service','create','--name',$Name,'-g',$Rg,'-l',$Loc,'--sku',$Sku,'--only-show-errors')
  }
  $endpoint = "https://$Name.search.windows.net"
  # ensure a query key
  $qk = ''
  try {
    $qk = (& az search query-key list --service-name $Name -g $Rg --query "[0].key" -o tsv).Trim()
  } catch { }
  if (-not $qk) { Invoke-Az @('search','query-key','create','--service-name',$Name,'-g',$Rg,'--name','default') | Out-Null }
  $qk = (& az search query-key list --service-name $Name -g $Rg --query "[0].key" -o tsv).Trim()
  $adminKey = (& az search admin-key show --service-name $Name -g $Rg --query "primaryKey" -o tsv).Trim()
  return @{ endpoint=$endpoint; queryKey=$qk; adminKey=$adminKey }
}

function Ensure-Acr {
  param([string]$Name,[string]$Rg,[string]$Loc)
  $exists = $false
  try { Invoke-Az @('acr','show','-n',$Name,'-g',$Rg,'-o','none'); $exists = $true } catch { $exists = $false }
  if (-not $exists) {
    Invoke-Az @('acr','create','-n',$Name,'-g',$Rg,'--sku','Basic','-l',$Loc,'--only-show-errors')
  }
  $loginServer = (& az acr show -n $Name -g $Rg --query loginServer -o tsv).Trim()
  if ([string]::IsNullOrWhiteSpace($loginServer)) { throw "Failed to get ACR login server for $Name" }
  return $loginServer
}

function Acr-Build {
  param([string]$Acr,[string]$Image,[string]$Tag,[string]$Context)
  if (-not (Test-Path -LiteralPath $Context)) { throw "Build context not found: $Context" }
  Invoke-Az @('acr','build','-r',$Acr,'-t',("${Image}:$Tag"),$Context,'--only-show-errors')
}

function Try-Create-Plan {
  param([string]$Rg,[string]$Plan,[string]$Loc,[string]$Sku)
  try {
    Invoke-Az @('appservice','plan','create','-g',$Rg,'-n',$Plan,'--is-linux','--sku',$Sku,'-l',$Loc,'--only-show-errors') | Out-Null
    return $true
  } catch {
    $msg = $_.Exception.Message
    if ($msg -match '(?i)quota' -or $msg -match '(?i)Operation cannot be completed without additional quota') { return $false }
    throw
  }
}

function Ensure-AppServicePlan {
  param([string]$Rg,[string]$Plan,[string]$Loc,[string]$Sku,[switch]$AllowFallback,[string[]]$Regions)
  $exists = $false
  try { Invoke-Az @('appservice','plan','show','-g',$Rg,'-n',$Plan,'-o','none'); $exists = $true } catch { $exists = $false }
  if ($exists) { return @{ created=$false; location=$Loc } }
  if (Try-Create-Plan -Rg $Rg -Plan $Plan -Loc $Loc -Sku $Sku) { return @{ created=$true; location=$Loc } }
  if ($AllowFallback.IsPresent) {
    foreach ($r in $Regions) {
      Write-Host ("[WARN] Quota in {0}. Trying fallback region: {1}" -f $Loc,$r) -ForegroundColor Yellow
      if (Try-Create-Plan -Rg $Rg -Plan $Plan -Loc $r -Sku $Sku) { return @{ created=$true; location=$r } }
    }
    throw "Quota exhausted in $Loc and fallbacks. Request quota increase (Microsoft.Web) or use existing plan."
  } else {
    throw "Quota exhausted in $Loc. Re-run with -AutoFallbackRegion or change region/SKU."
  }
}

function Ensure-WebApp {
  param([string]$Rg,[string]$Plan,[string]$App,[string]$ImageFull)
  $exists = $false
  try { Invoke-Az @('webapp','show','-g',$Rg,'-n',$App,'-o','none'); $exists = $true } catch { $exists = $false }
  if (-not $exists) {
    # Create a Linux Web App (runtime is temporary; container config will override)
    Invoke-Az @('webapp','create','-g',$Rg,'-p',$Plan,'-n',$App,'--runtime','PYTHON:3.11')
  }
  Invoke-Az @('webapp','identity','assign','-g',$Rg,'-n',$App) | Out-Null
  $registryUrl = 'https://' + ($ImageFull.Split('/')[0])
  Invoke-Az @('webapp','config','container','set','-g',$Rg,'-n',$App,'--container-image-name',$ImageFull,'--container-registry-url',$registryUrl)
  Invoke-Az @('webapp','config','set','-g',$Rg,'-n',$App,'--acr-use-identity','true','--acr-identity','[system]') | Out-Null
}

function Ensure-AcrPull-Role {
  param([string]$Rg,[string]$App,[string]$AcrName)
  try {
    $principalId = (& az webapp identity show -g $Rg -n $App --query principalId -o tsv).Trim()
    $acrId = (& az acr show -n $AcrName -g $Rg --query id -o tsv).Trim()
    if ($principalId -and $acrId) {
      Write-Host "[INFO] Granting AcrPull to Web App identity on ACR" -ForegroundColor DarkYellow
      Invoke-Az @('role','assignment','create','--assignee',$principalId,'--role','AcrPull','--scope',$acrId) | Out-Null
    }
  } catch {
    Write-Host "[WARN] AcrPull role assignment may already exist: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

function Set-WebApp-Port {
  param([string]$Rg,[string]$App,[int]$Port)
  Invoke-Az @('webapp','config','appsettings','set','-g',$Rg,'-n',$App,'--settings',("WEBSITES_PORT={0}" -f $Port)) | Out-Null
}

function Set-WebApp-Settings {
  param([string]$Rg,[string]$App,[hashtable]$Settings)
  if (-not $Settings -or $Settings.Keys.Count -eq 0) { return }
  $pairs = @()
  foreach ($k in $Settings.Keys) { $pairs += ("{0}={1}" -f $k, $Settings[$k]) }
  Invoke-Az @('webapp','config','appsettings','set','-g',$Rg,'-n',$App,'--settings',@($pairs)) | Out-Null
}

# ----------------- main -----------------
Ensure-LoggedIn
Ensure-Subscription -SubId $SubscriptionId

if (-not $AcrName)   { $AcrName   = Sanitize-NameLower -Base ("acr" + $BaseName) -MaxLen 50 }
if (-not $PlanName)  { $PlanName  = ("plan-" + $BaseName) }
if (-not $AppName)   { $AppName   = Sanitize-NameLower -Base ("app-" + $BaseName) -MaxLen 60 }
if (-not $OpenAIName){ $OpenAIName= Sanitize-NameLower -Base ("aoai" + $BaseName) -MaxLen 44 }
if (-not $SearchName){ $SearchName= Sanitize-NameLower -Base ("srch" + $BaseName) -MaxLen 60 }

Write-Host "[0/9] Target subscription: $SubscriptionId" -ForegroundColor Yellow
Write-Host "[1/9] Ensure resource group: $ResourceGroup ($Location)" -ForegroundColor Yellow
Ensure-ResourceGroup -Name $ResourceGroup -Loc $Location

Write-Host "[2/9] Ensure Azure OpenAI: $OpenAIName" -ForegroundColor Yellow
$aoai = Ensure-OpenAI -Name $OpenAIName -Rg $ResourceGroup -Loc $Location

Write-Host "[3/9] Ensure Azure AI Search: $SearchName" -ForegroundColor Yellow
$search = Ensure-Search -Name $SearchName -Rg $ResourceGroup -Loc $Location -Sku $SearchSku

Write-Host "[4/9] Ensure ACR: $AcrName" -ForegroundColor Yellow
$acrLogin = Ensure-Acr -Name $AcrName -Rg $ResourceGroup -Loc $Location

Write-Host "[5/9] ACR build image: $($ImageName):$($ImageTag) (context=$ContextPath)" -ForegroundColor Yellow
if ($SkipBuild.IsPresent) {
  Write-Host "[SKIP] Build skipped by -SkipBuild" -ForegroundColor DarkYellow
} else {
  Acr-Build -Acr $AcrName -Image $ImageName -Tag $ImageTag -Context $ContextPath
}
$imageFull = "{0}/{1}:{2}" -f $acrLogin, $ImageName, $ImageTag
Write-Host ("[INFO] Image: {0}" -f $imageFull) -ForegroundColor DarkYellow

Write-Host "[6/9] Ensure App Service Plan (Linux): $PlanName ($PlanSku)" -ForegroundColor Yellow
$plan = Ensure-AppServicePlan -Rg $ResourceGroup -Plan $PlanName -Loc $Location -Sku $PlanSku -AllowFallback:$AutoFallbackRegion -Regions $FallbackRegions
if ($plan.created) { Write-Host ("[INFO] Plan created in region: {0}" -f $plan.location) -ForegroundColor DarkYellow }

Write-Host "[7/9] Ensure Web App (container): $AppName" -ForegroundColor Yellow
Ensure-WebApp -Rg $ResourceGroup -Plan $PlanName -App $AppName -ImageFull $imageFull
Set-WebApp-Port -Rg $ResourceGroup -App $AppName -Port $ContainerPort
Ensure-AcrPull-Role -Rg $ResourceGroup -App $AppName -AcrName $AcrName

if ($CreateAoaiDeployments.IsPresent) {
  Write-Host "[INFO] Creating Azure OpenAI deployments (chat/embedding)" -ForegroundColor Yellow
  Ensure-Aoai-Deployment -Rg $ResourceGroup -Account $OpenAIName -DeploymentName $AoaiDeploymentChat -ModelName $AoaiChatModel -ModelVersion $AoaiChatVersion -SkuName $AoaiChatSku -SkuCapacity $AoaiChatCapacity
  Ensure-Aoai-Deployment -Rg $ResourceGroup -Account $OpenAIName -DeploymentName $AoaiDeploymentEmbed -ModelName $AoaiEmbedModel -ModelVersion $AoaiEmbedVersion -SkuName $AoaiEmbedSku -SkuCapacity $AoaiEmbedCapacity
}

Write-Host "[8/9] Configure App Settings from created resources" -ForegroundColor Yellow
$appSettings = @{
  'AZURE_OPENAI_ENDPOINT' = $aoai.endpoint
  'AZURE_OPENAI_KEY'      = $aoai.key
  'AOAI_DEPLOYMENT_CHAT'  = $AoaiDeploymentChat
  'AOAI_DEPLOYMENT_EMBED' = $AoaiDeploymentEmbed
  'AOAI_API_VERSION'      = $AoaiApiVersion
  'AOAI_API_EMBED_VERSION'= '2024-10-21'
  'SEARCH_ENDPOINT'       = $search.endpoint
  'SEARCH_QUERY_KEY'      = $search.queryKey
  # Optional but handy for this app
  'STORAGE_BACKEND'       = 'sqlite'
  'SQLITE_PATH'           = '/home/data/app.db'
  'WEBHOOK_API_BASE'      = 'http://127.0.0.1:9000'
}
Set-WebApp-Settings -Rg $ResourceGroup -App $AppName -Settings $appSettings

Write-Host "[9/9] Write .env for local dev" -ForegroundColor Yellow
@"
SUBSCRIPTION_ID=$SubscriptionId
RESOURCE_GROUP=$ResourceGroup
LOCATION=$Location

OPENAI_NAME=$OpenAIName
SEARCH_NAME=$SearchName

AZURE_OPENAI_ENDPOINT=$($aoai.endpoint)
AZURE_OPENAI_KEY=$($aoai.key)
AOAI_DEPLOYMENT_CHAT=$AoaiDeploymentChat
AOAI_DEPLOYMENT_EMBED=$AoaiDeploymentEmbed
AOAI_API_VERSION=$AoaiApiVersion

SEARCH_ENDPOINT=$($search.endpoint)
SEARCH_QUERY_KEY=$($search.queryKey)
SEARCH_ADMIN_KEY=$($search.adminKey)
SEARCH_INDEX=kb-playbook

STORAGE_BACKEND=sqlite
SQLITE_PATH=./app.db
"@ | Out-File -FilePath .env -Encoding utf8 -Force

Write-Host "" -ForegroundColor Green
Write-Host ("[DONE] Web App: https://{0}.azurewebsites.net" -f $AppName) -ForegroundColor Green
Write-Host "TIP: Create Azure OpenAI deployments for chat/embeddings if not present (Portal -> Deployments)." -ForegroundColor DarkYellow
Write-Host "TIP: For local dev, load .env with: . ./project_ktds/import-dotenv.ps1; Import-DotEnv -Path ./.env" -ForegroundColor DarkYellow
