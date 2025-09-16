Param(
  [Parameter(Mandatory=$true)][string]$SubscriptionId,
  [Parameter(Mandatory=$true)][string]$ResourceGroup,
  [Parameter(Mandatory=$true)][string]$Location,
  [Parameter(Mandatory=$true)][string]$OpenAIName,
  [Parameter(Mandatory=$true)][string]$SearchName
)

$ErrorActionPreference = "Stop"

Write-Host "[1/7] az account set"
az account set --subscription $SubscriptionId

Write-Host "[2/7] Create resource group: $ResourceGroup ($Location)"
az group create -n $ResourceGroup -l $Location | Out-Null

Write-Host "[3/7] Create Azure OpenAI: $OpenAIName"
az cognitiveservices account create `
  -n $OpenAIName -g $ResourceGroup -l $Location `
  --kind OpenAI --sku S0 --yes --custom-domain $OpenAIName | Out-Null

Write-Host "[4/7] Create Azure AI Search: $SearchName"
az search service create `
  --name $SearchName -g $ResourceGroup -l $Location `
  --sku basic | Out-Null

Write-Host "[5/7] Grab endpoints & keys"
$AoaiEndpoint = az cognitiveservices account show -n $OpenAIName -g $ResourceGroup --query "properties.endpoint" -o tsv
$AoaiKey = az cognitiveservices account keys list -n $OpenAIName -g $ResourceGroup --query "key1" -o tsv
$SearchEndpoint = "https://$SearchName.search.windows.net"
$SearchAdminKey = az search admin-key show --service-name $SearchName -g $ResourceGroup --query "primaryKey" -o tsv
# ensure query key
$QueryKeys = az search query-key list --service-name $SearchName -g $ResourceGroup --query "[0].key" -o tsv 2>$null
if (-not $QueryKeys) {
  az search query-key create --service-name $SearchName -g $ResourceGroup --name "default" | Out-Null
}
$SearchQueryKey = az search query-key list --service-name $SearchName -g $ResourceGroup --query "[0].key" -o tsv

Write-Host "[6/7] (Optional) List models in region"
az cognitiveservices account list-models -n $OpenAIName -g $ResourceGroup --query "[?capabilities && kind=='OpenAI'].{name:name,version:version}" -o table

@"
=== TIP ===
Deployments are required for chat & embeddings.
Portal: Azure OpenAI -> Deployments -> Create.
CLI example after picking exact names/versions:

  az cognitiveservices account deployment create `
    -g $ResourceGroup -n $OpenAIName `
    --deployment-name gpt-4o `
    --model-format OpenAI `
    --model-name gpt-4o `
    --model-version 2024-08-06

  az cognitiveservices account deployment create `
    -g $ResourceGroup -n $OpenAIName `
    --deployment-name text-embedding-3-small `
    --model-format OpenAI `
    --model-name text-embedding-3-small `
    --model-version 1
============
"@ | Write-Host

Write-Host "[7/7] Write .env"
@"
SUBSCRIPTION_ID=$SubscriptionId
RESOURCE_GROUP=$ResourceGroup
LOCATION=$Location
OPENAI_NAME=$OpenAIName
SEARCH_NAME=$SearchName

AZURE_OPENAI_ENDPOINT=$AoaiEndpoint
AZURE_OPENAI_KEY=$AoaiKey
AOAI_DEPLOYMENT_CHAT=gpt-4o
AOAI_DEPLOYMENT_EMBED=text-embedding-3-small
AOAI_API_VERSION=2024-10-21

SEARCH_ENDPOINT=$SearchEndpoint
SEARCH_ADMIN_KEY=$SearchAdminKey
SEARCH_QUERY_KEY=$SearchQueryKey
SEARCH_INDEX=kb-playbook
"@ | Out-File -FilePath .env -Encoding utf8 -Force

Write-Host "Done. .env created."
