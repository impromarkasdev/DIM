# Impromarkas PDF processor

## Procesamiento asíncrono y cola durable (búsqueda/redacción)

`POST /api/documents/process-redact` y `POST /api/admin/split-pdf` ya no esperan a que Graph termine. Validan y guardan el trabajo y sus ítems en Neon, publican cada ítem en Vercel Queues y responden `202 Accepted` con `jobId`. Wix consulta `GET /api/documents/jobs?jobId=<uuid>` con el mismo JWT hasta que `status` sea `completed`, `partial` o `failed`. Cada referencia o separación se procesa de forma independiente y la cola vuelve a entregar automáticamente mensajes cuyo consumidor falle. El consumidor es privado y está declarado en `app.queue_worker`.

Neon es la fuente durable del estado y Vercel Queues es el transporte durable de ejecución. Las tablas `dim_jobs` y `dim_job_items` se crean/migran de forma idempotente al enviar el primer trabajo. No se necesita un Cron para despertar los trabajos.

### Contrato de API asíncrona

Solicitud (Bearer JWT requerido):

```http
POST /api/documents/process-redact
Content-Type: application/json
Idempotency-Key: <clave-opcional-generada-por-Wix>
```

```json
{
  "references": ["A367-LLA-81", "ANC-2526"],
  "datosSensibles": []
}
```

Respuesta `202`:

```json
{
  "success": true,
  "jobId": "uuid",
  "status": "queued",
  "total": 2,
  "completed": 0,
  "statusUrl": "/api/documents/jobs?jobId=uuid"
}
```

El status endpoint devuelve `resultados` por referencia; el `downloadUrl` aparece al terminar satisfactoriamente ese documento. Los enlaces de Graph son temporales y se regeneran al consultar el trabajo terminado. El estado sólo puede consultarlo el usuario que creó el trabajo (o un administrador). El máximo es 30 referencias por trabajo.

Ejemplo para `backend/api.jsw` en Wix Velo:

```js
import { fetch } from 'wix-fetch';

const API = 'https://dim-xi.vercel.app';

async function json(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.success === false) {
    throw new Error(data.message || `Error HTTP ${response.status}`);
  }
  return data;
}

export async function startRedactionJob(token, references, idempotencyKey) {
  const response = await fetch(`${API}/api/documents/process-redact`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
      'Idempotency-Key': idempotencyKey
    },
    body: JSON.stringify({ references })
  });
  return json(response);
}

export async function getRedactionJob(token, jobId) {
  const response = await fetch(
    `${API}/api/documents/jobs?jobId=${encodeURIComponent(jobId)}`,
    { headers: { 'Authorization': `Bearer ${token}` } }
  );
  return json(response);
}

export async function startSplitJob(token, filePath, idempotencyKey) {
  const response = await fetch(`${API}/api/admin/split-pdf`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
      'Idempotency-Key': idempotencyKey
    },
    body: JSON.stringify({ filePath })
  });
  return json(response);
}
```

En las páginas Wix: llama una vez `startRedactionJob` o `startSplitJob`, conserva `jobId` y consulta `getRedactionJob` con `setTimeout` cada 2–3 segundos. Detén el polling cuando el estado sea terminal (`completed`, `partial` o `failed`). En búsqueda, muestra botones sólo para resultados con `estado === 'Exito'` y `downloadUrl`. Para separación, presenta `count`, `files` y `skippedPages` de `resultados[0]` al finalizar. Mantén los botones en un contenedor de altura fija o reserva su espacio para no desplazar el formulario. El usuario puede cerrar Wix: el trabajo y el resultado siguen persistidos en Neon.

Los PDFs censurados se guardan en OneDrive por DriveItem ID. Neon no conserva el enlace temporal de Graph: cada consulta autenticada del estado emite un `downloadUrl` recién generado. Así, al regresar al dashboard se consulta de nuevo el mismo `jobId` y se obtiene un enlace vigente. También existe `GET /api/documents/download?jobId=...&itemIndex=...&fileIndex=...`, que transmite el PDF después de validar JWT y propiedad del trabajo. La descarga directa debe abrirse pronto; el enlace temporal sólo se devuelve al frontend y nunca se persiste.

