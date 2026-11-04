[CmdletBinding()]
param(
    [ValidateSet('Inspect','PrepareIdentities','ApplyVault','VerifyRunner','VerifySteward','VerifyDeveloper','VerifyAudit','Rollback')]
    [string]$Mode = 'Inspect',
    [string]$ExpectedComputerName = $env:COMPUTERNAME,
    [string]$ApprovedVmName = '',
    [string]$ApprovedVmUuid = '',
    [string]$WorkspaceRoot = 'D:\ShopRelay',
    [string]$VaultPath = 'E:\ShopRelay-EvalVault',
    [string[]]$ProtectedRoots = @('D:\ShopRelay'),
    [int]$CodexPid = 0,
    [int]$ApiPid = 0,
    [datetime]$AuditSinceUtc = [datetime]::MinValue,
    [switch]$ConfirmRollback
)

# Default Inspect is read-only. No self-elevation. Apply and Rollback are
# separate explicit modes. Scope is only this machine's new Vault and the two
# dedicated accounts; never point this script at a repository or eval dataset.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Machine = $env:COMPUTERNAME
$script:Workspace = [IO.Path]::GetFullPath($WorkspaceRoot)
$script:Vault = [IO.Path]::GetFullPath($VaultPath)
$script:StateRoot = Join-Path $env:ProgramData 'ShopRelayEvalIsolation'
$script:StatePath = Join-Path $script:StateRoot 'state.json'
$script:RunnerName = 'ShopRelayEvalRunner'
$script:StewardName = 'ShopRelayEvalSteward'
$script:DeveloperName = "$env:USERDOMAIN\$env:USERNAME"
$script:SystemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$script:AdminsSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$script:FileSystemAuditGuid = '{0CCE921D-69AE-11D9-BED3-505054503030}'
$script:VmMutationApprovalConfigured = $false
$script:MarkerName = '.shoprelay-eval-vault.json'
$script:AllowName = 'synthetic-allowed.txt'
$script:DenyName = 'synthetic-denied.txt'
$script:AllowSentinel = 'SYNTHETIC_ALLOW_SENTINEL_NO_SENSITIVE_CONTENT'
$script:DenySentinel = 'SYNTHETIC_DENY_SENTINEL_NO_SENSITIVE_CONTENT'

