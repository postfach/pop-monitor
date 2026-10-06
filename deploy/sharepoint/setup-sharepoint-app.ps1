<#
.SYNOPSIS
  Sets up read-only SharePoint access for PoP Monitor's artwork sync (creatives-sync).

.DESCRIPTION
  Run once as a Global Administrator (or Application Administrator + SharePoint
  Administrator). It:
    1. creates (or reuses) the app registration "PoP Monitor - creatives sync";
    2. gives it the Microsoft Graph application permission Sites.Selected and grants
       admin consent. Sites.Selected on its own gives access to no site at all;
    3. grants the app READ on the LUMO SharePoint site only;
    4. creates a client secret (24 months by default);
    5. checks the app can sign in and list Booked Campaigns/Creative+MIs;
    6. writes AZ_TENANT_ID, AZ_CLIENT_ID, AZ_CLIENT_SECRET and AZ_SECRET_EXPIRES into
       your .env (with -EnvFile), or prints them for Portainer.

  Safe to run again: it reuses the app and the site grant, and adds a new secret
  (use it when the secret is about to expire; the old one keeps working until its
  end date, or delete it in Entra > App registrations > Certificates & secrets).

.EXAMPLE
  # From deploy\desktop (Docker Desktop), writing straight into .env:
  powershell -ExecutionPolicy Bypass -File ..\sharepoint\setup-sharepoint-app.ps1 -EnvFile .env
  docker compose up -d --build

.EXAMPLE
  # Portainer: print the values to paste into the stack's environment variables
  powershell -ExecutionPolicy Bypass -File .\deploy\sharepoint\setup-sharepoint-app.ps1
#>
param(
    [string]$SiteUrl = "https://lumodigital.sharepoint.com/sites/LUMO",
    [string]$LibraryFolder = "00_Sales (NEW FOLDER)/Booked Campaigns/Creative+MIs",
    [string]$AppName = "PoP Monitor - creatives sync",
    [int]$SecretMonths = 24,
    [string]$EnvFile = ""
)
$ErrorActionPreference = "Stop"
$GraphAppId = "00000003-0000-0000-c000-000000000000"

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

Step "Microsoft Graph PowerShell"
foreach ($m in "Microsoft.Graph.Authentication", "Microsoft.Graph.Applications") {
    if (-not (Get-Module -ListAvailable -Name $m)) {
        Write-Host "Installing $m for the current user (one-off)..."
        Install-Module $m -Scope CurrentUser -Force -AllowClobber
    }
    Import-Module $m
}
# Sites.FullControl.All is what lets an admin grant an app access to one site.
Connect-MgGraph -Scopes "Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All", "Sites.FullControl.All" -NoWelcome
$tenant = (Get-MgContext).TenantId
Write-Host "Signed in to tenant $tenant as $((Get-MgContext).Account)"

