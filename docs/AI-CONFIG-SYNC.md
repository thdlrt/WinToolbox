> Windows 0.4.1 / Android 0.6.2 起，AI 配置并入[统一配置备份](CONFIG-BACKUP.md)。下文保留 v1 协议供兼容读取，界面不再单独上传 AI 配置。

# AI configuration sharing v1

Both clients manually share `<shared WebDAV root>/ai-config/config-v1.json`.
No new connection, directory or password field is introduced. The WebDAV password
is the encryption password; rotating it requires uploading the config again.

Plaintext UTF-8 JSON:
`{format:"wintoolbox-ai-config",version:1,providers:[...],roles:{...},secrets:{provider_id:api_key}}`.
Providers use the existing PC fields id/name/kind/base_url/region (optional model).
Provider names may be omitted; IDs and role names use `[A-Za-z0-9._-]{1,100}`.
Role model names are strings of at most 200 characters and may be empty until
configured. Region is at most 50 characters; each API key is at most 8192 characters.
Kinds: openai, dashscope, gemini. Roles contain provider_id/model; local roles may
use provider_id="local" without a cloud provider or secret. Only AI configuration
is shared; preferences, presets, ledger and other application settings stay local.

Envelope JSON:
`{format:"wintoolbox-ai-config-encrypted",version:1,salt:"base64",nonce:"base64",ciphertext:"base64"}`.
Use AES-256-GCM, 16 random salt bytes, 12 random nonce bytes, 16-byte GCM tag
appended to ciphertext. Key derivation: PBKDF2-HMAC-SHA256, password UTF-8,
200000 iterations, 32 output bytes. AAD is UTF-8 `wintoolbox-ai-config-v1`.
Both envelope and plaintext are bounded to 1 MiB. Invalid authentication/schema
must leave local settings and credentials unchanged. Secret values never appear
in status, previews, jobs, logs or normal API responses.

PC APIs:

- `ai.config.status`: configured, remote_path, local_providers (redacted), last_upload,
  last_download, error. No network request.
- `ai.config.export`: returns the encrypted envelope using the saved WebDAV password.
- `ai.config.upload`: background job, manual overwrite of the shared encrypted file.
- `ai.config.download {}`: background preview job, returning confirmation_token,
  providers (redacted), roles and secret_count. It does not modify local settings.
- `ai.config.download {confirmation_token,confirm_replace:true}`: background apply job;
  replaces only providers/roles/secrets after explicit confirmation. The token expires
  after 15 minutes and is rejected if the connection or local AI configuration changed.

Uploads use a temporary encrypted object and MOVE with overwrite; interrupted
uploads retain the previous complete object. Local apply keeps protected rollback
bytes in memory and updates both settings and DPAPI-protected secrets under the
settings lock. Download never uses the configuration-backup password.

The deterministic cross-language fixture is
`tests/fixtures/ai-config-vector-v1.json`, with synthetic credentials only.