Operación de fallos: `GET /api/admin/jobs` lista trabajos con elementos fallidos y `POST /api/admin/jobs` con `{ "jobId": "..." }` reprograma sólo los ítems fallidos. Ambos requieren JWT de administrador. Cada reintento incrementa la generación de idempotencia para que Vercel no lo confunda con un mensaje anterior. Al agotarse diez intentos, el sistema registra el error seguro y, si está configurado, notifica el webhook HTTPS `ADMIN_ALERT_WEBHOOK_URL` (por ejemplo, un webhook privado de Teams/Slack). Sin ese valor, la falla se ve en logs y en la lista de trabajos del administrador.

OCR en Vercel: define `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` y `AZURE_DOCUMENT_INTELLIGENCE_KEY`. Sólo si el texto seleccionable no permite reconocer Campo 4, el worker construye un PDF temporal con los recortes de esa casilla y los envía al modelo `prebuilt-read` de Azure Document Intelligence en una sola operación; las páginas completas no se envían. Revisa privacidad, región y precio del recurso Azure. `pytesseract` es sólo una alternativa local y requiere que el ejecutable Tesseract esté instalado en ese equipo.

Duración del worker: `vercel.json` configura hasta 300 segundos para `app/queue_worker.py`; la API de recepción permanece en 60 segundos. Tras desplegar, confirma en el resumen del build que el suscriptor `app.queue_worker` quedó registrado y verifica en el detalle de funciones el límite efectivo del worker. Los máximos disponibles dependen de plan y configuración de Fluid Compute.

Ejemplo de polling para una página Velo (el estado y los archivos se actualizan sin mover los controles):

```js
import { startRedactionJob, getRedactionJob } from 'backend/api';

let polling = false;

async function submitAndPoll(token, references) {
  if (polling) return;
  polling = true;
  $w('#txtStatus').text = 'Solicitud recibida. Procesando…';
  $w('#resultsBox').collapse();
  try {
    const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const accepted = await startRedactionJob(token, references, key);
    const jobId = accepted.jobId;
    if (!jobId) throw new Error('El servidor no devolvió el identificador del trabajo.');

    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 2500));
      const job = await getRedactionJob(token, jobId);
      $w('#txtStatus').text = `Procesando: ${job.completed} de ${job.total}`;
      if (!['completed', 'partial', 'failed'].includes(job.status)) continue;

      const ready = (job.resultados || []).filter(
        item => item.estado === 'Exito' && item.downloadUrl
      );
      renderDownloadButtons(ready); // Mantén #resultsBox con altura fija.
      $w('#txtStatus').text = ready.length
        ? `Listo. ${ready.length} documento(s) disponible(s).`
        : 'El trabajo terminó, pero no hubo documentos disponibles para descargar.';
      return;
    }
  } catch (error) {
    $w('#txtStatus').text = error.message || 'No fue posible consultar la solicitud.';
  } finally {
    polling = false;
  }
}
```

`renderDownloadButtons` representa la integración visual con los elementos de Wix de la página; sólo debe actualizar los controles dentro de un contenedor fijo, no insertar elementos que alteren el flujo del formulario. Para que el usuario retome un trabajo después de cerrar sesión o recargar Wix, guarda el `jobId` junto con el token en almacenamiento de sesión y vuelve a consultar el mismo endpoint al abrir el dashboard.

### Configuración y despliegue