function Get-CurrentSid { [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
function Test-Elevated {
    $p = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Assert-Admin { if (-not (Test-Elevated)) { throw 'This mode requires an elevated administrator PowerShell; no self-elevation is attempted.' } }
function Assert-Machine {
    if ([string]::IsNullOrWhiteSpace($ExpectedComputerName) -or $env:COMPUTERNAME -cne $ExpectedComputerName -or $script:Machine -cne $ExpectedComputerName) { throw 'Unexpected or unapproved computer name; no changes made.' }
    if (-not (Test-Path -LiteralPath $script:Workspace -PathType Container)) { throw 'Configured code-only workspace is absent; no changes made.' }
    if ([string]::Equals($script:Workspace,$script:Vault,[StringComparison]::OrdinalIgnoreCase) -or $script:Vault.StartsWith($script:Workspace.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Vault must not be inside the code workspace.' }
    if ($script:Workspace.StartsWith($script:Vault.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Workspace must not be inside the Vault.' }
    if ([IO.Path]::GetPathRoot($script:Workspace) -eq [IO.Path]::GetPathRoot($script:Vault)) { throw 'Vault must be on a separate VM volume from the code-only workspace.' }
    $vaultDrive=Get-PSDrive -Name ([IO.Path]::GetPathRoot($script:Vault).TrimEnd(':','\')) -ErrorAction SilentlyContinue
    if (-not $vaultDrive -or $vaultDrive.Provider.Name -ne 'FileSystem') { throw 'Configured Vault volume is unavailable or is not a filesystem volume.' }
}
function Assert-ApprovedVm {
    # Q3 preparation only: identity values supplied as command-line parameters
    # are not an approval. Keep every system-mutating mode disabled until a
    # separately reviewed VM deployment record and trustable approval
    # mechanism are implemented and approved in a later change.
    if (-not $script:VmMutationApprovalConfigured) { throw 'No reviewed VM deployment approval is configured; all account, Vault, ACL, audit and rollback mutations remain disabled.' }
    if ([string]::IsNullOrWhiteSpace($ApprovedVmName) -or [string]::IsNullOrWhiteSpace($ApprovedVmUuid)) { throw 'Approved VM name and UUID are not configured; mutation modes remain disabled.' }
    if ($env:COMPUTERNAME -cne $ApprovedVmName -or $ExpectedComputerName -cne $ApprovedVmName) { throw 'Current host does not match the separately approved VM name.' }
    try {
        $system=Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
        $product=Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop
    } catch { throw 'Cannot verify VM identity through Windows system metadata; mutation modes remain disabled.' }
    if ([string]$product.UUID -cne $ApprovedVmUuid) { throw 'VM UUID does not match the separately approved deployment record.' }
    $modelText=([string]$system.Manufacturer)+' '+([string]$system.Model)
    if ($modelText -notmatch '(?i)(virtual machine|virtualbox|vmware virtual platform|qemu|kvm|xen)') { throw 'Windows system metadata does not identify an approved virtual machine model.' }
}
function Get-UserInfo([string]$Name) {
    $u = Get-LocalUser -Name $Name -ErrorAction SilentlyContinue
    if (-not $u) { return $null }
    [pscustomobject]@{ Name=$u.Name; Sid=$u.SID.Value; Enabled=$u.Enabled }
}
function Test-AdminMember([string]$Sid) {
    $g = Get-LocalGroup -SID $script:AdminsSid
    foreach ($m in Get-LocalGroupMember -Group $g.Name -ErrorAction Stop) { if ($m.SID.Value -eq $Sid) { return $true } }
    return $false
}
function Write-Record([string]$ModeName,[string]$Status,[string]$Detail='') {
    $r = [ordered]@{ at_utc=[datetime]::UtcNow.ToString('o'); machine=$env:COMPUTERNAME; mode=$ModeName; status=$Status; detail=$Detail; subject_sid=(Get-CurrentSid) }
    $line = $r | ConvertTo-Json -Compress
    Write-Output $line
    if ((Test-Path -LiteralPath $script:StateRoot -PathType Container) -and (Test-Elevated)) {
        Add-Content -LiteralPath (Join-Path $script:StateRoot 'events.jsonl') -Value $line -Encoding UTF8
    }
}
function Get-OwnerSid([int]$PidValue) {
    if ($PidValue -le 0) { return 'NOT_PROVIDED' }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$PidValue" -ErrorAction SilentlyContinue
    if (-not $p) { return 'NOT_VERIFIED' }
    $owner = Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid -ErrorAction SilentlyContinue
    if (-not $owner -or $owner.ReturnValue -ne 0) { return 'NOT_VERIFIED' }
    [string]$owner.Sid
}
function Read-State {
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) { throw 'State file is missing; refusing to infer ownership.' }
    Get-Content -LiteralPath $script:StatePath -Raw | ConvertFrom-Json
}
function Read-Marker {
    $path = Join-Path $script:Vault $script:MarkerName
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Vault installation marker is missing.' }
    Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}
function Assert-ManagedVault {
    $state = Read-State
    if ($state.machine -cne $script:Machine -or $state.vault -cne $script:Vault -or $state.stage -cne 'VAULT_CONFIGURED') { throw 'State is not a completed installation for this exact target.' }
    if (-not (Test-Path -LiteralPath $script:Vault -PathType Container)) { throw 'Vault is absent.' }
    $marker = Read-Marker
    if ($marker.install_id -cne $state.install_id -or $marker.machine -cne $script:Machine) { throw 'Vault marker mismatch.' }
    [pscustomobject]@{ State=$state; Marker=$marker }
}
function New-AdminStateRoot {
    if (-not (Test-Path -LiteralPath $script:StateRoot)) { New-Item -ItemType Directory -Path $script:StateRoot -ErrorAction Stop | Out-Null }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true,$false); $acl.SetOwner($script:AdminsSid)
    foreach ($sid in @($script:SystemSid,$script:AdminsSid)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,[Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $script:StateRoot -AclObject $acl
}
function Export-UserRights([string]$Path) {
    & secedit.exe /export /cfg $Path /areas USER_RIGHTS /quiet | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'secedit could not export User Rights Assignment.' }
}
function Test-UserRightContainsSid([string]$CfgPath,[string]$Right,[string]$Sid) {
    $line = Select-String -LiteralPath $CfgPath -Pattern ("^\s*" + [regex]::Escape($Right) + "\s*=") | Select-Object -First 1
    if (-not $line) { return $false }
    $values = ($line.Line -split '=',2)[1] -split ',' | ForEach-Object { $_.Trim().Trim('"') }
    return ($values -contains ("*" + $Sid) -or $values -contains $Sid)
}
function Assert-RunnerLogonPolicy([string]$RunnerSid) {
    $tmp = Join-Path $script:StateRoot ('rights-check-' + [guid]::NewGuid().ToString('N') + '.inf')
    try {
        Export-UserRights $tmp
        $must = @('SeDenyInteractiveLogonRight','SeDenyRemoteInteractiveLogonRight','SeDenyNetworkLogonRight','SeBatchLogonRight')
        foreach ($right in $must) { if (-not (Test-UserRightContainsSid $tmp $right $RunnerSid)) { throw "Runner SID is missing required user right: $right. Configure it in Local Security Policy, then retry." } }
        if (Test-UserRightContainsSid $tmp 'SeDenyBatchLogonRight' $RunnerSid) { throw 'Runner SID is denied batch logon; remove that conflict before continuing.' }
    } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
}
function Get-RunnerBoundaryPreflight {
    # A standard local Runner token necessarily receives Everyone, Authenticated
    # Users and BUILTIN\Users memberships. Check only directory ACL metadata;
    # never enumerate files or read protected content. Requiring a protected
    # root DACL is conservative: inherited parent grants are not treated as safe.
    $broadSids=@('S-1-1-0','S-1-5-11','S-1-5-32-545')
    $results=[System.Collections.Generic.List[object]]::new()
    foreach($root in $ProtectedRoots){
        if(-not (Test-Path -LiteralPath $root -PathType Container)){
            $results.Add([pscustomobject]@{path=$root;status='BLOCKED_ROOT_MISSING_OR_UNVERIFIABLE';reason='Protected root must be an existing directory whose ACL can be reviewed.'})
            continue
        }
        try {
            $acl=Get-Acl -LiteralPath $root -ErrorAction Stop
            $broad=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object {
                $broadSids -contains $_.IdentityReference.Value -and $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow
            })
            if(-not $acl.AreAccessRulesProtected){
                $results.Add([pscustomobject]@{path=$root;status='BLOCKED_INHERITED_DACL';reason='Runner may receive inherited directory access; protected-root ACL boundary is not established.'})
            } elseif($broad.Count -gt 0){
                $principals=@($broad | ForEach-Object {$_.IdentityReference.Value} | Sort-Object -Unique)
                $results.Add([pscustomobject]@{path=$root;status='BLOCKED_BROAD_ALLOW';reason=($principals -join ',')})
            } else {
                $results.Add([pscustomobject]@{path=$root;status='ROOT_ACL_PRECHECK_CLEAR';reason='No broad allow ACE on protected root; descendant and actual Runner-token checks remain required.'})
            }
        } catch {
            $results.Add([pscustomobject]@{path=$root;status='BLOCKED_ACL_UNREADABLE';reason='Could not inspect directory ACL metadata.'})
        }
    }
    @($results.ToArray())
}
function Assert-RunnerBoundaryPreflight {
    $results=@(Get-RunnerBoundaryPreflight)
    $blocked=@($results | Where-Object {$_.status -like 'BLOCKED_*'})
    if($blocked.Count -gt 0){
        $summary=($blocked | ForEach-Object {"$($_.path):$($_.status)"}) -join '; '
        throw "Runner boundary preflight failed before account/Vault changes: $summary. No Candidate/Frozen files were inspected; resolve the approved execution boundary first."
    }
    # A clean root check is not an effective-access proof for descendants or a
    # real Runner token. This script intentionally does not enumerate protected
    # trees; require a separately reviewed environment-specific verification.
    throw 'Directory-root ACL precheck is clear, but effective access across descendants and the real Runner token is not proven. Stop before account/Vault changes; obtain a reviewed isolation boundary and actual identity test plan.'
}
function Add-DirAllow($Acl,[Security.Principal.SecurityIdentifier]$Sid,[Security.AccessControl.FileSystemRights]$Rights) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($Sid,$Rights,
        ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
    $Acl.AddAccessRule($rule)
}
function Set-ExplicitFileAcl([string]$Path,[string]$RunnerSid,[string]$StewardSid,[bool]$DenyRunnerRead) {
    $acl=[Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner($script:AdminsSid)
    foreach($entry in @(
        @($script:SystemSid,[Security.AccessControl.FileSystemRights]::FullControl),
        @($script:AdminsSid,[Security.AccessControl.FileSystemRights]::FullControl),
        @([Security.Principal.SecurityIdentifier]::new($StewardSid),[Security.AccessControl.FileSystemRights]::Modify),
        @([Security.Principal.SecurityIdentifier]::new($RunnerSid),[Security.AccessControl.FileSystemRights]::ReadAndExecute)
    )) {
        $rule=[Security.AccessControl.FileSystemAccessRule]::new($entry[0],$entry[1],[Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    if($DenyRunnerRead){
        $deny=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($RunnerSid),[Security.AccessControl.FileSystemRights]::ReadData,[Security.AccessControl.AccessControlType]::Deny)
        $acl.AddAccessRule($deny)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Add-AuditRule([string]$Path,[Security.Principal.SecurityIdentifier]$Sid,[bool]$IsDirectory) {
    $acl = Get-Acl -LiteralPath $Path -Audit
    $rights = [Security.AccessControl.FileSystemRights]::ReadData -bor [Security.AccessControl.FileSystemRights]::ReadAttributes -bor
        [Security.AccessControl.FileSystemRights]::ReadExtendedAttributes -bor [Security.AccessControl.FileSystemRights]::ReadPermissions
    $inherit = if ($IsDirectory) { [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit } else { [Security.AccessControl.InheritanceFlags]::None }
    $rule = [Security.AccessControl.FileSystemAuditRule]::new($Sid,$rights,$inherit,[Security.AccessControl.PropagationFlags]::None,
        ([Security.AccessControl.AuditFlags]::Success -bor [Security.AccessControl.AuditFlags]::Failure))
    $acl.AddAuditRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Assert-NoBroadVaultAce([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) { throw 'Vault DACL is not protected from inherited ACEs.' }
    $broad = @('S-1-1-0','S-1-5-11','S-1-5-32-545')
    foreach ($ace in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if ($broad -contains $ace.IdentityReference.Value) { throw "Broad principal remains on Vault: $($ace.IdentityReference.Value)" }
    }
}
function Assert-VaultAcl([string]$RunnerSid,[string]$StewardSid) {
    Assert-NoBroadVaultAce $script:Vault
    $rootAcl=Get-Acl -LiteralPath $script:Vault
    if($rootAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $script:AdminsSid.Value){throw 'Vault owner is not BUILTIN\\Administrators.'}
    $expected=@($script:SystemSid.Value,$script:AdminsSid.Value,$RunnerSid,$StewardSid)
    foreach($path in @($script:Vault,(Join-Path $script:Vault $script:AllowName),(Join-Path $script:Vault $script:DenyName),(Join-Path $script:Vault $script:MarkerName))){
        if(-not (Test-Path -LiteralPath $path -PathType Leaf) -and $path -ne $script:Vault){throw "Expected managed file missing: $([IO.Path]::GetFileName($path))"}
        $acl=Get-Acl -LiteralPath $path
        if(-not $acl.AreAccessRulesProtected){throw "DACL inheritance is enabled: $path"}
        foreach($ace in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
            if($expected -notcontains $ace.IdentityReference.Value){throw "Unexpected principal in Vault ACL: $($ace.IdentityReference.Value)"}
        }
    }
    $denyAcl=Get-Acl -LiteralPath (Join-Path $script:Vault $script:DenyName)
    $hasRunnerDeny=@($denyAcl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -eq $RunnerSid -and $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny })
    if(-not $hasRunnerDeny){throw 'Deny sentinel is missing the explicit Runner deny ACE.'}
}
function Assert-ObjectAuditRules([string]$Path,[string[]]$Sids) {
    $acl=Get-Acl -LiteralPath $Path -Audit
    $rules=$acl.GetAuditRules($true,$true,[Security.Principal.SecurityIdentifier])
    foreach($sid in $Sids){
        $found=@($rules|Where-Object{$_.IdentityReference.Value -eq $sid -and ($_.AuditFlags -band [Security.AccessControl.AuditFlags]::Success) -and ($_.AuditFlags -band [Security.AccessControl.AuditFlags]::Failure)})
        if(-not $found){throw "Missing success/failure audit SACL for SID $sid on $Path"}
    }
}
function Test-ReadSentinel([string]$Path,[string]$Expected) {
    try { return ([IO.File]::ReadAllText($Path) -ceq $Expected) }
    catch [UnauthorizedAccessException] { return $false }
    catch [Security.SecurityException] { return $false }
}

function Invoke-Inspect {
    $runner=Get-UserInfo $script:RunnerName; $steward=Get-UserInfo $script:StewardName
    $vault=if (Test-Path -LiteralPath $script:Vault) { $i=Get-Item -LiteralPath $script:Vault -Force; $a=Get-Acl -LiteralPath $script:Vault; [pscustomobject]@{path=$i.FullName;attributes=[string]$i.Attributes;owner=$a.Owner;sddl=$a.Sddl} } else { 'ABSENT' }
    [pscustomobject]@{
        mode='INSPECT_READ_ONLY'; computer=$env:COMPUTERNAME; expected_computer=$script:Machine
        current_identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name; current_sid=Get-CurrentSid; elevated=Test-Elevated
        runner=$runner; runner_admin_member=if($runner){Test-AdminMember $runner.Sid}else{'NOT_PRESENT'}
        steward=$steward; steward_admin_member=if($steward){Test-AdminMember $steward.Sid}else{'NOT_PRESENT'}
        vault=$vault; workspace_root=$script:Workspace; workspace_root_sddl=(Get-Acl -LiteralPath $script:Workspace).Sddl
        vm_preflight=[pscustomobject]@{approved_name_configured=(-not [string]::IsNullOrWhiteSpace($ApprovedVmName));approved_uuid_configured=(-not [string]::IsNullOrWhiteSpace($ApprovedVmUuid));current_model_status='NOT_VERIFIED_BY_READ_ONLY_MODE'}
        runner_boundary_preflight=@(Get-RunnerBoundaryPreflight)
        codex_owner_sid=Get-OwnerSid $CodexPid; api_owner_sid=Get-OwnerSid $ApiPid
        note='No system changes; process IDs are optional and must be independently identified.'
    } | ConvertTo-Json -Depth 5
}

function Invoke-PrepareIdentities {
    Assert-Admin; Assert-Machine
    Assert-ApprovedVm
    Assert-RunnerBoundaryPreflight
    if (Test-Path -LiteralPath $script:StatePath) {
        $existing=Read-State
        if($existing.machine -cne $script:Machine -or $existing.vault -cne $script:Vault){throw 'Existing state target mismatch; refusing reuse.'}
        $r=Get-UserInfo $script:RunnerName;$s=Get-UserInfo $script:StewardName
        if($r -and $s -and $r.Sid -ceq $existing.runner_sid -and $s.Sid -ceq $existing.steward_sid){Write-Record 'PrepareIdentities' 'ALREADY_PREPARED_NO_MUTATION' "stage=$($existing.stage)";return}
        throw 'Existing state and local account SIDs differ; manual review required.'
    }
    if (Test-Path -LiteralPath $script:Vault) { throw 'Vault exists; inspect without changing it. Refusing to replace ACL/content.' }
    if (Test-Path -LiteralPath $script:StateRoot) { throw 'State root exists without a recognized installation; inspect manually.' }
    foreach ($name in @($script:RunnerName,$script:StewardName)) { if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) { throw "Account $name already exists; inspect manually, never reset it." } }
    New-AdminStateRoot
    $created = [System.Collections.Generic.List[string]]::new()
    try {
        foreach ($name in @($script:RunnerName,$script:StewardName)) {
            $secure = Read-Host "Enter a new password for $name (secure prompt; not logged)" -AsSecureString
            try { New-LocalUser -Name $name -Password $secure -AccountNeverExpires -UserMayNotChangePassword | Out-Null; $created.Add($name) }
            finally { Remove-Variable secure -ErrorAction SilentlyContinue }
        }
        $runner=Get-UserInfo $script:RunnerName; $steward=Get-UserInfo $script:StewardName; $developer=Get-CurrentSid
        if ($runner.Sid -eq $steward.Sid -or $runner.Sid -eq $developer -or $steward.Sid -eq $developer) { throw 'Runner, Steward, and developer must be three distinct SIDs.' }
        if ((Test-AdminMember $runner.Sid) -or (Test-AdminMember $steward.Sid)) { throw 'Runner and Steward must remain standard users, not local Administrators members.' }
        $userRightsBefore=Join-Path $script:StateRoot 'user-rights-before.inf'
        Export-UserRights $userRightsBefore
        $state=[ordered]@{schema='m6.1-c-os-isolation-v3';stage='IDENTITIES_CREATED';machine=$script:Machine;approved_vm_name=$ApprovedVmName;approved_vm_uuid=$ApprovedVmUuid;workspace=$script:Workspace;vault=$script:Vault;developer_sid=$developer;runner_sid=$runner.Sid;steward_sid=$steward.Sid;created_accounts=@($created.ToArray());user_rights_before=$userRightsBefore;created_at_utc=[datetime]::UtcNow.ToString('o')}
        $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
        Write-Record 'PrepareIdentities' 'IDENTITIES_CREATED_PENDING_USER_RIGHTS' 'No password material recorded. Configure and verify Runner batch/deny-interactive rights before ApplyVault.'
        Write-Output 'Next: Local Security Policy → User Rights Assignment: add Runner to Deny log on locally, Deny log on through Remote Desktop Services, and Log on as a batch job. Then run ApplyVault.'
    } catch {
        foreach($name in $created){ try{Disable-LocalUser -Name $name -ErrorAction Stop}catch{} }
        Write-Record 'PrepareIdentities' 'FAILED_CREATED_ACCOUNTS_DISABLED' $_.Exception.Message
        throw
    }
}

function Invoke-ApplyVault {
    Assert-Admin; Assert-Machine
    Assert-ApprovedVm
    Assert-RunnerBoundaryPreflight
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) { throw 'Run PrepareIdentities first; no identities will be created in ApplyVault mode.' }
    $state=Read-State
    if ($state.machine -cne $script:Machine -or $state.vault -cne $script:Vault) { throw 'Prepared state target mismatch.' }
        if ($state.stage -eq 'VAULT_CONFIGURED') {
        $managed=Assert-ManagedVault
        Assert-VaultAcl $managed.Marker.runner_sid $managed.Marker.steward_sid
        Write-Record 'ApplyVault' 'ALREADY_CONFIGURED_NO_MUTATION' "install_id=$($managed.Marker.install_id)"
        return
    }
    if ($state.stage -cne 'IDENTITIES_CREATED') { throw 'Unexpected state stage; refusing to continue.' }
    if (Test-Path -LiteralPath $script:Vault) { throw 'Vault path exists; inspect manually, no ACL/content changes made.' }
    $runner=Get-UserInfo $script:RunnerName; $steward=Get-UserInfo $script:StewardName
    if (-not $runner -or -not $steward -or $runner.Sid -cne $state.runner_sid -or $steward.Sid -cne $state.steward_sid) { throw 'Dedicated account SID does not match prepared state.' }
    if (-not $runner.Enabled -or -not $steward.Enabled -or (Test-AdminMember $runner.Sid) -or (Test-AdminMember $steward.Sid)) { throw 'Runner/Steward must be enabled standard accounts.' }
    Assert-RunnerLogonPolicy $runner.Sid

    $auditBackup=Join-Path $script:StateRoot 'audit-policy-before.csv'
    & auditpol.exe /backup /file:$auditBackup | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not save pre-change audit policy.' }
    $installId=[guid]::NewGuid().ToString('N')
    $vaultCreated=$false
    try {
        New-Item -ItemType Directory -Path $script:Vault -ErrorAction Stop | Out-Null; $vaultCreated=$true
        [IO.File]::WriteAllText((Join-Path $script:Vault $script:AllowName),$script:AllowSentinel,[Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path $script:Vault $script:DenyName),$script:DenySentinel,[Text.UTF8Encoding]::new($false))
        $dacl=[Security.AccessControl.DirectorySecurity]::new(); $dacl.SetAccessRuleProtection($true,$false); $dacl.SetOwner($script:AdminsSid)
        Add-DirAllow $dacl $script:SystemSid ([Security.AccessControl.FileSystemRights]::FullControl)
        Add-DirAllow $dacl $script:AdminsSid ([Security.AccessControl.FileSystemRights]::FullControl)
        Add-DirAllow $dacl ([Security.Principal.SecurityIdentifier]::new($steward.Sid)) ([Security.AccessControl.FileSystemRights]::Modify)
        Add-DirAllow $dacl ([Security.Principal.SecurityIdentifier]::new($runner.Sid)) ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
        Set-Acl -LiteralPath $script:Vault -AclObject $dacl
        Set-ExplicitFileAcl (Join-Path $script:Vault $script:AllowName) $runner.Sid $steward.Sid $false
        Set-ExplicitFileAcl (Join-Path $script:Vault $script:DenyName) $runner.Sid $steward.Sid $true
        & auditpol.exe /set /subcategory:$script:FileSystemAuditGuid /success:enable /failure:enable | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'File System audit policy could not be enabled.' }
        foreach($sid in @($runner.Sid,$steward.Sid,$state.developer_sid)){
            $sidObj=[Security.Principal.SecurityIdentifier]::new($sid)
            Add-AuditRule $script:Vault $sidObj $true
            Add-AuditRule (Join-Path $script:Vault $script:AllowName) $sidObj $false
            Add-AuditRule (Join-Path $script:Vault $script:DenyName) $sidObj $false
        }
        $marker=[ordered]@{schema='m6.1-c-os-vault-marker-v1';machine=$script:Machine;install_id=$installId;developer_sid=$state.developer_sid;runner_sid=$runner.Sid;steward_sid=$steward.Sid;sentinels=@($script:AllowName,$script:DenyName)}
        $marker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $script:Vault $script:MarkerName) -Encoding UTF8
        Set-ExplicitFileAcl (Join-Path $script:Vault $script:MarkerName) $runner.Sid $steward.Sid $false
        foreach($sid in @($runner.Sid,$steward.Sid,$state.developer_sid)){
            Add-AuditRule (Join-Path $script:Vault $script:MarkerName) ([Security.Principal.SecurityIdentifier]::new($sid)) $false
        }
        $state.stage='VAULT_CONFIGURED'; $state.install_id=$installId; $state.audit_policy_backup=$auditBackup; $state.configured_at_utc=[datetime]::UtcNow.ToString('o')
        $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
        Assert-VaultAcl $runner.Sid $steward.Sid
        Write-Record 'ApplyVault' 'CONFIGURED_AWAITING_REAL_IDENTITY_TESTS' "install_id=$installId; no secret values logged"
        Write-Output 'No ACL test is implied. Run VerifyDeveloper non-elevated, VerifyRunner as Runner, VerifySteward as Steward, and VerifyAudit as Administrator.'
    } catch {
        Write-Record 'ApplyVault' 'FAILED_FAIL_CLOSED_REVIEW_REQUIRED' $_.Exception.Message
        foreach($name in @($script:RunnerName,$script:StewardName)){
            try { Disable-LocalUser -Name $name -ErrorAction Stop } catch { }
        }
        if ($vaultCreated -and (Test-Path -LiteralPath $script:Vault)) {
            try {
                $closed=[Security.AccessControl.DirectorySecurity]::new();$closed.SetAccessRuleProtection($true,$false);$closed.SetOwner($script:AdminsSid)
                Add-DirAllow $closed $script:SystemSid ([Security.AccessControl.FileSystemRights]::FullControl);Add-DirAllow $closed $script:AdminsSid ([Security.AccessControl.FileSystemRights]::FullControl)
                Set-Acl -LiteralPath $script:Vault -AclObject $closed
            } catch { Write-Record 'ApplyVault' 'FAIL_CLOSED_ACL_NOT_VERIFIED' $_.Exception.Message }
        }
        throw
    }
}

function Invoke-IdentityProbe {
    $sid=Get-CurrentSid; $elevated=Test-Elevated
    $runner=Get-UserInfo $script:RunnerName; $steward=Get-UserInfo $script:StewardName
    $allow=Join-Path $script:Vault $script:AllowName; $deny=Join-Path $script:Vault $script:DenyName
    if($Mode -in @('VerifyRunner','VerifySteward')){
        $marker=Read-Marker
        if($marker.machine -cne $script:Machine -or $marker.runner_sid -cne $runner.Sid -or $marker.steward_sid -cne $steward.Sid){throw 'Vault marker identity binding mismatch.'}
    }
    $readAllow=$false; $readDeny=$false; $writeProbe='NOT_RUN'; $passed=$false
    switch($Mode){
        'VerifyRunner' {
            if(-not $runner -or $sid -cne $runner.Sid -or $elevated -or (Test-AdminMember $sid)){throw 'Run as the non-elevated standard Runner identity.'}
            $readAllow=Test-ReadSentinel $allow $script:AllowSentinel; $readDeny=Test-ReadSentinel $deny $script:DenySentinel
            $passed=$readAllow -and -not $readDeny
        }
        'VerifySteward' {
            if(-not $steward -or $sid -cne $steward.Sid -or $elevated -or (Test-AdminMember $sid)){throw 'Run as the non-elevated standard independent Steward identity.'}
            $readAllow=Test-ReadSentinel $allow $script:AllowSentinel; $readDeny=Test-ReadSentinel $deny $script:DenySentinel
            $tmp=Join-Path $script:Vault 'steward-write-probe.synthetic'
            try{[IO.File]::WriteAllText($tmp,'STEWARD_SYNTHETIC_WRITE_PROBE');$writeProbe='WRITE_ALLOWED'}catch{$writeProbe='WRITE_DENIED'}finally{Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}
            $passed=$readAllow -and $readDeny -and $writeProbe -eq 'WRITE_ALLOWED'
        }
        'VerifyDeveloper' {
            $developerSid=(New-Object Security.Principal.NTAccount($script:DeveloperName)).Translate([Security.Principal.SecurityIdentifier]).Value
            if($sid -cne $developerSid -or $elevated){throw 'Run as the recorded developer identity in a non-elevated shell.'}
            $readAllow=Test-ReadSentinel $allow $script:AllowSentinel; $readDeny=Test-ReadSentinel $deny $script:DenySentinel
            $passed=(-not $readAllow) -and (-not $readDeny)
        }
    }
    $out=[ordered]@{mode=$Mode;subject_sid=$sid;elevated=$elevated;allow_file=if($readAllow){'READ_ALLOWED'}else{'READ_DENIED'};deny_file=if($readDeny){'READ_ALLOWED'}else{'READ_DENIED'};steward_write=$writeProbe;status=if($passed){'PASS_IDENTITY_PROBE'}else{'FAIL_IDENTITY_PROBE'}}
    $out | ConvertTo-Json -Compress
    if(-not $passed){throw 'ACL identity test failed.'}
}

function Invoke-AuditProbe {
    Assert-Admin
    if($AuditSinceUtc -eq [datetime]::MinValue){throw 'Supply -AuditSinceUtc for the actual ACL test window.'}
    $managed=Assert-ManagedVault; $wanted=@($managed.Marker.runner_sid,$managed.Marker.steward_sid,$managed.Marker.developer_sid)
    $events=Get-WinEvent -FilterHashtable @{LogName='Security';Id=@(4656,4663,4670);StartTime=$AuditSinceUtc.ToLocalTime()} -ErrorAction SilentlyContinue
    $matched=foreach($event in $events){
        $xml=[xml]$event.ToXml();$data=@{}
        foreach($node in $xml.Event.EventData.Data){$data[[string]$node.Name]=[string]$node.InnerText}
        if($wanted -contains $data.SubjectUserSid -and $data.ObjectName -like "$($script:Vault)*"){
            [pscustomobject]@{time_utc=$event.TimeCreated.ToUniversalTime().ToString('o');event_id=$event.Id;subject_sid=$data.SubjectUserSid;object_path=$data.ObjectName;access_mask=$data.AccessMask}
        }
    }
    $matched | ConvertTo-Json -Depth 4
    if(-not $matched){throw 'No matching SACL access records; file access audit remains NOT_VERIFIED.'}
}

function Invoke-Rollback {
    Assert-Admin; Assert-Machine
    Assert-ApprovedVm
    if(-not $ConfirmRollback){throw 'Rollback requires -ConfirmRollback and exact typed confirmation.'}
    $typed=Read-Host 'Type exactly ROLLBACK SHOPRELAY EVAL VAULT'
    if($typed -cne 'ROLLBACK SHOPRELAY EVAL VAULT'){throw 'Confirmation did not match.'}
    $state=Read-State
    if($state.machine -cne $script:Machine -or $state.vault -cne $script:Vault){throw 'Rollback state target mismatch.'}
    if(Test-Path -LiteralPath $script:Vault){
        $expected=@($script:AllowName,$script:DenyName,$script:MarkerName)|Sort-Object
        $actual=@(Get-ChildItem -LiteralPath $script:Vault -Force|Select-Object -ExpandProperty Name|Sort-Object)
        if(($expected -join '|') -cne ($actual -join '|')){throw 'Unexpected Vault contents; refusing deletion.'}
        if((Get-Content -LiteralPath (Join-Path $script:Vault $script:AllowName) -Raw) -cne $script:AllowSentinel -or (Get-Content -LiteralPath (Join-Path $script:Vault $script:DenyName) -Raw) -cne $script:DenySentinel){throw 'Sentinel mismatch; refusing deletion.'}
        $mark=Read-Marker;if($state.install_id -and $mark.install_id -cne $state.install_id){throw 'Install marker mismatch.'}
        Disable-LocalUser -Name $script:RunnerName -ErrorAction SilentlyContinue;Disable-LocalUser -Name $script:StewardName -ErrorAction SilentlyContinue
        $closed=[Security.AccessControl.DirectorySecurity]::new();$closed.SetAccessRuleProtection($true,$false);$closed.SetOwner($script:AdminsSid)
        Add-DirAllow $closed $script:SystemSid ([Security.AccessControl.FileSystemRights]::FullControl);Add-DirAllow $closed $script:AdminsSid ([Security.AccessControl.FileSystemRights]::FullControl)
        Set-Acl -LiteralPath $script:Vault -AclObject $closed
        Remove-Item -LiteralPath $script:Vault -Recurse -Force
    } else {
        foreach($name in @($script:RunnerName,$script:StewardName)){Disable-LocalUser -Name $name -ErrorAction SilentlyContinue}
    }
    Write-Record 'Rollback' 'VAULT_REMOVED_ACCOUNTS_DISABLED' 'Accounts retained disabled. Restore only the reviewed Runner user-right entries and audit subcategory; do not blindly restore a global policy snapshot.'
    Write-Output 'Vault removed if it matched the managed synthetic-only contents. Accounts are retained disabled for administrator review. User-right and audit-policy restoration requires comparison to the saved pre-change snapshots.'
}

try {
    switch($Mode){
        'Inspect' { Invoke-Inspect }
        'PrepareIdentities' { Invoke-PrepareIdentities }
        'ApplyVault' { Invoke-ApplyVault }
        {$_ -in 'VerifyRunner','VerifySteward','VerifyDeveloper'} { Invoke-IdentityProbe }
        'VerifyAudit' { Invoke-AuditProbe }
        'Rollback' { Invoke-Rollback }
    }
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
