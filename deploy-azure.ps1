# deploy-azure.fixed3.ps1
# Azure App Service (Web App for Containers) deploy via ACR build
# - Prints each az command before running
# - Protects against empty-arg az calls
# - Handles "quota" errors when creating App Service Plan:
#     * Optionally auto-fallback to another region
# - Uses Managed Identity to pull image from ACR
# - Sets WEBSITES_PORT for non-80 containers
param(
  [string]$Location           = "westus",
  [string]$ResourceGroup      = "rg-ktds5-02",
  [string]$AcrName            = "acrktds502",
  [string]$PlanName           = "plan-ktds5-02",
  [string]$AppName            = "app-ktds5-02",
  [string]$ImageName          = "sms-ktds5-02",
  [string]$ImageTag           = "latest",
  [int]   $ContainerPort      = 8501,
  [string]$Sku                = "B1",
  [switch]$AutoFallbackRegion,
  [string[]]$FallbackRegions  = @("eastus2","westus","westus2","swedencentral")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Az {
  [CmdletBinding()]
  param([Parameter(Mandatory=$true)][string[]]$Args)
  if (-not $Args -or $Args.Count -eq 0) { throw "BUG: Invoke-Az called with no arguments" }
  $cmdline = 'az ' + ($Args -join ' ')
  Write-Host ">> $cmdline" -ForegroundColor DarkCyan
  $out = & az @Args 2>&1
  $code = $LASTEXITCODE
  if ($out) { $out | ForEach-Object { Write-Host $_ } }
  if ($code -ne 0) { throw ("Azure CLI failed (exit {0}) for: {1}" -f $code, $cmdline) }
  return $out
}

function Ensure-LoggedIn {
  try { Invoke-Az @('account','show','-o','none') } catch { Invoke-Az @('login','-o','none') }
}

function Ensure-ResourceGroup {
  param([string]$Name,[string]$Loc)
  Invoke-Az @('group','create','-n',$Name,'-l',$Loc,'--only-show-errors')
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
  param([string]$Acr,[string]$Image,[string]$Tag)
  Invoke-Az @('acr','build','-r',$Acr,'-t',("${Image}:$Tag"),'.','--only-show-errors')
}

function Try-Create-Plan {
  param([string]$Rg,[string]$Plan,[string]$Loc,[string]$Sku)
  try {
    Invoke-Az @('appservice','plan','create','-g',$Rg,'-n',$Plan,'--is-linux','--sku',$Sku,'-l',$Loc,'--only-show-errors')
    return $true
  } catch {
    $msg = $_.Exception.Message
    if ($msg -match '(?i)quota' -or $msg -match '(?i)Operation cannot be completed without additional quota') {
      return $false
    }
    throw
  }
}

function Ensure-AppServicePlan {
  param([string]$Rg,[string]$Plan,[string]$Loc,[string]$Sku)
  $exists = $false
  try { Invoke-Az @('appservice','plan','show','-g',$Rg,'-n',$Plan,'-o','none'); $exists = $true } catch { $exists = $false }
  if ($exists) { return @{ created=$false; location=$Loc } }

  # First try desired region
  if (Try-Create-Plan -Rg $Rg -Plan $Plan -Loc $Loc -Sku $Sku) {
    return @{ created=$true; location=$Loc }
  }

  if ($PSBoundParameters.ContainsKey('AutoFallbackRegion') -and $AutoFallbackRegion.IsPresent) {
    foreach ($r in $FallbackRegions) {
      Write-Host ("[WARN] Quota in {0}. Trying fallback region: {1}" -f $Loc,$r) -ForegroundColor Yellow
      if (Try-Create-Plan -Rg $Rg -Plan $Plan -Loc $r -Sku $Sku) {
        return @{ created=$true; location=$r }
      }
    }
    throw "Quota exhausted in $Loc and all fallback regions. Request quota increase (Provider: Microsoft.Web) or use an existing plan."
  } else {
    throw "Quota exhausted in $Loc. Re-run with -AutoFallbackRegion or pick another region/SKU, or request a quota increase (Provider: Microsoft.Web)."
  }
}

function Ensure-WebApp {
  param([string]$Rg,[string]$Plan,[string]$App,[string]$ImageFull)
  $exists = $false
  try { Invoke-Az @('webapp','show','-g',$Rg,'-n',$App,'-o','none'); $exists = $true } catch { $exists = $false }
  if (-not $exists) {
    Invoke-Az @('webapp','create','-g',$Rg,'-p',$Plan,'-n',$App,'--deployment-container-image-name',$ImageFull)
  }
  Invoke-Az @('webapp','identity','assign','-g',$Rg,'-n',$App)
  $registryUrl = 'https://' + ($ImageFull.Split('/')[0])
  Invoke-Az @('webapp','config','container','set','-g',$Rg,'-n',$App,'--container-image-name',$ImageFull,'--container-registry-url',$registryUrl)
  Invoke-Az @('webapp','config','set','-g',$Rg,'-n',$App,'--acr-use-identity','true','--acr-identity','[system]')
}

function Set-WebApp-Port {
  param([string]$Rg,[string]$App,[int]$Port)
  Invoke-Az @('webapp','config','appsettings','set','-g',$Rg,'-n',$App,'--settings',("WEBSITES_PORT={0}" -f $Port))
}

# ----------------- main -----------------
Ensure-LoggedIn
Ensure-ResourceGroup -Name $ResourceGroup -Loc $Location
$AcrLoginServer = Ensure-Acr -Name $AcrName -Rg $ResourceGroup -Loc $Location
Acr-Build -Acr $AcrName -Image $ImageName -Tag $ImageTag

$ImageFull = "{0}/{1}:{2}" -f $AcrLoginServer, $ImageName, $ImageTag
Write-Host ("[INFO] Image: {0}" -f $ImageFull) -ForegroundColor Yellow

# Ensure plan with quota fallback if requested
$planResult = Ensure-AppServicePlan -Rg $ResourceGroup -Plan $PlanName -Loc $Location -Sku $Sku -AutoFallbackRegion:$AutoFallbackRegion
if ($planResult.created) {
  Write-Host ("[INFO] Plan created in region: {0}" -f $planResult.location) -ForegroundColor Yellow
}
# Create/Configure app
Ensure-WebApp -Rg $ResourceGroup -Plan $PlanName -App $AppName -ImageFull $ImageFull
Set-WebApp-Port -Rg $ResourceGroup -App $AppName -Port $ContainerPort

Write-Host ("[DONE] Visit: https://{0}.azurewebsites.net" -f $AppName) -ForegroundColor Green
