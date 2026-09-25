$ErrorActionPreference = "Continue"

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Starting Azure Container Apps Deployment Pipeline " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# Configuration parameters
$RESOURCE_GROUP = "rg-foundry"
$LOCATION = "eastus"
$REGISTRY_NAME = "payeriqregistry"
$APP_NAME = "payeriq-api"
$ENV_NAME = "rg-foundry-env"
$IMAGE_TAG = "payeriq-api:{0:yyyyMMddHHmmss}" -f (Get-Date)

# ------------------------------------------------------------------
# STEP 0: Load secrets/config from local .env (never hardcode these)
# ------------------------------------------------------------------
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "[ERROR] .env not found at $envFile. Create it with the Azure OpenAI / Azure Search values before deploying." -ForegroundColor Red
    exit 1
}

$envValues = @{}
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim().Trim('"')
    $envValues[$key] = $value
}

function Get-RequiredEnv($name) {
    $value = $envValues[$name]
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "[ERROR] $name is missing from .env. Cannot deploy without it." -ForegroundColor Red
        exit 1
    }
    return $value
}

$OPENAI_ENDPOINT = Get-RequiredEnv "AZURE_OPENAI_ENDPOINT"
$OPENAI_KEY = Get-RequiredEnv "AZURE_OPENAI_API_KEY"
$OPENAI_VERSION = Get-RequiredEnv "OPENAI_API_VERSION"
$OPENAI_DEPLOYMENT = Get-RequiredEnv "AZURE_OPENAI_DEPLOYMENT_NAME"
$OPENAI_FALLBACK_DEPLOYMENT = $envValues["AZURE_OPENAI_FALLBACK_DEPLOYMENT_NAME"]

$SEARCH_ENDPOINT = $envValues["AZURE_SEARCH_ENDPOINT"]
$SEARCH_KEY = $envValues["AZURE_SEARCH_KEY"]
$SEARCH_INDEX = $envValues["AZURE_SEARCH_INDEX_NAME"]
if ([string]::IsNullOrWhiteSpace($SEARCH_ENDPOINT) -or [string]::IsNullOrWhiteSpace($SEARCH_KEY)) {
    Write-Host "[WARN] AZURE_SEARCH_ENDPOINT/AZURE_SEARCH_KEY not set -- the deployed app will run with knowledge-base grounding disabled (search_knowledge_base degrades to empty results)." -ForegroundColor Yellow
}

$COSMOS_ENDPOINT = $envValues["AZURE_COSMOS_ENDPOINT"]
$COSMOS_KEY = $envValues["AZURE_COSMOS_KEY"]
$COSMOS_DATABASE = $envValues["AZURE_COSMOS_DATABASE_NAME"]
$COSMOS_CONTAINER = $envValues["AZURE_COSMOS_CONTAINER_NAME"]
$COSMOS_CHECKPOINT_CONTAINER = $envValues["AZURE_COSMOS_CHECKPOINT_CONTAINER_NAME"]
$DOCS_STORAGE_CONNECTION_STRING = $envValues["AZURE_DOCS_STORAGE_CONNECTION_STRING"]
$DOCUMENT_HISTORY_CONTAINER = $envValues["DOCUMENT_HISTORY_INDEX_NAME"]
if ([string]::IsNullOrWhiteSpace($COSMOS_ENDPOINT) -or [string]::IsNullOrWhiteSpace($COSMOS_KEY)) {
    Write-Host "[WARN] AZURE_COSMOS_ENDPOINT/AZURE_COSMOS_KEY not set -- the deployed app will run with document history logging AND the /v2 LangGraph checkpointer both disabled." -ForegroundColor Yellow
} elseif ([string]::IsNullOrWhiteSpace($DOCS_STORAGE_CONNECTION_STRING)) {
    Write-Host "[WARN] AZURE_DOCS_STORAGE_CONNECTION_STRING not set -- document history logging will be disabled (the /v2 checkpointer is unaffected)." -ForegroundColor Yellow
}

