# Ledger sync v1 (Windows / Android)

Both clients reuse their configured shared WebDAV URL, credentials and remote directory.
The ledger namespace is `<remote_path>/ledger-v1/`, with `ops/<op_id>.json` and
`blobs/<sha256>`. No mutable snapshot or settings overwrite participates.

Every operation is UTF-8 JSON: `{"schema":1,"op_id":"32 lowercase hex",
"entity_type":"entry|project|network_profile","entity_id":"32 lowercase hex","parents":["op ids"],
"changes":{"field":value},"deleted":false,"created_at":1780000000.0}`.
IDs are random UUID hex (no dashes), time is Unix seconds and is informational only.
`parents` contains all currently known heads for THIS entity. Operations are immutable;
PUT with If-None-Match:*; an existing name must have equivalent parsed JSON.
Downloaded operations with missing parents stay durable but must not be materialized
until all ancestors arrive. Filename must equal op_id. No parent crosses entities.

Field merge: among operations changing a field, discard any writer that is an ancestor
of another writer. One remaining value wins; equal values coalesce. Different concurrent
values are retained as conflict candidates `{op_id,value}`. Until explicitly resolved,
display the candidate with lexicographically largest op_id. A resolving patch contains
the chosen field and ALL current entity heads as parents. Disjoint fields merge freely.
Any `deleted:true` permanently deletes that entity (remove wins; recreate with a new id).
Tombstones and operation history are never automatically removed.

Entry fields: title, date (YYYY-MM-DD), amount (decimal string with 2 places),
amount_minor (integer cents), currency (CNY/USD/EUR/HKD), entry_type (expense/income),
status (waiting/reimbursed/non_reimbursable; income uses not_applicable), category,
notes, project_id, created_at (Unix seconds). Amount and amount_minor MUST change together;
derive amount_minor from the winning amount to avoid incoherent conflict previews.
Attachments are individual fields `attachment:<32hex attachment id>` with value
`{id,original_name,kind,size,sha256,relative_path,created_at}`; kind is
invoice/receipt/payment/other. `relative_path` is
`attachments/<entry id>/<attachment id>.<pdf|png|jpg|jpeg|webp|ofd>`.
Removing an attachment writes null (concurrent null wins to avoid resurrection).
Transfer bytes through blobs/<sha256>; verify size AND SHA256. Upload blobs BEFORE ops;
download/verify blobs BEFORE applying referencing ops. Never sync absolute paths.

Project fields: name, settlement_mode (general/half), created_at, archived (boolean).
Built-in AI报销 project ID: `00000000000000000000000000000001`, mode half.
It exists implicitly on each device (no initialization op required, archived=false). Legacy PC entries
migrate into this project once. Other projects default general; no forced 50/50 split.

Network profile fields: name, target, port (integer or null for protocol default),
timeout_ms (integer), attempts (integer). Credentials and diagnostic history stay local.

PC API: expenses.projects.list => {items}; expenses.projects.save {id?,name?,settlement_mode?,archived?,heads?};
expenses.list additionally accepts project_id (default built-in; all supported),
start_date/end_date (inclusive, instead of month). expenses.save accepts project_id.
expenses.sync => background job; expenses.sync.status => configured,syncing,pending,last_sync,error,conflicts.
expenses.conflicts => {items}; expenses.resolve {entity_type,id,changes,heads}.
expenses.export {project_id,start_date,end_date,format:xlsx|csv} => background job,
result includes path and rows; job has downloadable artifact. No destination required.
Conflict result: `{items:[{entity_type,id,heads,fields:{field:[{op_id,value}]}}]}`.
`amount_minor` conflicts are hidden because the winning amount determines them;
resolving amount writes BOTH amount and derived amount_minor. Income previews force
status=not_applicable; expense previews map not_applicable back to waiting.
Local writes remain durable offline; startup, post-change and periodic sync retry.
Root startup hook: app.ledger_auto_sync(); stop hook: app.ledger_close().
Backup restore hooks: app.ledger_before_restore(), app.ledger_after_restore().
The sync endpoint reports remote_path so users can match Android's separate shared
sync root (default WinToolbox); Android's relay file directory is unrelated.
Project archives retain entries, and exports can include archived projects.
