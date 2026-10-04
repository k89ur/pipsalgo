import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

export type DiagnosticCheck = {
  id: string;
  label: string;
  category: string;
  component: string;
  severity: string;
  status: "OK" | "WARNING" | "ERROR" | "SKIPPED";
  error_code?: string;
  message: string;
  technical_detail?: string;
  duration_ms?: number;
  symbol?: string;
};

type DiagnosticEvent = {
  id: number;
  account_id: number | null;
  broker: string;
  severity: string;
  category: string;
  component: string;
  service: string;
  error_code: string;
  symbol: string;
  http_status: number | null;
  provider_code: string;
  message: string;
  technical_detail: string;
  resolved: boolean;
  occurrence_count: number;
  first_seen: string;
  last_seen: string;
};

type Props = {
  accountId: number | null;
  embedded?: boolean;
  onClose?: () => void;
};

function statusClass(value: string) {
  return value.toLowerCase().replace(/[^a-z]+/g, "-");
}

export function StatusCenter({ accountId, embedded = false, onClose }: Props) {
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [events, setEvents] = useState<DiagnosticEvent[]>([]);
  const [checks, setChecks] = useState<DiagnosticCheck[]>([]);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState<number | null>(null);
  const [marketDataSource, setMarketDataSource] = useState("unknown");

  const load = useCallback(async () => {
    const query = accountId ? "?account_id=" + encodeURIComponent(String(accountId)) : "";
    const [summaryResponse, eventsResponse, sourceResponse] = await Promise.all([
      apiFetch("/api/diagnostics/summary" + query, { cache: "no-store" }),
      apiFetch("/api/diagnostics/events" + query + (query ? "&" : "?") + "limit=100", { cache: "no-store" }),
      apiFetch("/api/market-data/source", { cache: "no-store" }),
    ]);
    const summaryPayload = await summaryResponse.json().catch(() => ({}));
    const eventsPayload = await eventsResponse.json().catch(() => []);
    const sourcePayload = await sourceResponse.json().catch(() => ({}));
    if (summaryResponse.ok) setSummary(summaryPayload);
    if (eventsResponse.ok && Array.isArray(eventsPayload)) setEvents(eventsPayload);
    if (sourceResponse.ok && typeof sourcePayload.source === "string") {
      setMarketDataSource(sourcePayload.source);
    }
  }, [accountId]);

  useEffect(() => { void load(); }, [load]);

  const runCheck = async () => {
    if (!accountId) {
      setMessage("Connect a broker account before running diagnostics.");
      return;
    }
    setRunning(true);
    setMessage("");
    try {
      const response = await apiFetch("/api/startup/check/" + accountId, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Diagnostic check failed.");
      setChecks(Array.isArray(payload.checks) ? payload.checks : []);
      setSummary(payload.summary || null);
      await load();
      setMessage(payload.ready ? "System check completed successfully." : "System check found a blocking problem.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Diagnostic check failed.");
    } finally {
      setRunning(false);
    }
  };

  const status = String(summary?.status || "UNKNOWN");

  const content = (
    <section className={embedded ? "status-center embedded" : "status-center"}>
      <div className="status-center-header">
        <div>
          <div className="status-eyebrow">PIPSGOX SYSTEM</div>
          <h1>STATUS CENTER</h1>
          <p>Every important startup and runtime problem is recorded here.</p>
        </div>
        <div className="status-header-actions">
          {onClose && <button className="status-secondary" onClick={onClose}>CLOSE</button>}
          <button className="status-primary" disabled={running || !accountId} onClick={() => void runCheck()}>
            {running ? "RUNNING CHECK..." : "RUN FULL SYSTEM CHECK"}
          </button>
        </div>
      </div>

      {message && <div className={"status-message " + (statusClass(status))}>{message}</div>}

      <div className="status-overview-grid">
        <div className={"status-health-card " + statusClass(status)}>
          <span>OVERALL STATUS</span>
          <strong>{status}</strong>
          <small>{accountId ? "Selected broker account" : "Primary market data · no broker required"}</small>
          <small>Market data source: <strong>{marketDataSource.toUpperCase()}</strong></small>
        </div>
        <div className="status-stat"><span>CRITICAL</span><strong>{Number(summary?.critical || 0)}</strong></div>
        <div className="status-stat"><span>ERRORS</span><strong>{Number(summary?.errors || 0)}</strong></div>
        <div className="status-stat"><span>WARNINGS</span><strong>{Number(summary?.warnings || 0)}</strong></div>
      </div>

      {checks.length > 0 && (
        <div className="status-section">
          <div className="status-section-title">LATEST FULL CHECK</div>
          <div className="diagnostic-check-list">
            {checks.map((check) => (
              <div className={"diagnostic-check " + statusClass(check.status)} key={check.id}>
                <div className="diagnostic-check-icon">{check.status === "OK" ? "✓" : check.status === "WARNING" ? "!" : check.status === "SKIPPED" ? "–" : "×"}</div>
                <div className="diagnostic-check-main">
                  <strong>{check.label}</strong>
                  <span>{check.message}</span>
                  {check.error_code && <code>{check.error_code}</code>}
                </div>
                {advanced && check.technical_detail && (
                  <pre className="diagnostic-technical">{check.technical_detail}</pre>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="status-section">
        <div className="status-section-title">
          <span>DIAGNOSTIC HISTORY</span>
          <button className="status-text-button" onClick={() => setAdvanced((value) => !value)}>
            {advanced ? "SIMPLE VIEW" : "ADVANCED VIEW"}
          </button>
        </div>
        {events.length ? (
          <div className="diagnostic-event-list">
            {events.map((event) => (
              <div
                className={"diagnostic-event " + statusClass(event.severity)}
                key={event.id}
                role="button"
                tabIndex={0}
                onClick={() => setSelectedEvent(selectedEvent === event.id ? null : event.id)}
                onKeyDown={(keyboardEvent) => {
                  if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
                    keyboardEvent.preventDefault();
                    setSelectedEvent(selectedEvent === event.id ? null : event.id);
                  }
                }}
              >
                <span className="diagnostic-event-severity">{event.severity}</span>
                <span className="diagnostic-event-time">{new Date(event.last_seen).toLocaleString()}</span>
                <span className="diagnostic-event-main">
                  <strong>{event.message}</strong>
                  <small>{event.component}{event.symbol ? " · " + event.symbol : ""} · {event.error_code}</small>
                </span>
                <span className="diagnostic-event-count">{event.occurrence_count}×</span>
                {selectedEvent === event.id && (
                  <div className="diagnostic-event-detail">
                    <b>Component:</b> {event.component}<br />
                    <b>Service:</b> {event.service || "—"}<br />
                    <b>Status:</b> {event.http_status ?? "—"}<br />
                    <b>Provider code:</b> {event.provider_code || "—"}<br />
                    {advanced && <><b>Technical detail:</b><pre>{event.technical_detail || "No technical detail recorded."}</pre></>}
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="status-empty">{accountId ? "No diagnostic events recorded for this account." : "No diagnostic events recorded yet. Primary market-data errors will appear here."}</div>
        )}
      </div>
    </section>
  );

  return embedded ? content : <main className="status-screen">{content}</main>;
}
