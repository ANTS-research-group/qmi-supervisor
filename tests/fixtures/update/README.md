# Update signature fixtures

Generated once by `tools/update/make_test_signing_fixtures.sh`; the tests only
need `gpgv`. The private keys were generated inside temporary `GNUPGHOME`
directories and discarded with them.

- `test-keyring.gpg` — binary keyring with the TEST signing key.
- `fingerprint.txt` — the primary-key fingerprint gpgv must report.
- `qmi-supervisor_9.9.0-1_all.deb` — a 36-byte text file, not a real package.
- `SHA256SUMS` — checksum of that file, signed by the TEST key in
  `SHA256SUMS.asc`.
- `SHA256SUMS.other.asc` — the same file signed by the OTHER key (not in the
  keyring).
- `SHA256SUMS.tampered` — a modified checksum file.