$CONTENT_SAFETY_ENDPOINT = $envValues["CONTENT_SAFETY_ENDPOINT"]
$CONTENT_SAFETY_KEY = $envValues["CONTENT_SAFETY_KEY"]
# Content Safety is provisioned automatically in Step 3 below if not already set here.
# The /v2 LangGraph checkpointer itself reuses the AZURE_COSMOS_* values above --
# no separate Postgres server is needed.

# POST /v2/auth/login (app/auth_router.py, app/services/auth_service.py) --
# without AUTH_JWT_SECRET the deployed app refuses to issue/verify tokens
# (503, "authentication service misconfigured") even though the container
# itself deploys and starts fine.
$AUTH_JWT_SECRET = $envValues["AUTH_JWT_SECRET"]
$AUTH_COSMOS_DB = $envValues["AZURE_COSMOS_AUTH_DATABASE_NAME"]
$AUTH_USER_CONTAINER = $envValues["AZURE_COSMOS_USER_CONTAINER_NAME"]
if ([string]::IsNullOrWhiteSpace($AUTH_JWT_SECRET)) {
    Write-Host "[WARN] AUTH_JWT_SECRET not set in .env -- the deployed /v2/auth/login and every /v2/generate|refine|status call will fail with 503 until it's set." -ForegroundColor Yellow
}

# ------------------------------------------------------------------
# STEP 1: Azure Authentication Check
# ------------------------------------------------------------------
Write-Host "`n[Step 1/5] Checking Azure login status..." -ForegroundColor Yellow
$azAccount = az account show --output json 2>$null
if (-not $azAccount) {
    Write-Host "Not authenticated. Initiating Azure login..." -ForegroundColor Yellow
    az login
} else {
    Write-Host "Authenticated successfully." -ForegroundColor Green
}

# ------------------------------------------------------------------
# STEP 2: Ensure Resource Group, Registry, & Environment Exist
# ------------------------------------------------------------------
Write-Host "`n[Step 2/5] Ensuring Resource Group, Registry, and ACA Environment exist..." -ForegroundColor Yellow

az group create --name $RESOURCE_GROUP --location $LOCATION --output none
Write-Host "Resource Group '$RESOURCE_GROUP' is ready." -ForegroundColor Green

$acrExists = az acr show --name $REGISTRY_NAME --output json 2>$null
if (-not $acrExists) {
    az acr create --resource-group $RESOURCE_GROUP --name $REGISTRY_NAME --sku Basic --admin-enabled true --output none
    Write-Host "Azure Container Registry '$REGISTRY_NAME' created." -ForegroundColor Green
} else {
    az acr update --name $REGISTRY_NAME --admin-enabled true --output none
    Write-Host "Azure Container Registry '$REGISTRY_NAME' configured." -ForegroundColor Green
}

az containerapp env create --name $ENV_NAME --resource-group $RESOURCE_GROUP --location $LOCATION --output none
Write-Host "Container App Environment '$ENV_NAME' is ready." -ForegroundColor Green

# ------------------------------------------------------------------
# STEP 3: Ensure the /v2 LangGraph pipeline's dependency exists --
#         Azure AI Content Safety (Prompt Shields + Groundedness).
#         /v2 returns 503 or errors without it, even though the
#         container itself deploys fine. The checkpointer itself reuses
#         the existing Cosmos DB account, so nothing to provision there.
# ------------------------------------------------------------------
Write-Host "`n[Step 3/5] Ensuring Content Safety resource exists..." -ForegroundColor Yellow

