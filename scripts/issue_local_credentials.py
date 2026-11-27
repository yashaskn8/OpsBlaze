"""Create separate Community credentials in a new private env file."""

import argparse
import os
from pathlib import Path
import secrets
import subprocess

WINDOWS_PRIVATE_ACL = """
$ErrorActionPreference = 'Stop'
$file = [System.IO.FileInfo]::new($env:OPSBLAZE_ISSUER_PATH)
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$sidType = [System.Security.Principal.SecurityIdentifier]
$acl = $file.GetAccessControl()
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in $acl.GetAccessRules($true, $false, $sidType)) {
    $acl.RemoveAccessRuleSpecific($rule)
}
$acl.SetOwner($user)
$acl.SetAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
    $user, 'FullControl', 'Allow'))
$file.SetAccessControl($acl)
$saved = $file.GetAccessControl()
$rules = $saved.GetAccessRules($true, $true, $sidType)
if (!$saved.AreAccessRulesProtected -or $rules.Count -ne 1 -or
    $rules[0].IdentityReference.Value -ne $user.Value -or
    $rules[0].AccessControlType -ne 'Allow' -or
    $saved.GetOwner($sidType).Value -ne $user.Value) { exit 1 }
"""


def issue_credentials(path: Path, review_epoch: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            if os.name == "nt":
                windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
                powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
                subprocess.run(
                    [str(powershell), "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PRIVATE_ACL],
                    env={**os.environ, "OPSBLAZE_ISSUER_PATH": str(path)},
                    check=True, capture_output=True,
                )
            else:
                os.fchmod(output.fileno(), 0o600)
            operator = secrets.token_urlsafe(32)
            reviewer = secrets.token_urlsafe(32)
            if secrets.compare_digest(operator, reviewer):
                raise RuntimeError("credential generation returned a duplicate")
            output.write(
                f"OPSBLAZE_LOCAL_TOKEN={operator}\n"
                f"OPSBLAZE_REVIEW_TOKEN={reviewer}\n"
                f"OPSBLAZE_REVIEW_EPOCH={review_epoch}\n"
            )
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError):
        path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new file in a private local directory")
    parser.add_argument("--review-epoch", type=int, default=1)
    args = parser.parse_args()
    if args.review_epoch < 1:
        parser.error("--review-epoch must be positive")
    try:
        issue_credentials(args.output.absolute(), args.review_epoch)
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        parser.error(f"could not create private credentials ({type(exc).__name__})")


if __name__ == "__main__":
    main()
