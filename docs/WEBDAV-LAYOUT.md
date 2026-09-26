# Shared WebDAV layout v2

Both clients expose ONE connection: URL, username, password and root `remote_path`
(existing configured root retained; new default `WinToolbox`). Services derive fixed
children; do not expose per-service connection or remote-directory controls.

| Relative to shared root | Purpose |
| --- | --- |
| `config-backups/windows` | Manual Windows configuration backups (.wtbak) |
| `config-backups/android` | Manual Android configuration backups |
| `ledger-v1/ops`, `ledger-v1/blobs` | Existing immutable ledger/network-profile merge sync |
| `file-relay` | Shared file inbox |
| `project-memory` | Existing immutable project memory sync |

Create service child directories automatically. The parent of the selected shared
root must exist; never treat an unrelated relay folder as the shared root.

Windows `webdav.get/save` remains the only connection API. `webdav.get` includes
`service_paths`. `relay.get` retains compatibility fields but always uses shared
credentials and derived `remote_path`; relay.save only changes download_dir/menu.
Legacy relay connection/path is retained privately for safe migration; copy old
files to canonical file-relay without deleting sources or replacing destination
files. Collisions use a stable `legacy-<source fingerprint>/` subdirectory; migration
failures are surfaced and retried. The legacy source remains intact.
`relay.get` and relay list/test job results include `migration:{pending,warning,
legacy_path,copied}`. When the shared root changes, retain the previous canonical
file-relay directory as another migration source. A source that contains the new
destination never traverses/copies that destination into itself.

`webdav.upload` creates configuration-only snapshots. It includes settings and
configuration, optionally decrypted API credentials ONLY inside password-encrypted
backup envelopes. It excludes expenses, ledger operations/blobs, memory records,
files, media, jobs, databases and other business data. `webdav.restore` overwrites
only the configuration allowlist with a recovery copy; ledger/files/business data
remain intact. Both operations are manual. Local full backups remain available.

`webdav.list` reports new rows with `scope:"config"`, `location:"config"` and old
root snapshots with `scope:"legacy_full"`, `location:"legacy_root"`. Restore accepts
location and safely imports only configuration fields from old full snapshots too;
full legacy recovery remains the local backup-import workflow after downloading.
The Windows configuration allowlist is settings.json (model providers/roles,
preferences and presets), orb-settings.json, memory-cleaner.json and
fnconnect-tun.json. Existing per-device GPU registry state and connection credentials
are not included. Recovery is a local directory containing the previous configuration
and any old DPAPI-protected API secrets. Paths in preferences remain literal settings;
restoring settings does not move or download referenced local files.

Changing the shared root/account/endpoint queues read-only pulls from previous ledger
roots before republishing merged operations to the new root. No writes are sent to
old ledger roots. An unavailable old root produces migration_pending/migration_error
in expenses.sync.status while new-root sync continues. Private migration connection
snapshots live in webdav-migrations.json (outside config backups and ledger sync).
Credential rotation on the same endpoint/account reuses the current shared password
for old-path migration; different connections retain their previous protected secret.

Android migrates its previous ledger-specific root into shared root only when no
shared root is configured, independently of old relay directory. Existing ledger
operations at old root are merged through their immutable IDs, never replaced.
Configuration backup formats are platform-specific and stored in separate children.