if ((-not [string]::IsNullOrWhiteSpace($CONTENT_SAFETY_ENDPOINT)) -and (-not [string]::IsNullOrWhiteSpace($CONTENT_SAFETY_KEY))) {
    Write-Host "CONTENT_SAFETY_ENDPOINT/CONTENT_SAFETY_KEY already set in .env -- using as-is." -ForegroundColor Green
} else {
    $CS_NAME = "payeriq-contentsafety"
    $csExists = az cognitiveservices account show --name $CS_NAME --resource-group $RESOURCE_GROUP --output json 2>$null
    if (-not $csExists) {
        az cognitiveservices account create `
          --name $CS_NAME `
          --resource-group $RESOURCE_GROUP `
          --location $LOCATION `
          --kind ContentSafety `
          --sku S0 `
          --custom-domain $CS_NAME `
          --yes `
          --output none
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[WARN] Content Safety resource creation failed -- /v2 guardrails/groundedness will error until this is fixed." -ForegroundColor Yellow
        } else {
            Write-Host "Content Safety resource '$CS_NAME' created." -ForegroundColor Green
        }
    } else {
        Write-Host "Content Safety resource '$CS_NAME' already exists." -ForegroundColor Green
    }

    $CONTENT_SAFETY_ENDPOINT = az cognitiveservices account show --name $CS_NAME --resource-group $RESOURCE_GROUP --query "properties.endpoint" -o tsv 2>$null
    $CONTENT_SAFETY_KEY = az cognitiveservices account keys list --name $CS_NAME --resource-group $RESOURCE_GROUP --query "key1" -o tsv 2>$null
}

if ([string]::IsNullOrWhiteSpace($CONTENT_SAFETY_ENDPOINT) -or [string]::IsNullOrWhiteSpace($CONTENT_SAFETY_KEY)) {
    Write-Host "[WARN] CONTENT_SAFETY_ENDPOINT/CONTENT_SAFETY_KEY are still not fully available -- the deployed /v2 LangGraph endpoints will be disabled or error." -ForegroundColor Yellow
}

# ------------------------------------------------------------------
# STEP 4: Remote Cloud Docker Build
# ------------------------------------------------------------------
Write-Host "`n[Step 4/5] Building Docker container image in Azure Cloud..." -ForegroundColor Yellow

# --no-logs: az acr build's live log streaming pipes colorized build output
# through colorama, which crashes with UnicodeEncodeError on this machine's
# non-UTF8 console codepage (a known Azure CLI/Windows issue). --no-logs
# still blocks until the build finishes and sets $LASTEXITCODE correctly --
# it just skips the streaming that crashes.
az acr build --registry $REGISTRY_NAME --image $IMAGE_TAG --no-logs .
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ERROR] Docker build failed. Halting pipeline execution." -ForegroundColor Red
    exit 1
}

Write-Host "Cloud container build completed successfully!" -ForegroundColor Green

# ------------------------------------------------------------------
# STEP 5: Fetch Credentials & Provision Container App
# ------------------------------------------------------------------
Write-Host "`n[Step 5/5] Provisioning/Updating Azure Container App..." -ForegroundColor Yellow

$REGISTRY_SERVER = "$REGISTRY_NAME.azurecr.io"
$ACR_USER = az acr credential show --name $REGISTRY_NAME --query "username" -o tsv
$ACR_PASS = az acr credential show --name $REGISTRY_NAME --query "passwords[0].value" -o tsv

