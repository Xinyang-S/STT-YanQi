# Vernest Updater

Vernest will use GitHub Releases as the update channel.

Tauri updater artifacts require a Tauri signing key pair. This is separate from a
Windows code-signing certificate.

Planned release endpoint:

```text
https://github.com/Xinyang-S/STT-YanQi/releases/latest/download/latest.json
```

Do not commit the private updater key. Store it in local environment variables
or in the GitHub Actions secret store when CI is added.
