# Impromarkas PDF processor

Endpoint: `POST /api/process`. It receives one or more Wix references, downloads the OneDrive workbook once into memory, searches year worksheets locally, and returns irreversibly-redacted PDFs as Base64. It does not send email, upload files, or modify Excel.

## Expected request

```json
{
  "referencias": ["REF-001", "REF-002"],
  "datosSensibles": ["12345678", "Ana Pérez"]
}
```

The endpoint also accepts a raw array such as `["REF-001", "REF-002"]`. `datosSensibles` is optional; configured standard terms are always included. Set `X-Webhook-Secret` when `WEBHOOK_SECRET` is configured.

The workbook is searched in descending order from the current year through `EXCEL_FIRST_YEAR` (for example `2026`, `2025`, …, `2021`). This means that when the calendar reaches 2027, worksheet `2027` is searched automatically. Lookup uses zero-based configured column indices; the defaults reproduce a VLOOKUP range where the reference is column 1 and the PDF/location are columns 3 and 4.

## Microsoft Graph permissions

The service uses delegated OAuth refresh-token authentication: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_REFRESH_TOKEN`. It refreshes an access token for each serverless invocation using `offline_access User.Read Files.ReadWrite`. Omit `GRAPH_DRIVE_ID` to use the authenticated user's `/me/drive`; set it only for an explicitly selected accessible drive. The workbook does not need an Excel table because it is read directly in memory. Copy `.env.example` to `.env` locally. Vercel uses Project Settings → Environment Variables rather than uploading `.env`.

When the workbook is in a shared SharePoint/OneDrive library, add delegated `Files.ReadWrite.All` in Entra ID, grant consent, set `AZURE_SCOPES=offline_access User.Read Files.ReadWrite.All`, and obtain a new refresh token with that exact scope. `Files.ReadWrite.All` grants the delegated user access to files they can access across site collections; it does not grant access beyond that user's own permissions.

## Verify OneDrive

After deploying and setting the Vercel variables, request `GET /api/process` with the `X-Webhook-Secret` header. It safely returns the delegated username, selected drive id/type and configured workbook name/size. It never returns an access token, refresh token or client secret.

## Vercel Cron keep-alive

`vercel.json` invokes `GET /api/cron/keep-alive` every Monday at 00:00 UTC. Vercel sends `Authorization: Bearer <CRON_SECRET>` automatically when `CRON_SECRET` is set in the project. The endpoint refreshes the delegated Azure token and calls `GET /me`; it does not expose secrets or return tokens. Set a distinct, random `CRON_SECRET` in Vercel.

> Microsoft can issue a rotated refresh token on a refresh response. Vercel environment variables are immutable at runtime, so for long-running production use store any new refresh token in a secret manager or database and update it through a controlled deployment process before the prior token expires.

Each successful result contains `nombre_pdf`, `ubicacion`, and `pdf_base64`; an absent reference returns `Pendiente (No encontrado)`. `MAX_RESPONSE_PDF_BYTES` limits the raw sanitized PDF size so the Base64 JSON response stays within Vercel limits.

Deploy with `vercel --prod` after linking the repository. The endpoint returns `202` when its work completes successfully.
