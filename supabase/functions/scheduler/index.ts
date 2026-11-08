// Supabase Edge Function: scheduler
//
// pg_cron (supabase/migrations/0003_cron.sql) invokes this every minute. It is a
// thin, authenticated relay to the FastAPI scheduler so the domain logic lives
// in one place:
//
//   POST {API_BASE_URL}/internal/cron/tick
//
// Required function secrets (supabase secrets set ...):
//   API_BASE_URL            e.g. https://api.your-domain.com
//   API_INTERNAL_TOKEN      same value as the API's API_INTERNAL_TOKEN
//
// Deployed with verify_jwt=true, so only the service-role JWT used by pg_cron
// (or another privileged caller) can reach it.

const API_BASE_URL = (Deno.env.get("API_BASE_URL") ?? "").replace(/\/+$/, "");
const API_INTERNAL_TOKEN = Deno.env.get("API_INTERNAL_TOKEN") ?? "";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", Connection: "keep-alive" },
  });
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST" && req.method !== "GET") {
    return json({ error: "Method not allowed" }, 405);
  }

  if (!API_BASE_URL || !API_INTERNAL_TOKEN) {
    // Honest failure: do not pretend a schedule ran.
    return json(
      {
        ok: false,
        error:
          "scheduler is not configured: set API_BASE_URL and API_INTERNAL_TOKEN as Edge Function secrets",
      },
      503,
    );
  }

  const started = Date.now();
  try {
    const response = await fetch(`${API_BASE_URL}/internal/cron/tick`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${API_INTERNAL_TOKEN}`,
      },
      body: JSON.stringify({ source: "edge:scheduler" }),
    });
    const text = await response.text();
    let result: unknown = text;
    try {
      result = JSON.parse(text);
    } catch {
      // keep raw text
    }
    return json(
      {
        ok: response.ok,
        upstream_status: response.status,
        duration_ms: Date.now() - started,
        result,
      },
      response.ok ? 200 : 502,
    );
  } catch (error) {
    return json(
      {
        ok: false,
        error: `scheduler could not reach the API: ${(error as Error).message}`,
        duration_ms: Date.now() - started,
      },
      502,
    );
  }
});