$envVars = @(
    "AZURE_OPENAI_ENDPOINT=$OPENAI_ENDPOINT"
    "AZURE_OPENAI_API_KEY=$OPENAI_KEY"
    "OPENAI_API_VERSION=$OPENAI_VERSION"
    "AZURE_OPENAI_DEPLOYMENT_NAME=$OPENAI_DEPLOYMENT"
)
if (-not [string]::IsNullOrWhiteSpace($OPENAI_FALLBACK_DEPLOYMENT)) {
    $envVars += "AZURE_OPENAI_FALLBACK_DEPLOYMENT_NAME=$OPENAI_FALLBACK_DEPLOYMENT"
}
if (-not [string]::IsNullOrWhiteSpace($SEARCH_ENDPOINT)) {
    $envVars += "AZURE_SEARCH_ENDPOINT=$SEARCH_ENDPOINT"
    $envVars += "AZURE_SEARCH_KEY=$SEARCH_KEY"
    if (-not [string]::IsNullOrWhiteSpace($SEARCH_INDEX)) {
        $envVars += "AZURE_SEARCH_INDEX_NAME=$SEARCH_INDEX"
    }
}
if (-not [string]::IsNullOrWhiteSpace($COSMOS_ENDPOINT) -and -not [string]::IsNullOrWhiteSpace($COSMOS_KEY)) {
    # Shared by document-history logging and the /v2 LangGraph checkpointer --
    # pushed independently of AZURE_DOCS_STORAGE_CONNECTION_STRING below so a
    # missing blob connection string doesn't also disable the checkpointer.
    $envVars += "AZURE_COSMOS_ENDPOINT=$COSMOS_ENDPOINT"
    $envVars += "AZURE_COSMOS_KEY=$COSMOS_KEY"
    if (-not [string]::IsNullOrWhiteSpace($COSMOS_DATABASE)) {
        $envVars += "AZURE_COSMOS_DATABASE_NAME=$COSMOS_DATABASE"
    }
    if (-not [string]::IsNullOrWhiteSpace($COSMOS_CONTAINER)) {
        $envVars += "AZURE_COSMOS_CONTAINER_NAME=$COSMOS_CONTAINER"
    }
    if (-not [string]::IsNullOrWhiteSpace($COSMOS_CHECKPOINT_CONTAINER)) {
        $envVars += "AZURE_COSMOS_CHECKPOINT_CONTAINER_NAME=$COSMOS_CHECKPOINT_CONTAINER"
    }
}
if (-not [string]::IsNullOrWhiteSpace($DOCS_STORAGE_CONNECTION_STRING)) {
    $envVars += "AZURE_DOCS_STORAGE_CONNECTION_STRING=$DOCS_STORAGE_CONNECTION_STRING"
    if (-not [string]::IsNullOrWhiteSpace($DOCUMENT_HISTORY_CONTAINER)) {
        $envVars += "DOCUMENT_HISTORY_INDEX_NAME=$DOCUMENT_HISTORY_CONTAINER"
    }
}
if (-not [string]::IsNullOrWhiteSpace($CONTENT_SAFETY_ENDPOINT) -and -not [string]::IsNullOrWhiteSpace($CONTENT_SAFETY_KEY)) {
    $envVars += "CONTENT_SAFETY_ENDPOINT=$CONTENT_SAFETY_ENDPOINT"
    $envVars += "CONTENT_SAFETY_KEY=$CONTENT_SAFETY_KEY"
}
if (-not [string]::IsNullOrWhiteSpace($AUTH_JWT_SECRET)) {
    $envVars += "AUTH_JWT_SECRET=$AUTH_JWT_SECRET"
}
if (-not [string]::IsNullOrWhiteSpace($AUTH_COSMOS_DB)) {
    $envVars += "AZURE_COSMOS_AUTH_DATABASE_NAME=$AUTH_COSMOS_DB"
}
if (-not [string]::IsNullOrWhiteSpace($AUTH_USER_CONTAINER)) {
    $envVars += "AZURE_COSMOS_USER_CONTAINER_NAME=$AUTH_USER_CONTAINER"
}

$appExists = az containerapp show --name $APP_NAME --resource-group $RESOURCE_GROUP --output json 2>$null
if (-not $appExists) {
    az containerapp create `
      --name $APP_NAME `
      --resource-group $RESOURCE_GROUP `
      --environment $ENV_NAME `
      --image "$REGISTRY_SERVER/$IMAGE_TAG" `
      --registry-server $REGISTRY_SERVER `
      --registry-username $ACR_USER `
      --registry-password $ACR_PASS `
      --target-port 8000 `
      --ingress external `
      --cpu 0.5 --memory 1.0Gi `
      --min-replicas 1 `
      --env-vars $envVars
    Write-Host "Container App '$APP_NAME' created." -ForegroundColor Green
} else {
    # 1. Update the container image and environment variables
    az containerapp update `
      --name $APP_NAME `
      --resource-group $RESOURCE_GROUP `
      --image "$REGISTRY_SERVER/$IMAGE_TAG" `
      --set-env-vars $envVars

    # 2. Update ingress target port separately
    az containerapp ingress update `
      --name $APP_NAME `
      --resource-group $RESOURCE_GROUP `
      --target-port 8000

    Write-Host "Container App '$APP_NAME' updated to new revision." -ForegroundColor Green
}
