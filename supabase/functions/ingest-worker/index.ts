// Supabase Edge Function: ingest-worker
//
// pg_cron invokes this every minute. It:
//   1. asks the API to requeue jobs orphaned by a crashed worker;
//   2. asks the API to drain due ingestion jobs (bounded batch).
//
// Required function secrets (supabase secrets set ...):
//   API_BASE_URL            e.g. https://api.your-domain.com
//   API_INTERNAL_TOKEN      same value as the API's API_INTERNAL_TOKEN
//
// Deployed with verify_jwt=true so only the service-role JWT used by pg_cron
// can reach it.

const API_BASE_URL = (Deno.env.get("API_BASE_URL") ?? "").replace(/\/+$/, "");
const API_INTERNAL_TOKEN = Deno.env.get("API_INTERNAL_TOKEN") ?? "";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", Connection: "keep-alive" },
  });
}

async function callApi(path: string, body: unknown): Promise<{ status: number; body: unknown }> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${API_INTERNAL_TOKEN}`,
    },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  let parsed: unknown = text;
  try {
    parsed = JSON.parse(text);
  } catch {
    // keep raw text
  }
  return { status: response.status, body: parsed };
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST" && req.method !== "GET") {
    return json({ error: "Method not allowed" }, 405);
  }
  if (!API_BASE_URL || !API_INTERNAL_TOKEN) {
    return json(
      {
        ok: false,
        error:
          "ingest-worker is not configured: set API_BASE_URL and API_INTERNAL_TOKEN as Edge Function secrets",
      },
      503,
    );
  }

  const started = Date.now();
  let maxJobs = 5;
  try {
    const payload = req.method === "POST" ? await req.json().catch(() => ({})) : {};
    if (typeof payload?.max_jobs === "number") {
      maxJobs = Math.min(Math.max(1, payload.max_jobs), 20);
    }
  } catch {
    // default batch size
  }

  try {
    const recovered = await callApi("/internal/recover", { stale_minutes: 15 });
    const processed = await callApi(`/internal/cron/tick?max_jobs=${maxJobs}`, {
      source: "edge:ingest-worker",
    });
    const ok = recovered.status < 400 && processed.status < 400;
    return json(
      {
        ok,
        duration_ms: Date.now() - started,
        recovered: recovered.body,
        processed: processed.body,
        upstream_status: { recovered: recovered.status, processed: processed.status },
      },
      ok ? 200 : 502,
    );
  } catch (error) {
    return json(
      {
        ok: false,
        error: `ingest-worker could not reach the API: ${(error as Error).message}`,
        duration_ms: Date.now() - started,
      },
      502,
    );
  }
});
