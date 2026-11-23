# Release signing keyring

`release-signing.gpg` is the **public** keyring, in binary form (`gpg --export`), that verifies signed releases. `tools/packaging/build-deb.sh` installs it as `/opt/qmi-supervisor/keys/release-signing.gpg`, and the updater checks the `SHA256SUMS.asc` of every release against it with `gpgv` before installing anything.

- `tools/packaging/make-signing-key.sh` creates it on the maintainer's computer. A private key never belongs in this directory or anywhere in the repository.
- Its fingerprint is published in [INSTALLATION.md](../../../INSTALLATION.md#signatures).
- While the file is absent, packages cannot verify releases. See [Signing key](../README.md#signing-key) for the setup and for key rotation.
