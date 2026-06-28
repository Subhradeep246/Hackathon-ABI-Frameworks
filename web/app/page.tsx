async function getHealth(): Promise<{ status: string }> {
  const url = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${url}/healthz`, { cache: "no-store" });
    if (!res.ok) return { status: "error" };
    return (await res.json()) as { status: string };
  } catch {
    return { status: "unreachable" };
  }
}

export default async function Home() {
  const health = await getHealth();
  const ok = health.status === "ok";
  return (
    <main className="min-h-screen flex flex-col items-center justify-center p-8">
      <div className="max-w-xl w-full space-y-8">
        <header className="space-y-2">
          <h1 className="text-4xl font-bold tracking-tight">Wound IQ</h1>
          <p className="text-neutral-400">
            Medicare Part B wound care billing eligibility · Phase 0 scaffold
          </p>
        </header>

        <section className="rounded-lg border border-neutral-800 p-6 space-y-3">
          <h2 className="text-lg font-semibold">API status</h2>
          <div className="flex items-center gap-3">
            <span
              className={`inline-block h-3 w-3 rounded-full ${
                ok ? "bg-emerald-500" : "bg-red-500"
              }`}
              aria-hidden
            />
            <span className="text-sm text-neutral-300">
              {ok ? "API is healthy" : `API: ${health.status}`}
            </span>
          </div>
          <p className="text-xs text-neutral-500">
            Reading from <code className="text-neutral-400">/healthz</code>
          </p>
        </section>

        <footer className="text-xs text-neutral-500">
          See <code>PRD.md</code> for the full architecture.
        </footer>
      </div>
    </main>
  );
}
