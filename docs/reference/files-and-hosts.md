# Files and external agent hosts

Précis exposes one file contract across its remote MCP deployments. Redis/Valkey holds bounded query snapshots; PostgreSQL holds artifact identity, ownership, expiry, provenance and quota state; the managed user-data directory holds bytes.

## Export and download

Use a `data_ref` from `run_statement`, `run_metric` or `inspect_rows` or `search_hierarchy`, then call `export_dataset(data_ref, format="json"|"xlsx")`. Export reads the same cached snapshot; it does not run a new query. It preserves engine precision and the bounded rows omitted from the model preview. The former `run_statement_excel` and `run_metric_excel` tool names are removed.

The result contains `artifact`: `file_id`, filename, MIME type, byte size, SHA-256, version, artifact expiry, `download_url` and URL expiry. MCP framing also delivers a Markdown link and `resource_link`. Fetch the URL with HTTP GET. Call `get_file_download(file_id)` to renew an expired URL while the artifact remains available.

Download URLs expire within fifteen minutes; generated exports expire after twenty-four hours. Ownership, current grants and file availability are checked again on download. Updating a file invalidates older version-bound URLs. A resource link does not guarantee that the host downloads bytes into its sandbox.

## Upload raw bytes

Receiving external files is **disabled by default**. Set
`PRECIS_MCP_UPLOADS_ENABLED=true` on a trusted deployment and restart the server
to enable the upload tools and signed PUT endpoint. Without this opt-in, the
tools are omitted from discovery, direct calls are denied, and PUT returns 404
even for a previously issued capability. Exports, downloads and link renewal
remain enabled. This setting does not govern the full Précis application's
authenticated multipart upload workflow.

1. Call `begin_file_upload(filename, mime_type, size_bytes)` to reserve quota and receive `upload_id`, `upload_url`, method `PUT` and expiry.
2. PUT the exact raw bytes to that URL, without multipart wrapping or an OAuth header. Transfer capabilities authorize this request independently.
3. Call `complete_file_upload(upload_id)`. Retries within the reservation lifetime return the same `file_id`.

Files are limited to 10 MiB and owners to 100 MiB. Reservations count toward quota. A failed or partial transfer can retry within its fifteen-minute reservation; an individual receiving attempt is bounded to sixty seconds. Expired files and failed deletions continue counting until the GC removes physical storage. File registration does not ingest or modify financial data.

## Host adapters and acceptance

| Host | Integration contract | Acceptance status |
|---|---|---|
| An organisation's own agent | HTTPS GET/PUT plus the MCP lifecycle above; the caller controls its filesystem | Local fake-based verification pending; deployed transfer acceptance required |
| Claude.ai | Deliver the link; sandbox download/upload requires the host's permitted HTTP capabilities and network policy | Live account/sandbox acceptance pending |
| ChatGPT | Optional `import_chatgpt_file(file)` attachment adapter using `_meta["openai/fileParams"]`; signed GET is available by default; receiving files requires upload opt-in | Descriptor follows documented file inputs; live account acceptance pending |
| Copilot Studio | Map attachments through the host's connector/action and call the shared upload lifecycle | Live action/connector acceptance pending |

The ChatGPT importer requires both `PRECIS_MCP_UPLOADS_ENABLED=true` and explicit trusted hostnames in `PRECIS_FILE_IMPORT_HOSTS`. It accepts a host-supplied file object with required `download_url` and `file_id`, and optional `mime_type` and `file_name` properties. An actual import needs a supported filename. It permits HTTPS on port 443, requires all resolved addresses to be public, pins the connection to a validated address while verifying the original TLS hostname, rejects redirects and encoded responses, and limits bytes/time. It never forwards OAuth credentials or stores the transient URL. Keep the hostname allow-list narrow; this is not an arbitrary URL-fetch tool.

Host attachment metadata and sandbox access are separate capabilities. MCP resources and links alone do not provide a portable bidirectional filesystem bridge. The file-input descriptor is based on [OpenAI's file API reference](https://developers.openai.com/plugins/reference#file-apis). Claude sandbox network access depends on [the host's file features](https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude); Copilot attachment mapping is described in [Microsoft's connector guidance](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/pass-files-to-connectors). These references do not substitute for live acceptance.

## Copilot Studio attachment mapping recipe

Configure a File question with metadata, or use a topic variable from `First(System.Activity.Attachments)`. Map its `.Name` and `.Content` to a connector/flow file input. Tool inputs must use **Custom value** Power Fx mapping; automatic AI filling does not provide the file object. This mapping is documented by [Microsoft](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/pass-files-to-connectors).

In the host action, obtain the raw binary bytes and their actual size, call `begin_file_upload`, send those bytes as the PUT body, then call `complete_file_upload`. Handle the returned `file_id` as the durable identity while available. Do not send workbook contents through an LLM tool argument. The host action's binary conversion, authenticated MCP connection and HTTP permissions require deployment-specific acceptance.

## Operator requirements

Configure `ARTIFACT_SIGNING_KEY` (a distinct secret of at least 32 bytes), `PRECIS_BASE_URL` (public HTTPS origin), Redis/Valkey and `USER_DATA_DIR`. Apply the open migrations before startup. Back up file metadata and managed blobs together; snapshots in Redis are disposable. Keep signed URL query strings out of ingress logs and telemetry. The reference Nginx configuration permits 10 MiB transfers and suppresses URI-bearing access/error logs for transfer paths. The bundled Caddy runtime logger removes the capability query parameter using its [documented query filter](https://caddyserver.com/docs/caddyfile/directives/log#query) and [global logger configuration](https://caddyserver.com/docs/caddyfile/options#log); access logging stays disabled by default. `/readyz` checks artifact storage/schema/signing and Redis in addition to PostgreSQL and ClickHouse.