Step "App registration"
$graphSp = Get-MgServicePrincipal -Filter "appId eq '$GraphAppId'"
$role = $graphSp.AppRoles | Where-Object { $_.Value -eq "Sites.Selected" -and $_.AllowedMemberTypes -contains "Application" }
if (-not $role) { throw "Sites.Selected was not found on Microsoft Graph in this tenant." }
$access = @{ ResourceAppId = $GraphAppId; ResourceAccess = @(@{ Id = $role.Id; Type = "Role" }) }
$app = Get-MgApplication -Filter "displayName eq '$($AppName -replace "'", "''")'" | Select-Object -First 1
if ($app) {
    Write-Host "Reusing $AppName (client ID $($app.AppId))"
    Update-MgApplication -ApplicationId $app.Id -RequiredResourceAccess @($access)
} else {
    $app = New-MgApplication -DisplayName $AppName -SignInAudience "AzureADMyOrg" -RequiredResourceAccess @($access) `
        -Notes "PoP Monitor creatives-sync: reads booked artwork and Material Instructions from the LUMO SharePoint site (read only, that site only). Owner: IT."
    Write-Host "Created $AppName (client ID $($app.AppId))"
}
$sp = Get-MgServicePrincipal -Filter "appId eq '$($app.AppId)'"
if (-not $sp) {
    for ($i = 0; $i -lt 10 -and -not $sp; $i++) {
        try { $sp = New-MgServicePrincipal -AppId $app.AppId } catch { Start-Sleep 3 }
    }
}

Step "Admin consent for Sites.Selected"
$granted = Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id | Where-Object { $_.AppRoleId -eq $role.Id }
if ($granted) { Write-Host "Already consented" }
else {
    New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id -PrincipalId $sp.Id -ResourceId $graphSp.Id -AppRoleId $role.Id | Out-Null
    Write-Host "Consent granted"
}

Step "Read access to $SiteUrl (this site only)"
$u = [Uri]$SiteUrl
$site = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/sites/$($u.Host):$($u.AbsolutePath.TrimEnd('/'))"
$perms = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/sites/$($site.id)/permissions"
$mine = $perms.value | Where-Object { $_.grantedToIdentitiesV2.application.id -eq $app.AppId -or $_.grantedToIdentities.application.id -eq $app.AppId }
if ($mine) { Write-Host "Already granted ($($mine.roles -join ', '))" }
else {
    $body = @{ roles = @("read"); grantedToIdentities = @(@{ application = @{ id = $app.AppId; displayName = $AppName } }) } | ConvertTo-Json -Depth 5
    Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/v1.0/sites/$($site.id)/permissions" -Body $body -ContentType "application/json" | Out-Null
    Write-Host "Granted read on $($site.webUrl)"
}

Step "Client secret ($SecretMonths months)"
$end = (Get-Date).AddMonths($SecretMonths)
$secret = Add-MgApplicationPassword -ApplicationId $app.Id -PasswordCredential @{
    DisplayName = "creatives-sync $(Get-Date -Format yyyy-MM-dd)"; EndDateTime = $end }
Write-Host "Secret created, expires $($end.ToString('yyyy-MM-dd'))"

Step "Checking the app can read the artwork folder"
$ok = $false
for ($i = 1; $i -le 12 -and -not $ok; $i++) {          # a new secret / grant can take a minute to apply
    try {
        $tok = Invoke-RestMethod -Method POST -Uri "https://login.microsoftonline.com/$tenant/oauth2/v2.0/token" -Body @{
            client_id = $app.AppId; client_secret = $secret.SecretText; grant_type = "client_credentials"
            scope = "https://graph.microsoft.com/.default" }
        $h = @{ Authorization = "Bearer $($tok.access_token)" }
        $drives = Invoke-RestMethod -Headers $h -Uri "https://graph.microsoft.com/v1.0/sites/$($site.id)/drives?`$select=id,name,webUrl"
        $lib = $drives.value | Where-Object { ([Uri]::UnescapeDataString($_.webUrl)).TrimEnd('/').EndsWith("/Sales") -or $_.name -eq "Sales" } | Select-Object -First 1
        if (-not $lib) { throw "no 'Sales' library found; libraries: $(($drives.value.name) -join ', ')" }
        $path = [Uri]::EscapeDataString($LibraryFolder).Replace("%2F", "/")
        $kids = Invoke-RestMethod -Headers $h -Uri "https://graph.microsoft.com/v1.0/drives/$($lib.id)/root:/$($path):/children?`$select=name"
        Write-Host "OK: the app can read $LibraryFolder ($(($kids.value.name) -join ', '))" -ForegroundColor Green
        $ok = $true
    } catch {
        Write-Host "  not yet ($($_.Exception.Message)); retrying in 10 s ($i/12)"
        Start-Sleep 10
    }
}
if (-not $ok) { Write-Warning "The check didn't pass yet. Give it a few minutes, then use 'Sync now' on the Bookings page. See docs/sharepoint-setup.md > Troubleshooting." }

$values = [ordered]@{
    AZ_TENANT_ID      = $tenant
    AZ_CLIENT_ID      = $app.AppId
    AZ_CLIENT_SECRET  = $secret.SecretText
    AZ_SECRET_EXPIRES = $end.ToString("yyyy-MM-dd")
}
Step "Settings"
if ($EnvFile) {
    $lines = if (Test-Path $EnvFile) { @(Get-Content $EnvFile) } else { @() }
    foreach ($k in $values.Keys) {
        $line = "$k=$($values[$k])"
        $idx = [Array]::FindIndex([string[]]$lines, [Predicate[string]]{ param($l) $l -match "^\s*$k\s*=" })
        if ($idx -ge 0) { $lines[$idx] = $line } else { $lines += $line }
    }
    Set-Content -Path $EnvFile -Value $lines -Encoding ascii
    Write-Host "Written to $EnvFile. Now run:  docker compose up -d --build" -ForegroundColor Green
} else {
    Write-Host "Add these to the stack's environment variables (Portainer) or .env, then redeploy:" -ForegroundColor Green
    $values.GetEnumerator() | ForEach-Object { Write-Host "$($_.Key)=$($_.Value)" }
}
Write-Host "`nKeep the secret private: it reads the LUMO site. It is shown only once."
Disconnect-MgGraph | Out-Null
