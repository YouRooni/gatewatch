# Failed-password capture

Optional integration for standard Debian/Ubuntu `pam_unix` SSH stacks. Off by default.

From the installed release's `source` directory:

```sh
sudo bash scripts/install-pam.sh enable
```

Use `disable` to undo it. The installer backs up PAM first; custom authentication stacks require manual review. Keep an SSH session open while checking a new login.

Captured failed passwords reach the local queue, hub database and notification channel. A mistyped legitimate password may be included. Successful passwords are discarded in memory. OpenSSH substitutes passwords for invalid users and some prohibited logins, so those originals are unavailable. The hook always returns PAM_IGNORE and does not decide authentication. [Security](../../SECURITY.md).