El SDK oficial de Vercel Queues (`vercel-queue`) está listado en `pyproject.toml` y en ambos requirements. `pyproject.toml` registra el dispatcher HTTP existente y el módulo de suscriptor; no cambies esos entrypoints sin revisar la configuración del runtime. Vincula el checkout a `dim-xi` con Vercel CLI, asegúrate de que Neon `POSTGRES_URL` y el resto de variables estén definidos para Production, y luego despliega. Vercel Queues usa OIDC automáticamente dentro de un deployment; para pruebas locales se necesita el proyecto enlazado y las credenciales locales obtenidas por el CLI. La disponibilidad real de Vercel Queues debe verificarse en el dashboard del proyecto/plan.

Verificación post-deploy: (1) confirmar en el build que se detectó el subscriber `app.queue_worker`; (2) iniciar un trabajo con una referencia válida; (3) confirmar HTTP `202` y que Wix conserva `jobId`; (4) consultar el endpoint protegido hasta terminal; (5) descargar el PDF y validar la redacción. No vuelvas a llamar al endpoint síncrono esperando el `downloadUrl` en la respuesta inicial.

Endpoint: `POST /api/process`. It receives one or more Wix references, downloads the OneDrive workbook once into memory, searches year worksheets locally, and returns irreversibly-redacted PDFs as Base64. It does not send email, upload files, or modify Excel.

## Expected request

```json
{
  "referencias": ["REF-001", "REF-002"],
  "datosSensibles": ["12345678", "Ana Pérez"]
}
```

The legacy endpoint also accepts a raw array such as `["REF-001", "REF-002"]`. `datosSensibles` is optional. `POST /api/process` is disabled unless a non-empty `WEBHOOK_SECRET` is configured and supplied in `X-Webhook-Secret`.

The workbook is searched in descending order from the current year through `EXCEL_FIRST_YEAR` (for example `2026`, `2025`, …, `2021`). This means that when the calendar reaches 2027, worksheet `2027` is searched automatically. Lookup uses zero-based configured column indices; the defaults reproduce a VLOOKUP range where the reference is column 1 and the PDF/location are columns 3 and 4.

## Microsoft Graph permissions

The current production configuration uses Microsoft Graph application permissions: `AZURE_AUTH_MODE=client_credentials`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_CLIENT_SECRET`, with `Files.ReadWrite.All` admin-consented in Entra ID. Select the target with `GRAPH_DRIVE_ID` (preferred) or `GRAPH_USER_ID`; the workbook is downloaded once per request and searched in memory. Copy `.env.example` to `.env` locally. Vercel uses Project Settings → Environment Variables rather than uploading `.env`.

If you intentionally switch to delegated refresh-token authentication, set `AZURE_AUTH_MODE=refresh_token`, configure `AZURE_REFRESH_TOKEN` and `AZURE_SCOPES`, and obtain a new token with the required delegated consent. Do not mix delegated and application permission setup; use the mode that matches the Entra app and deployment variables.

## Verify OneDrive

After deploying and setting the Vercel variables, request `GET /api/process` with the `X-Webhook-Secret` header. It verifies access to the selected drive and workbook and returns non-secret connection metadata. It never returns access tokens, refresh tokens, client secrets, or file contents.

## Vercel Cron keep-alive

`vercel.json` invokes `GET /api/cron/keep-alive` every Monday at 00:00 UTC. Set a distinct, random `CRON_SECRET` in Vercel. The endpoint verifies authentication and calls a minimal Microsoft Graph endpoint; it does not expose secrets or return tokens.

For a successful reference, the job result contains its form number, status, diagnostics, and an authenticated download link. The link is regenerated from the stored DriveItem ID when the authenticated status endpoint is called; temporary Graph URLs are never stored in Neon. Search uses the whole configured drive's Graph search endpoint, exhausts all result pages, validates exact form names, and then searches the relevant container hierarchy. A Graph error is surfaced as a retryable job error, not reported as “not found.” Graph's drive search is an indexed search, so very recently uploaded/renamed items may take time to appear; verify a failed item through admin job diagnostics and retry once indexing catches up.

Legacy `POST /api/process` remains synchronous for existing webhook callers. The Wix search/redaction UI should use the queued endpoints above.
