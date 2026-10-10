import { Component, useEffect, useMemo, useRef, useState, type ErrorInfo, type PointerEvent as ReactPointerEvent, type ReactNode, type SetStateAction, type FormEvent } from "react";
import { apiFetch } from "./api";
import ReactDOM from "react-dom/client";
import { Chart, type ChartType, type Timeframe, type ChartRange, type DrawingTool, type PipscriptOutput, type ChartColors } from "./Chart";
import "./styles.css";
import { DevConsole } from "./DevConsole";
import { BrokerConnections } from "./BrokerConnections";
import { StatusCenter } from "./StatusCenter";
import { SecuritySettings } from "./SecuritySettings";
import { loginWithPasskey, passkeySupported, signupWithPasskey } from "./webauthn";

type WatchItem = { symbol: string; price: string; change: string; apiSymbol?: string };
export type ChartTheme = "pipsgox" | "classic" | "light";
type PipscriptLanguage = "python" | "javascript";
type PipscriptOutputType = "indicator" | "table";
type PipscriptScope = "symbol" | "multi-symbol" | "static";

type ActivePipscript = {
  language: PipscriptLanguage;
  outputType: PipscriptOutputType;
  code: string;
  scope: PipscriptScope;
};

type CachedPipscriptResult = {
  output: PipscriptOutput;
  cachedAt: number;
};

type PyodideRuntime = {
  runPythonAsync: (code: string) => Promise<unknown>;
};

declare global {
  interface Window {
    loadPyodide?: (options?: { indexURL?: string }) => Promise<PyodideRuntime>;
  }
}

let pyodidePromise: Promise<PyodideRuntime> | null = null;
let pyodideRuntime: PyodideRuntime | null = null;

function loadPyodideRuntime(): Promise<PyodideRuntime> {
  if (pyodideRuntime) return Promise.resolve(pyodideRuntime);
  if (pyodidePromise) return pyodidePromise;

  if (window.loadPyodide) {
    pyodidePromise = window.loadPyodide({
      indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/",
    }).then((runtime) => {
      pyodideRuntime = runtime;
      return runtime;
    });
    return pyodidePromise;
  }

  pyodidePromise = new Promise<PyodideRuntime>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>("script[data-pipsgox-pyodide]");
    if (existing) {
      existing.addEventListener("load", () => {
        if (!window.loadPyodide) reject(new Error("Pyodide loaded without loadPyodide()."));
        else window.loadPyodide({ indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/" }).then((runtime) => {
          pyodideRuntime = runtime;
          resolve(runtime);
        }).catch(reject);
      }, { once: true });
      existing.addEventListener("error", () => reject(new Error("Could not load the Python runtime.")), { once: true });
      return;
    }

    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.js";
    script.async = true;
    script.dataset.pipsgoxPyodide = "true";
    script.onload = () => {
      if (!window.loadPyodide) reject(new Error("Pyodide loaded without loadPyodide()."));
      else window.loadPyodide({ indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/" }).then((runtime) => {
        pyodideRuntime = runtime;
        resolve(runtime);
      }).catch(reject);
    };
    script.onerror = () => reject(new Error("Could not load the Python runtime."));
    document.head.appendChild(script);
  });

  return pyodidePromise;
}

function inferPipscriptScope(code: string): PipscriptScope {
  // A script without data_requests() receives the selected chart candles,
  // so it is inherently dependent on the current symbol.
  if (!/\bdata_requests\s*\(/.test(code)) return "symbol";

  // SYMBOL is injected by the runtime and is the standard way for a script
  // to declare that its data follows the currently selected chart symbol.
  if (/\bSYMBOL\b/.test(code)) return "symbol";

  // A data_requests() script with fixed symbols is multi-symbol. It should
  // not be re-executed just because the chart selection changes.
  return "multi-symbol";
}

function pipscriptCacheKey(
  language: PipscriptLanguage,
  outputType: PipscriptOutputType,
  code: string,
  symbolValue: string,
  timeframeValue: Timeframe,
): string {
  return JSON.stringify([language, outputType, code, symbolValue, timeframeValue]);
}

function normalizePipscriptOutput(raw: unknown, preferredType: PipscriptOutputType): PipscriptOutput {
  if (!raw || typeof raw !== "object") throw new Error("Script must return an object.");

  const value = raw as Record<string, unknown>;
  const outputType = value.type === "table" || value.type === "line" ? value.type : preferredType === "table" ? "table" : "line";

  if (outputType === "table") {
    const columns = Array.isArray(value.columns)
      ? value.columns.map((item) => String(item)).slice(0, 12)
      : [];
    const rows = Array.isArray(value.rows)
      ? value.rows
          .filter((row): row is unknown[] => Array.isArray(row))
          .slice(0, 50)
          .map((row) => row.slice(0, columns.length || 12).map((item) => String(item ?? "")))
      : [];

    if (!columns.length) throw new Error("Table output needs a non-empty columns array.");

    const rawLines = Array.isArray(value.lines) ? value.lines : [];
    const lines = rawLines
      .filter((line): line is Record<string, unknown> => Boolean(line) && typeof line === "object")
      .slice(0, 20)
      .map((line) => {
        const rawPoints = Array.isArray(line.points) ? line.points : [];
        const points = rawPoints
          .filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
          .map((point) => ({
            time: Number(point.time),
            value: Number(point.value),
          }))
          .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value));

        return {
          name: String(line.name || "PIPScript"),
          points,
        };
      })
      .filter((line) => line.points.length > 0);

    return {
      type: "table",
      title: String(value.title || "PIPScript Table"),
      columns,
      rows,
      lines,
    };
  }

  const rawPoints = Array.isArray(value.points)
    ? value.points
    : Array.isArray(value.values)
      ? value.values
      : [];

  const points = rawPoints
    .filter((point): point is Record<string, unknown> => Boolean(point) && typeof point === "object")
    .map((point) => ({
      time: Number(point.time),
      value: Number(point.value),
    }))
    .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value));

  if (!points.length) throw new Error("Indicator output needs points: [{ time, value }].");

  return {
    type: "line",
    name: String(value.name || "PIPScript"),
    points,
  };
}

type ChartSettings = {
  theme: ChartTheme;
  colors: ChartColors;
  showGrid: boolean;
  showCrosshair: boolean;
  showVolume: boolean;
  showMa44: boolean;
  showVwap: boolean;
  show52WeekHigh: boolean;
  show52WeekLow: boolean;
  showPreviousClose: boolean;
};

const DEFAULT_CHART_COLORS: ChartColors = {
  background: "#090909", grid: "#202020", axis: "#626b75",
  candleUp: "#12d98b", candleDown: "#ff4d5a",
  volumeUp: "#d9dde3", volumeDown: "#d9dde3",
  ma44: "#4dd0e1", ma50: "#f6c85f", ma200: "#b07cff",
};

const CHART_COLOR_PRESETS: Record<ChartTheme, ChartColors> = {
  pipsgox: DEFAULT_CHART_COLORS,
  classic: { background:"#101317", grid:"#28303a", axis:"#697482", candleUp:"#26a69a", candleDown:"#ef5350", volumeUp:"#7d8792", volumeDown:"#7d8792", ma44:"#4dd0e1", ma50:"#f6c85f", ma200:"#b07cff" },
  light: { background:"#ffffff", grid:"#e5e7eb", axis:"#9ca3af", candleUp:"#168a59", candleDown:"#c93643", volumeUp:"#8b95a1", volumeDown:"#8b95a1", ma44:"#0891b2", ma50:"#c27a00", ma200:"#7654a8" },
};

const DEFAULT_CHART_SETTINGS: ChartSettings = {
  theme: "pipsgox",
  colors: DEFAULT_CHART_COLORS,
  showGrid: true,
  showCrosshair: true,
  showVolume: true,
  showMa44: true,
  showVwap: false,
  show52WeekHigh: false,
  show52WeekLow: false,
  showPreviousClose: false,
};

function loadChartSettings(): ChartSettings {
  try {
    const raw = localStorage.getItem("pipsgox-chart-settings");
    if (!raw) return DEFAULT_CHART_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<ChartSettings>;
    return { ...DEFAULT_CHART_SETTINGS, ...parsed, colors: { ...DEFAULT_CHART_COLORS, ...(parsed.colors || {}) } };
  } catch {
    return DEFAULT_CHART_SETTINGS;
  }
}

const MAX_WATCHLIST_SIZE = 1000;
const WATCHLIST_STORAGE_KEY = "pipsgox-watchlists";
const LEGACY_WATCHLIST_STORAGE_KEY = "pipsgox-watchlist";
const DEFAULT_WATCHLIST_NAME = "Main";
const MAX_WATCHLISTS = 10;

const DEFAULT_WATCHLIST: WatchItem[] = [
  { symbol: "BHARTIARTL", price: "1,756.90", change: "+1.08%" },
  { symbol: "RELIANCE", price: "1,482.30", change: "+1.21%" },
  { symbol: "TCS", price: "4,021.50", change: "+0.64%" },
  { symbol: "INFY", price: "1,612.80", change: "-0.31%" },
  { symbol: "HDFCBANK", price: "1,008.40", change: "+0.82%" },
  { symbol: "TRENT", price: "5,214.20", change: "+2.14%" },
  { symbol: "ITC", price: "412.75", change: "-0.18%" },
  { symbol: "SBIN", price: "812.60", change: "+0.47%" },
  { symbol: "LT", price: "3,487.10", change: "+1.32%" },
  { symbol: "ICICIBANK", price: "1,214.90", change: "+0.63%" },
  { symbol: "KOTAKBANK", price: "1,981.65", change: "-0.22%" },
  { symbol: "AXISBANK", price: "1,104.30", change: "+0.56%" },
  { symbol: "M&M", price: "2,896.15", change: "+0.56%" },
  { symbol: "TATAMOTORS", price: "987.25", change: "-0.45%" },
  { symbol: "ADANIENT", price: "2,842.60", change: "+1.18%" },
  { symbol: "SUNPHARMA", price: "1,543.20", change: "-0.29%" },
  { symbol: "HINDUNILVR", price: "2,356.70", change: "+0.92%" },
  { symbol: "NIFTY", price: "24,198.85", change: "-0.12%" },
];

function formatWatchVolume(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (absolute >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (absolute >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return Math.round(value).toLocaleString("en-IN");
}

function formatWatchQuoteValue(value: number | null | undefined): string {
  return value != null && Number.isFinite(value) && value > 0 ? value.toFixed(2) : "—";
}

function normalizeWatchItems(value: unknown): WatchItem[] {
  if (!Array.isArray(value)) return [];

  return value
    .map((item) => {
      if (typeof item === "string") {
        return { symbol: item, price: "—", change: "—" };
      }
      const source = item as Partial<WatchItem>;
      return {
        symbol: String(source.symbol ?? "").trim().toUpperCase(),
        price: String(source.price ?? "—"),
        change: String(source.change ?? "—"),
        ...(source.apiSymbol ? { apiSymbol: String(source.apiSymbol).trim().toUpperCase() } : {}),
      };
    })
    .filter((item) => item.symbol)
    .slice(0, MAX_WATCHLIST_SIZE);
}

function loadWatchlists(): Record<string, WatchItem[]> {
  try {
    const raw = localStorage.getItem(WATCHLIST_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      const result: Record<string, WatchItem[]> = {};

      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        for (const [name, value] of Object.entries(parsed)) {
          const cleanName = name.trim().slice(0, 24);
          if (cleanName) result[cleanName] = normalizeWatchItems(value);
        }
      }

      if (Object.keys(result).length) return result;
    }

    const legacy = localStorage.getItem(LEGACY_WATCHLIST_STORAGE_KEY);
    const migrated = legacy ? normalizeWatchItems(JSON.parse(legacy)) : DEFAULT_WATCHLIST;
    return { [DEFAULT_WATCHLIST_NAME]: migrated };
  } catch {
    return { [DEFAULT_WATCHLIST_NAME]: DEFAULT_WATCHLIST };
  }
}

function normalizeImportedSymbol(value: string): string {
  let symbol = value
    .replace(/^\uFEFF/, "")
    .replace(/^["']|["']$/g, "")
    .trim()
    .toUpperCase();

  // Accept broker/FYERS CSV formats such as NSE:MBECL-EQ and MBECL-BE.
  // Keep an explicit supported series so the backend can request the exact
  // instrument instead of incorrectly appending -EQ again.
  if (symbol.startsWith("NSE:")) {
    symbol = symbol.slice(4);
  }
  const match = symbol.match(/^(.+)-(EQ|BE)$/);
  return match ? match[1] + "-" + match[2] : symbol;
}

function parseWatchlistImport(text: string): string[] {
  const rows = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const symbols: string[] = [];

  for (const row of rows) {
    const cells = row.split(/[,;\t]/).map((cell) => normalizeImportedSymbol(cell));
    if (!cells.length) continue;

    const header = cells[0].replace(/\s+/g, "").toLowerCase();
    if (header === "symbol" || header === "ticker" || header === "tradingsymbol") continue;

    // Broker exports often store the ticker and NSE series in separate
    // columns, e.g. "LOTUSDEV,BE". Preserve that series so BE-only stocks
    // reach the backend as LOTUSDEV-BE instead of being forced to -EQ.
    const tickerIndex = cells.findIndex(
      (cell) => cell && !/^(SYMBOL|TICKER|TRADINGSYMBOL)$/i.test(cell),
    );
    const candidate = tickerIndex >= 0 ? cells[tickerIndex] : "";
    const series = cells.slice(tickerIndex + 1).find((cell) => /^(EQ|BE)$/i.test(cell));
    if (candidate) {
      symbols.push(series ? `${candidate}-${series}` : candidate);
    }
  }

  return [...new Set(symbols)];
}

type SavedPipscript = {
  id: string;
  name: string;
  language: PipscriptLanguage;
  outputType: PipscriptOutputType;
  code: string;
  updatedAt: number;
};

const PIPSCRIPT_STORAGE_KEY = "pipsgox-pipscripts";
const LEGACY_PIPSCRIPT_STORAGE_KEY = "pipsgox-pipscript";

function loadSavedPipscripts(): SavedPipscript[] {
  try {
    const raw = localStorage.getItem(PIPSCRIPT_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (Array.isArray(parsed)) {
        return parsed.filter((item): item is SavedPipscript =>
          Boolean(item) &&
          typeof item === "object" &&
          typeof (item as SavedPipscript).id === "string" &&
          typeof (item as SavedPipscript).name === "string" &&
          ((item as SavedPipscript).language === "python" || (item as SavedPipscript).language === "javascript") &&
          ((item as SavedPipscript).outputType === "indicator" || (item as SavedPipscript).outputType === "table") &&
          typeof (item as SavedPipscript).code === "string"
        );
      }
    }

    // Migrate the previous single-script storage format.
    const legacyRaw = localStorage.getItem(LEGACY_PIPSCRIPT_STORAGE_KEY);
    if (legacyRaw) {
      const saved = JSON.parse(legacyRaw) as {
        language?: PipscriptLanguage;
        outputType?: PipscriptOutputType;
        code?: string;
      };
      if (
        (saved.language === "python" || saved.language === "javascript") &&
        (saved.outputType === "indicator" || saved.outputType === "table") &&
        typeof saved.code === "string"
      ) {
        const migrated: SavedPipscript = {
          id: "legacy-" + Date.now(),
          name: "My PIPScript",
          language: saved.language,
          outputType: saved.outputType,
          code: saved.code,
          updatedAt: Date.now(),
        };
        const list = [migrated];
        localStorage.setItem(PIPSCRIPT_STORAGE_KEY, JSON.stringify(list));
        return list;
      }
    }
  } catch {
    // Ignore invalid local storage and start clean.
  }
  return [];
}

type UiIconName = "candle" | "bar" | "line" | "indicator" | "pipscript" | "sun" | "settings" | "add" | "import" | "export" | "new" | "delete" | "hide";

function UiIcon({ name }: { name: UiIconName }) {
  const common = {
    width: 14,
    height: 14,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  if (name === "candle") {
    return (
      <svg {...common}>
        <path d="M7 3v4M7 17v4M7 7h0M5 7h4v10H5z" />
        <path d="M17 3v7M17 19v2M15 10h4v9h-4z" />
      </svg>
    );
  }

  if (name === "bar") {
    return (
      <svg {...common}>
        <path d="M6 4v16M4 7h4M4 17h4M15 7v10M13 9h4M13 15h4" />
      </svg>
    );
  }

  if (name === "line") {
    return (
      <svg {...common}>
        <path d="M4 17l5-5 4 3 7-8" />
        <path d="M17 7h3v3" />
      </svg>
    );
  }

  if (name === "indicator") {
    return (
      <svg {...common}>
        <path d="M5 19V9M12 19V5M19 19v-7" />
        <path d="M3 19h18" />
      </svg>
    );
  }

  if (name === "pipscript") {
    return (
      <svg {...common}>
        <path d="M8 6l-5 6 5 6M16 6l5 6-5 6M14 4l-4 16" />
      </svg>
    );
  }

  if (name === "add") {
    return (
      <svg {...common}>
        <path d="M12 5v14M5 12h14" />
      </svg>
    );
  }

  if (name === "import") {
    return (
      <svg {...common}>
        <path d="M12 4v11M8 11l4 4 4-4" />
        <path d="M5 19h14" />
      </svg>
    );
  }

  if (name === "export") {
    return (
      <svg {...common}>
        <path d="M12 20V9M8 13l4-4 4 4" />
        <path d="M5 5h14" />
      </svg>
    );
  }

  if (name === "new") {
    return (
      <svg {...common}>
        <path d="M6 4h9l3 3v13H6z" />
        <path d="M14 4v4h4M12 11v6M9 14h6" />
      </svg>
    );
  }

  if (name === "delete") {
    return (
      <svg {...common}>
        <path d="M5 7h14M9 7V4h6v3M8 10v8M12 10v8M16 10v8" />
        <path d="M6 7l1 14h10l1-14" />
      </svg>
    );
  }

  if (name === "hide") {
    return (
      <svg {...common}>
        <path d="M3.5 12s3.2-5 8.5-5 8.5 5 8.5 5-3.2 5-8.5 5-8.5-5-8.5-5z" />
        <path d="M5 5l14 14" />
      </svg>
    );
  }

  if (name === "sun") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="3.5" />
        <path d="M12 2.5v2M12 19.5v2M4.5 4.5l1.4 1.4M18.1 18.1l1.4 1.4M2.5 12h2M19.5 12h2M4.5 19.5l1.4-1.4M18.1 5.9l1.4-1.4" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <path d="M12 3.5l1.1 1.9 2.2.4.4 2.2 1.9 1.1-1.9 1.1-.4 2.2-2.2.4-1.1 1.9-1.1-1.9-2.2-.4-.4-2.2-1.9-1.1 1.9-1.1.4-2.2 2.2-.4z" />
      <circle cx="12" cy="9.1" r="2.1" />
      <path d="M5.5 20.5h13" />
    </svg>
  );
}

function DrawingIcon({ name }: { name: DrawingTool | "delete" | "clear" }) {
  const common = {
    width: 15,
    height: 15,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  if (name === "horizontalRay") return <svg {...common}><path d="M3 12h17" /><path d="M17 8l4 4-4 4" /></svg>;
  if (name === "trendline") return <svg {...common}><path d="M4 18L20 6" /><path d="M17 6h3v3" /></svg>;
  if (name === "rectangle") return <svg {...common}><rect x="4" y="6" width="16" height="12" rx="1" /></svg>;
  if (name === "long") return <svg {...common}><path d="M5 18L18 5" /><path d="M12 5h6v6" /><path d="M5 18h6" /></svg>;
  if (name === "short") return <svg {...common}><path d="M5 6l13 13" /><path d="M12 19h6v-6" /><path d="M5 6h6" /></svg>;
  if (name === "arrow") return <svg {...common}><path d="M4 20L19 5" /><path d="M11 5h8v8" /></svg>;
  if (name === "brush") return <svg {...common}><path d="M14 5l5 5-9.5 9.5H5V15z" /><path d="M13 6l5 5" /><path d="M5 20c1.5-2 3-2 4 0" /></svg>;
  if (name === "text") return <svg {...common}><path d="M5 5h14M12 5v14M8 19h8" /></svg>;
  if (name === "delete") return <svg {...common}><path d="M4 7h16M9 7V4h6v3M7 7l1 14h8l1-14M10 11v6M14 11v6" /></svg>;
  return <svg {...common}><path d="M6 6l12 12M18 6L6 18" /><rect x="3.5" y="3.5" width="17" height="17" rx="2" /></svg>;
}

function initialPasswordResetToken(): string {
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const query = new URLSearchParams(window.location.search);
  return hash.get("reset_token") || query.get("reset_token") || "";
}

function App() {
  const [authReady, setAuthReady] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [appReady, setAppReady] = useState(false);
  const [authMode, setAuthMode] = useState<"login" | "signup">("login");
  const [authUsername, setAuthUsername] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authConfirmPassword, setAuthConfirmPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [signupWithPasskeyMode, setSignupWithPasskeyMode] = useState(false);
  const [totpChallengeId, setTotpChallengeId] = useState("");
  const [totpChallengeExpiresAt, setTotpChallengeExpiresAt] = useState<number | null>(null);
  const [totpChallengeSecondsLeft, setTotpChallengeSecondsLeft] = useState<number | null>(null);
  const [totpCode, setTotpCode] = useState("");
  const [totpUsingRecoveryCode, setTotpUsingRecoveryCode] = useState(false);
  const [passwordResetMode, setPasswordResetMode] = useState<"login" | "request" | "sent" | "reset">(
    () => initialPasswordResetToken() ? "reset" : "login",
  );
  const [passwordResetEmail, setPasswordResetEmail] = useState("");
  const [passwordResetToken, setPasswordResetToken] = useState(
    () => initialPasswordResetToken(),
  );
  const [passwordResetPassword, setPasswordResetPassword] = useState("");
  const [passwordResetConfirmPassword, setPasswordResetConfirmPassword] = useState("");
  const [passwordResetMessage, setPasswordResetMessage] = useState("");

  useEffect(() => {
    if (!totpChallengeId || !totpChallengeExpiresAt) {
      setTotpChallengeSecondsLeft(null);
      return;
    }

    const updateCountdown = () => {
      const secondsLeft = Math.max(
        0,
        Math.ceil((totpChallengeExpiresAt * 1000 - Date.now()) / 1000),
      );
      setTotpChallengeSecondsLeft(secondsLeft);

      if (secondsLeft === 0) {
        setTotpChallengeId("");
        setTotpChallengeExpiresAt(null);
        setTotpCode("");
        setTotpUsingRecoveryCode(false);
        setAuthError("Verification session expired. Please sign in again.");
      }
    };

    updateCountdown();
    const timer = window.setInterval(updateCountdown, 1000);
    return () => window.clearInterval(timer);
  }, [totpChallengeId, totpChallengeExpiresAt]);

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/auth/status")
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload?.detail || "Authentication check failed.");
        return payload;
      })
      .then((payload) => {
        if (cancelled) return;
        setAuthenticated(Boolean(payload.authenticated));
        setAuthReady(true);
      })
      .catch((error) => {
        if (cancelled) return;
        setAuthError(error instanceof Error ? error.message : "Authentication check failed.");
        setAuthReady(true);
      });
    return () => { cancelled = true; };
  }, []);

  const submitAuth = async (event: FormEvent) => {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError("");

    try {
      if (authMode === "signup" && authPassword !== authConfirmPassword) {
        throw new Error("Passwords do not match.");
      }

      const endpoint = authMode === "signup" ? "/api/auth/signup" : "/api/auth/login";
      const response = await apiFetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: authUsername.trim(), password: authPassword }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          payload?.detail ||
          (authMode === "signup" ? "Could not create the PIPSGOX account." : "Authentication failed.")
        );
      }

      if (authMode === "login" && payload?.totp_required && payload?.challenge_id) {
        setTotpChallengeId(String(payload.challenge_id));
        setTotpChallengeExpiresAt(Number(payload.challenge_expires_at || 0) || null);
        setTotpCode("");
        setAuthPassword("");
        return;
      }

      setAuthenticated(true);
      setAppReady(false);
      setAuthPassword("");
      setAuthConfirmPassword("");
      setTotpChallengeId("");
      setTotpChallengeExpiresAt(null);
      setTotpCode("");
    } catch (error) {
      setAuthError(
        error instanceof Error
          ? error.message
          : (authMode === "signup" ? "Could not create the PIPSGOX account." : "Authentication failed.")
      );
    } finally {
      setAuthBusy(false);
    }
  };

  const requestPasswordReset = async (event: FormEvent) => {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError("");
    setPasswordResetMessage("");

    try {
      const response = await apiFetch("/api/auth/password-reset/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: passwordResetEmail.trim() }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.detail || "Could not start password reset.");
      }
      setPasswordResetMode("sent");
      setPasswordResetMessage(
        "If an account exists for that email, password reset instructions will be sent. Check your inbox.",
      );
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Could not start password reset.");
    } finally {
      setAuthBusy(false);
    }
  };

  const confirmPasswordReset = async (event: FormEvent) => {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError("");
    setPasswordResetMessage("");

    try {
      if (passwordResetPassword !== passwordResetConfirmPassword) {
        throw new Error("Passwords do not match.");
      }
      if (passwordResetPassword.length < 12) {
        throw new Error("Password must be at least 12 characters.");
      }
      if (!passwordResetToken) {
        throw new Error("Password reset link is missing. Request a new link.");
      }

      const response = await apiFetch("/api/auth/password-reset/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token: passwordResetToken,
          password: passwordResetPassword,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.detail || "Could not reset the password.");
      }

      setPasswordResetPassword("");
      setPasswordResetConfirmPassword("");
      setPasswordResetToken("");
      setPasswordResetMode("sent");
      setPasswordResetMessage("Your password was reset successfully. Sign in with your new password.");
      window.history.replaceState({}, document.title, window.location.pathname);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Could not reset the password.");
    } finally {
      setAuthBusy(false);
    }
  };

  const returnToSignIn = () => {
    setPasswordResetMode("request");
    setPasswordResetEmail("");
    setPasswordResetToken("");
    setPasswordResetPassword("");
    setPasswordResetConfirmPassword("");
    setPasswordResetMessage("");
    setAuthError("");
    setAuthMode("login");
    window.history.replaceState({}, document.title, window.location.pathname);
  };

  const submitTotpLogin = async (event: FormEvent) => {
    event.preventDefault();
    if (!totpChallengeId) {
      setAuthError("Authentication challenge is missing. Sign in again.");
      return;
    }

    const code = totpCode.replace(/\s+/g, "");
    if (totpUsingRecoveryCode ? !/^[A-Za-z0-9-]{8,20}$/.test(code) : !/^\d{6}$/.test(code)) {
      setAuthError(totpUsingRecoveryCode ? "Enter a valid recovery code." : "Enter the current 6-digit authenticator code.");
      return;
    }

    setAuthBusy(true);
    setAuthError("");
    try {
      const endpoint = totpUsingRecoveryCode
        ? "/api/auth/login/recovery"
        : "/api/auth/login/totp";
      const response = await apiFetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ challenge_id: totpChallengeId, code }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          String(payload?.detail || (totpUsingRecoveryCode ? "Recovery code verification failed." : "Authenticator verification failed."))
        );
      }
      if (!payload?.authenticated) throw new Error("Verification did not establish a session.");

      setAuthenticated(true);
      setAppReady(false);
      setAuthPassword("");
      setAuthConfirmPassword("");
      setTotpChallengeId("");
      setTotpChallengeExpiresAt(null);
      setTotpCode("");
      setTotpUsingRecoveryCode(false);
    } catch (error) {
      const message = error instanceof Error
        ? error.message
        : (totpUsingRecoveryCode ? "Recovery code verification failed." : "Authenticator verification failed.");

      if (/authentication challenge expired|verification session expired/i.test(message)) {
        setTotpChallengeId("");
        setTotpChallengeExpiresAt(null);
        setTotpChallengeSecondsLeft(null);
        setTotpCode("");
        setTotpUsingRecoveryCode(false);
        setAuthError("Verification session expired. Please sign in again.");
      } else {
        setAuthError(message);
      }
    } finally {
      setAuthBusy(false);
    }
  };

  const createAccountWithPasskey = async () => {
    const username = authUsername.trim();
    if (username.length < 3 || username.length > 64 || /\s/.test(username)) {
      setAuthError("Enter a username between 3 and 64 characters with no spaces.");
      return;
    }
    setAuthBusy(true);
    setAuthError("");
    try {
      const payload = await signupWithPasskey(username);
      if (!payload?.authenticated) throw new Error("Passkey signup did not establish a session.");
      setAuthenticated(true);
      setAppReady(false);
      setAuthPassword("");
      setAuthConfirmPassword("");
      setSignupWithPasskeyMode(false);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Passkey account creation failed.");
    } finally {
      setAuthBusy(false);
    }
  };

  const signInWithPasskey = async () => {
    setAuthBusy(true);
    setAuthError("");
    try {
      const payload = await loginWithPasskey();
      if (!payload?.authenticated) throw new Error("Passkey sign-in did not establish a session.");
      setAuthenticated(true);
      setAppReady(false);
      setAuthPassword("");
      setAuthConfirmPassword("");
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Passkey sign-in failed.");
    } finally {
      setAuthBusy(false);
    }
  };

  const logout = async () => {
    setAuthBusy(true);
    setAuthError("");

    try {
      const response = await apiFetch("/api/auth/logout", { method: "POST" });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload?.detail || "Could not sign out.");
      }

      // The server has revoked the session and cleared the HttpOnly cookie.
      // Reset the UI immediately without requiring a browser refresh.
      setAuthenticated(false);
      setAppReady(false);
      setBrokerAccounts([]);
      setSelectedAccountId(null);
      setAuthUsername("");
      setAuthPassword("");
      setAuthConfirmPassword("");
    } catch (error) {
      // Keep the terminal closed even if the request itself fails; the
      // timeout prevents an indefinite "logging out" state.
      setAuthenticated(false);
      setAppReady(false);
      setBrokerAccounts([]);
      setSelectedAccountId(null);
      setAuthError(error instanceof Error ? error.message : "Could not sign out.");
    } finally {
      setAuthBusy(false);
    }
  };

  const disconnectAllBrokers = async () => {
    if (!window.confirm("Disconnect every broker account and invalidate all stored broker access tokens?")) return;
    const response = await apiFetch("/api/security/disconnect-all", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload?.detail || "Could not disconnect broker accounts.");
    setBrokerAccounts((accounts) => accounts.map((account) => ({ ...account, status: "disconnected" })));
    setSelectedAccountId(null);
  };

  const [chartType, setChartType] = useState<ChartType>("candles");
  const [timeframe, setTimeframe] = useState<Timeframe>("D");
  const [chartRange, setChartRange] = useState<ChartRange>("6M");
  const [drawingToolbarOpen, setDrawingToolbarOpen] = useState(false);
  const [drawingToolbarPosition, setDrawingToolbarPosition] = useState(() => {
    try {
      const saved = localStorage.getItem("pipsgox-drawing-toolbar-position");
      if (saved) {
        const parsed = JSON.parse(saved) as { x?: unknown; y?: unknown };
        if (Number.isFinite(Number(parsed.x)) && Number.isFinite(Number(parsed.y))) {
          return { x: Number(parsed.x), y: Number(parsed.y) };
        }
      }
    } catch {
      // Ignore invalid saved toolbar position.
    }
    return { x: 10, y: 10 };
  });
  const drawingToolbarDragRef = useRef<{ offsetX: number; offsetY: number } | null>(null);
  const [activeDrawingTool, setActiveDrawingTool] = useState<DrawingTool | null>(null);
  const [drawingCommand, setDrawingCommand] = useState<{ type: "delete" | "clear"; nonce: number } | undefined>();

  const chartRangeTimeframes: Record<ChartRange, Timeframe> = {
    "1D": "5m",
    "5D": "15m",
    "1M": "1h",
    "3M": "1h",
    "6M": "D",
    YTD: "D",
    "1Y": "W",
    "5Y": "M",
    ALL: "M",
  };

  const selectChartRange = (preset: ChartRange) => {
    setChartRange(preset);
    setTimeframe(chartRangeTimeframes[preset]);
  };

  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(() => {
    const queryAccount = new URLSearchParams(window.location.search).get("account_id");
    const queryValue = queryAccount ? Number(queryAccount) : NaN;
    if (Number.isInteger(queryValue) && queryValue > 0) return queryValue;
    const raw = localStorage.getItem("pipsgox-selected-account");
    const value = raw ? Number(raw) : NaN;
    return Number.isInteger(value) && value > 0 ? value : null;
  });

  useEffect(() => {
    if (!authenticated) {
      setAppReady(false);
      return;
    }
    // Authentication alone is sufficient to open PIPSGOX.
    // Broker accounts are optional and must never block application startup.
    setAppReady(true);

    let cancelled = false;
    apiFetch("/api/broker/accounts")
      .then(async (response) => {
        const payload = await response.json().catch(() => []);
        if (!response.ok) throw new Error(payload?.detail || "Could not load broker accounts.");
        return Array.isArray(payload) ? payload : [];
      })
      .then((accounts) => {
        if (cancelled) return;
        setBrokerAccounts(accounts);
        setSelectedAccountId((current) => {
          if (current && accounts.some((account) => account.id === current)) return current;
          const connected = accounts.find((account) => account.status === "connected");
          return connected?.id ?? null;
        });
      })
      .catch(() => {
        if (!cancelled) setBrokerAccounts([]);
      });
    return () => { cancelled = true; };
  }, [authenticated]);

  const watchPanelRef = useRef<"watchlist" | "44ma" | "movers">("watchlist");
  const ma44ModeRef = useRef<"live" | "eod">("live");

  useEffect(() => {
    if (!authenticated) return;
    let cancelled = false;
    const load = async () => {
      if (watchPanelRef.current !== "44ma") return;
      try {
        const response = await apiFetch(
          `/api/scanner/44ma?mode=${ma44ModeRef.current}`,
          { cache: "no-store" },
        );
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(String(payload.detail || "44 MA scanner status failed."));
        if (!cancelled) setMa44Scan(payload);
      } catch (error) {
        if (!cancelled) console.warn(error);
      }
    };
    void load();
    const timer = window.setInterval(load, 10000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [authenticated]);

  useEffect(() => {
    if (selectedAccountId) localStorage.setItem("pipsgox-selected-account", String(selectedAccountId));
    else localStorage.removeItem("pipsgox-selected-account");
  }, [selectedAccountId]);

  useEffect(() => {
    try {
      localStorage.setItem("pipsgox-drawing-toolbar-position", JSON.stringify(drawingToolbarPosition));
    } catch {
      // Ignore storage failures.
    }
  }, [drawingToolbarPosition]);

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      const drag = drawingToolbarDragRef.current;
      const container = document.querySelector<HTMLElement>(".chart-container");
      const toolbar = document.querySelector<HTMLElement>(".drawing-toolbar");
      if (!drag || !container || !toolbar) return;

      const rect = container.getBoundingClientRect();
      const maxX = Math.max(0, rect.width - toolbar.offsetWidth);
      const maxY = Math.max(0, rect.height - toolbar.offsetHeight);
      const x = Math.min(Math.max(0, event.clientX - rect.left - drag.offsetX), maxX);
      const y = Math.min(Math.max(0, event.clientY - rect.top - drag.offsetY), maxY);

      setDrawingToolbarPosition({ x, y });
    };

    const handlePointerUp = () => {
      drawingToolbarDragRef.current = null;
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, []);

  const startDrawingToolbarDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest("button")) return;

    const toolbar = event.currentTarget;
    const container = toolbar.closest<HTMLElement>(".chart-container");
    if (!container) return;

    const toolbarRect = toolbar.getBoundingClientRect();
    drawingToolbarDragRef.current = {
      offsetX: event.clientX - toolbarRect.left,
      offsetY: event.clientY - toolbarRect.top,
    };
    event.preventDefault();
  };

  const [watchlists, setWatchlists] = useState<Record<string, WatchItem[]>>(loadWatchlists);
  const [activeWatchlistName, setActiveWatchlistName] = useState(() => Object.keys(loadWatchlists())[0] ?? DEFAULT_WATCHLIST_NAME);
  const watchlist = watchlists[activeWatchlistName] ?? [];
  const setWatchlist = (update: SetStateAction<WatchItem[]>) => {
    setWatchlists((current) => {
      const currentList = current[activeWatchlistName] ?? [];
      const nextList = typeof update === "function" ? update(currentList) : update;
      return { ...current, [activeWatchlistName]: nextList };
    });
  };
  const [chartApiSymbol, setChartApiSymbol] = useState<string | null>(null);
  const [symbol, setSymbol] = useState(() => {
    const initialLists = loadWatchlists();
    const initial = Object.values(initialLists)[0] ?? DEFAULT_WATCHLIST;
    return initial[0]?.symbol ?? "BHARTIARTL";
  });
  const [search, setSearch] = useState("");
  const [searchResults, setSearchResults] = useState<Array<{
    symbol: string;
    name: string;
    exchange: string;
    api_symbol: string;
  }>>([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [watchSearch, setWatchSearch] = useState("");
  const [watchOpen, setWatchOpen] = useState(true);
  const [watchPanel, setWatchPanel] = useState<"watchlist" | "44ma" | "movers">("watchlist");
  const [ma44Mode, setMa44Mode] = useState<"live" | "eod">("live");
  const [ma44Search, setMa44Search] = useState("");
  const [ma44Scan, setMa44Scan] = useState<{
    status: string; stage?: string; message: string; error: string;
    progress?: number;
    universe_count: number; eligible_trend_count: number;
    processed: number; total: number; last_scan: number | null;
    results: Array<{ symbol: string; closed: number; change_percent: number; ma_distance: number; sma44: number; high?: number; low?: number }>;
  } | null>(null);
  watchPanelRef.current = watchPanel;
  ma44ModeRef.current = ma44Mode;
  const [watchWidth, setWatchWidth] = useState(315);
  const [dark, setDark] = useState(true);
  const [panel, setPanel] = useState<"indicators" | "pipscript" | "settings" | null>(null);
  const [accountOpen, setAccountOpen] = useState(false);
  const [securityOpen, setSecurityOpen] = useState(false);
  const [statusOpen, setStatusOpen] = useState(false);
  const [orderOpen, setOrderOpen] = useState(false);
  const [orderSide, setOrderSide] = useState<"BUY" | "SELL">("BUY");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT" | "STOP_LOSS" | "STOP_LOSS_MARKET">("MARKET");
  const [orderProduct, setOrderProduct] = useState<"CNC" | "INTRADAY" | "MARGIN" | "MTF">("INTRADAY");
  const [orderQuantity, setOrderQuantity] = useState("1");
  const [orderPrice, setOrderPrice] = useState("");
  const [orderTriggerPrice, setOrderTriggerPrice] = useState("");
  const [orderSubmitting, setOrderSubmitting] = useState(false);
  const [orderMessage, setOrderMessage] = useState("");

  const [accountFundsOpen, setAccountFundsOpen] = useState(false);
  const [accountFunds, setAccountFunds] = useState<Record<string, number | string> | null>(null);
  const [accountFundsLoading, setAccountFundsLoading] = useState(false);
  const [accountFundsError, setAccountFundsError] = useState("");
  const [positionsOpen, setPositionsOpen] = useState(false);
  const [positions, setPositions] = useState<Array<Record<string, unknown>>>([]);
  const [positionsLoading, setPositionsLoading] = useState(false);
  const [positionsError, setPositionsError] = useState("");
  const [holdingsOpen, setHoldingsOpen] = useState(false);
  const [holdings, setHoldings] = useState<Array<Record<string, unknown>>>([]);
  const [holdingsLoading, setHoldingsLoading] = useState(false);
  const [holdingsError, setHoldingsError] = useState("");
  const [ordersOpen, setOrdersOpen] = useState(false);
  const [ordersTab, setOrdersTab] = useState<"orders" | "trades">("orders");
  const [orders, setOrders] = useState<Array<Record<string, unknown>>>([]);
  const [trades, setTrades] = useState<Array<Record<string, unknown>>>([]);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [ordersError, setOrdersError] = useState("");
  const [cancellingOrderId, setCancellingOrderId] = useState<string | null>(null);
  const [brokerAccounts, setBrokerAccounts] = useState<Array<{
    id: number;
    broker: string;
    account_name: string;
    client_id: string;
    status: string;
  }>>([]);

  const [quote, setQuote] = useState<{
    last: number; change: number; change_percent: number;
    open?: number; high?: number; low?: number; volume?: number;
    bid?: number; ask?: number;
  } | null>(null);
  const [liveQuotes, setLiveQuotes] = useState<Record<string, {
    last: number;
    change: number;
    change_percent: number;
    volume?: number;
    bid?: number;
    ask?: number;
  }>>({});
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteError, setQuoteError] = useState(false);
  const [chartSettings, setChartSettings] = useState<ChartSettings>(loadChartSettings);
  const [savedChartThemes, setSavedChartThemes] = useState<Record<string, ChartColors>>(() => {
    try { return JSON.parse(localStorage.getItem("pipsgox-chart-themes") || "{}") as Record<string, ChartColors>; } catch { return {}; }
  });
  const [chartThemeName, setChartThemeName] = useState("");
  const [watchImportMessage, setWatchImportMessage] = useState("");
  const [watchDialog, setWatchDialog] = useState<{ type: "add" | "new" | "delete" | "export" | "import"; value: string } | null>(null);
  const [draggedSymbol, setDraggedSymbol] = useState<string | null>(null);
  const watchImportRef = useRef<HTMLInputElement>(null);
  const applyChartPreset = (theme: ChartTheme) => updateChartSettings({ theme, colors: { ...CHART_COLOR_PRESETS[theme] } });
  const updateChartColor = (key: keyof ChartColors, value: string) => updateChartSettings({ colors: { ...chartSettings.colors, [key]: value } });
  const saveChartTheme = () => {
    const name = chartThemeName.trim().slice(0, 24);
    if (!name) return;
    const next = { ...savedChartThemes, [name]: { ...chartSettings.colors } };
    setSavedChartThemes(next);
    localStorage.setItem("pipsgox-chart-themes", JSON.stringify(next));
    setChartThemeName("");
  };
  const loadChartTheme = (name: string) => {
    if (!name) return;
    const colors = savedChartThemes[name];
    if (colors) updateChartSettings({ colors: { ...DEFAULT_CHART_COLORS, ...colors } });
  };

  const loadAccountFunds = async () => {
    if (!selectedAccountId) {
      setAccountFundsError("Select a connected account first.");
      return;
    }
    setAccountFundsLoading(true);
    setAccountFundsError("");
    try {
      const response = await apiFetch("/api/broker/accounts/" + selectedAccountId + "/funds", { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload.detail || "Could not load funds."));
      const raw = payload.data?.data ?? payload.data ?? {};
      setAccountFunds(raw && typeof raw === "object" ? raw as Record<string, number | string> : null);
      setAccountFundsOpen(true);
    } catch (error) {
      setAccountFunds(null);
      setAccountFundsError(error instanceof Error ? error.message : "Could not load funds.");
      setAccountFundsOpen(true);
    } finally {
      setAccountFundsLoading(false);
    }
  };

  const fundNumber = (keys: string[]) => {
    if (!accountFunds) return null;
    for (const key of keys) {
      const value = Number(accountFunds[key]);
      if (Number.isFinite(value)) return value;
    }
    return null;
  };

  const loadPositions = async () => {
    if (!selectedAccountId) {
      setPositionsError("Select a connected account first.");
      setPositionsOpen(true);
      return;
    }
    setPositionsLoading(true);
    setPositionsError("");
    try {
      const response = await apiFetch("/api/broker/accounts/" + selectedAccountId + "/positions", { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload.detail || "Could not load positions."));
      const data = Array.isArray(payload.data) ? payload.data : [];
      setPositions(data as Array<Record<string, unknown>>);
      setPositionsOpen(true);
    } catch (error) {
      setPositions([]);
      setPositionsError(error instanceof Error ? error.message : "Could not load positions.");
      setPositionsOpen(true);
    } finally {
      setPositionsLoading(false);
    }
  };

  const loadHoldings = async () => {
    if (!selectedAccountId) {
      setHoldingsError("Select a connected account first.");
      setHoldingsOpen(true);
      return;
    }
    setHoldingsLoading(true);
    setHoldingsError("");
    try {
      const response = await apiFetch("/api/broker/accounts/" + selectedAccountId + "/holdings", { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload.detail || "Could not load holdings."));
      const data = Array.isArray(payload.data) ? payload.data : [];
      setHoldings(data as Array<Record<string, unknown>>);
      setHoldingsOpen(true);
    } catch (error) {
      setHoldings([]);
      setHoldingsError(error instanceof Error ? error.message : "Could not load holdings.");
      setHoldingsOpen(true);
    } finally {
      setHoldingsLoading(false);
    }
  };

  const loadOrdersAndTrades = async (tab: "orders" | "trades" = ordersTab) => {
    if (!selectedAccountId) {
      setOrdersError("Select a connected account first.");
      setOrdersOpen(true);
      return;
    }
    setOrdersLoading(true);
    setOrdersError("");
    try {
      const endpoint = tab === "trades" ? "trades" : "orders";
      const response = await apiFetch("/api/broker/accounts/" + selectedAccountId + "/" + endpoint, { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload.detail || "Could not load " + endpoint + "."));
      const data = Array.isArray(payload.data) ? payload.data : [];
      if (tab === "trades") setTrades(data as Array<Record<string, unknown>>);
      else setOrders(data as Array<Record<string, unknown>>);
      setOrdersTab(tab);
      setOrdersOpen(true);
    } catch (error) {
      setOrdersError(error instanceof Error ? error.message : "Could not load " + tab + ".");
      setOrdersOpen(true);
    } finally {
      setOrdersLoading(false);
    }
  };

  const cancelBrokerOrder = async (orderId: string) => {
    if (!selectedAccountId || !window.confirm("Cancel order " + orderId + "?")) return;
    setCancellingOrderId(orderId);
    setOrdersError("");
    try {
      const response = await apiFetch("/api/broker/accounts/" + selectedAccountId + "/orders/" + encodeURIComponent(orderId), { method: "DELETE" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload.detail || "Could not cancel order."));
      await loadOrdersAndTrades("orders");
    } catch (error) {
      setOrdersError(error instanceof Error ? error.message : "Could not cancel order.");
    } finally {
      setCancellingOrderId(null);
    }
  };

  const positionNumber = (value: unknown) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  };

  const submitOrder = async () => {
    if (!selectedAccountId) { setOrderMessage("Select a connected trading account first."); return; }
    const quantity = Number(orderQuantity);
    const price = orderPrice ? Number(orderPrice) : undefined;
    const triggerPrice = orderTriggerPrice ? Number(orderTriggerPrice) : undefined;
    if (!Number.isInteger(quantity) || quantity <= 0) { setOrderMessage("Quantity must be a positive whole number."); return; }
    if (orderType === "LIMIT" && (!price || price <= 0)) { setOrderMessage("Enter a valid limit price."); return; }
    if ((orderType === "STOP_LOSS" || orderType === "STOP_LOSS_MARKET") && (!triggerPrice || triggerPrice <= 0)) { setOrderMessage("Enter a valid trigger price."); return; }
    setOrderSubmitting(true); setOrderMessage("");
    try {
      const response = await apiFetch("/api/broker/accounts/" + selectedAccountId + "/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol, side: orderSide, quantity, order_type: orderType, product_type: orderProduct, validity: "DAY", price, trigger_price: triggerPrice, correlation_id: crypto.randomUUID() }) });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload.detail || "Order request failed."));
      const orderId = payload?.data?.orderId || payload?.data?.id || payload?.data?.order_id || "submitted";
      setOrderMessage("Order " + orderId + " submitted.");
    } catch (error) { setOrderMessage(error instanceof Error ? error.message : "Order request failed."); }
    finally { setOrderSubmitting(false); }
  };
  const updateChartSettings = (patch: Partial<ChartSettings>) => {
    setChartSettings((current) => {
      const next = { ...current, ...patch };
      localStorage.setItem("pipsgox-chart-settings", JSON.stringify(next));
      return next;
    });
  };

  useEffect(() => {
    localStorage.setItem(WATCHLIST_STORAGE_KEY, JSON.stringify(watchlists));
  }, [watchlists]);

  const exportWatchlist = () => {
    const csv = ["symbol", ...watchlist.map((item) => item.symbol)].join("\n") + "\n";
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "pipsgox-watchlist.csv";
    link.click();
    URL.revokeObjectURL(url);
    setWatchDialog(null);
    setWatchImportMessage(`Exported ${watchlist.length} symbols`);
    window.setTimeout(() => setWatchImportMessage(""), 2500);
  };

  const importWatchlist = async (file: File) => {
    try {
      const text = await file.text();
      const imported = parseWatchlistImport(text);

      if (!imported.length) {
        setWatchImportMessage("No symbols found in the file");
        return;
      }

      const limited = imported.slice(0, MAX_WATCHLIST_SIZE);
      const next = limited.map((item) => ({ symbol: item, price: "—", change: "—" }));
      setWatchlist(next);

      if (!next.some((item) => item.symbol === symbol)) {
        selectSymbol(next[0].symbol);
      }

      setWatchImportMessage(
        imported.length > MAX_WATCHLIST_SIZE
          ? `Imported ${MAX_WATCHLIST_SIZE} symbols (1000 maximum)`
          : `Imported ${next.length} symbols`,
      );
    } catch {
      setWatchImportMessage("Could not read the watchlist file");
    } finally {
      if (watchImportRef.current) watchImportRef.current.value = "";
      window.setTimeout(() => setWatchImportMessage(""), 3500);
    }
  };

  const createWatchlist = (rawName?: string) => {
    if (Object.keys(watchlists).length >= MAX_WATCHLISTS) {
      setWatchDialog(null);
      setWatchImportMessage(`Maximum ${MAX_WATCHLISTS} watchlists`);
      window.setTimeout(() => setWatchImportMessage(""), 2500);
      return;
    }

    const name = String(rawName ?? "").trim().replace(/\s+/g, " ").slice(0, 24);
    if (!name) return;

    if (watchlists[name]) {
      setActiveWatchlistName(name);
      setWatchDialog(null);
      return;
    }

    setWatchlists((current) => ({ ...current, [name]: [] }));
    setActiveWatchlistName(name);
    setChartApiSymbol(null);
    setSymbol("BHARTIARTL");
    setSearch("BHARTIARTL");
    setSearchOpen(false);
    setWatchDialog(null);
  };

  const deleteWatchlist = () => {
    const names = Object.keys(watchlists);
    if (names.length <= 1) {
      setWatchDialog(null);
      setWatchImportMessage("Keep at least one watchlist");
      window.setTimeout(() => setWatchImportMessage(""), 2500);
      return;
    }

    const next = { ...watchlists };
    delete next[activeWatchlistName];
    const nextName = Object.keys(next)[0];
    setWatchlists(next);
    setActiveWatchlistName(nextName);
    const nextItem = next[nextName]?.[0];
    if (nextItem) selectSymbol(nextItem.symbol, nextItem.apiSymbol);
    else {
      setChartApiSymbol(null);
      setSymbol("BHARTIARTL");
      setSearch("");
    }
    setWatchDialog(null);
  };

  const removeWatchSymbol = (itemSymbol: string) => {
    setWatchlist((current) => current.filter((item) => item.symbol !== itemSymbol));
    if (symbol === itemSymbol) {
      const remaining = watchlist.filter((item) => item.symbol !== itemSymbol);
      const next = remaining[0]?.symbol ?? "BHARTIARTL";
      const nextItem = remaining[0];
      if (nextItem) selectSymbol(nextItem.symbol, nextItem.apiSymbol);
      else {
        setChartApiSymbol(null);
        setSymbol("BHARTIARTL");
        setSearch("BHARTIARTL");
      }
    }
  };

  const dropWatchSymbol = (targetSymbol: string) => {
    if (!draggedSymbol || draggedSymbol === targetSymbol) return;

    setWatchlist((current) => {
      const from = current.findIndex((item) => item.symbol === draggedSymbol);
      const to = current.findIndex((item) => item.symbol === targetSymbol);
      if (from < 0 || to < 0) return current;

      const next = [...current];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });

    setDraggedSymbol(null);
  };

  const addWatchSymbol = (rawSymbol?: string) => {
    const nextSymbol = normalizeImportedSymbol(rawSymbol ?? "");
    if (!nextSymbol) return;

    if (watchlist.some((item) => item.symbol === nextSymbol)) {
      selectSymbol(nextSymbol);
      setWatchDialog(null);
      setWatchImportMessage(`${nextSymbol} is already in the watchlist`);
      window.setTimeout(() => setWatchImportMessage(""), 2500);
      return;
    }

    if (watchlist.length >= MAX_WATCHLIST_SIZE) {
      setWatchDialog(null);
      setWatchImportMessage("Watchlist limit reached: 1000 symbols");
      window.setTimeout(() => setWatchImportMessage(""), 2500);
      return;
    }

    setWatchlist((current) => [...current, { symbol: nextSymbol, price: "—", change: "—" }]);
    selectSymbol(nextSymbol);
    setWatchDialog(null);
    setWatchImportMessage(`Added ${nextSymbol}`);
    window.setTimeout(() => setWatchImportMessage(""), 2500);
  };

  const [pipscriptLanguage, setPipscriptLanguage] = useState<PipscriptLanguage>("python");
  const [pipscriptOutputType, setPipscriptOutputType] = useState<PipscriptOutputType>("indicator");
  const [pipscript, setPipscript] = useState(
    "def calculate(data):\n    values = []\n    for row in data:\n        values.append({\"time\": row[\"time\"], \"value\": row[\"close\"]})\n    return {\"type\": \"line\", \"name\": \"Close Script\", \"values\": values}",
  );
  const [pipscriptOutput, setPipscriptOutput] = useState<PipscriptOutput | null>(null);
  const [pipscriptStatus, setPipscriptStatus] = useState("Ready");
  const [pipscriptRunning, setPipscriptRunning] = useState(false);
  const [savedPipscripts, setSavedPipscripts] = useState<SavedPipscript[]>(loadSavedPipscripts);
  const [selectedSavedPipscriptId, setSelectedSavedPipscriptId] = useState("");
  const pipscriptHasOutputRef = useRef(false);
  const activePipscriptRef = useRef<ActivePipscript | null>(null);
  const pipscriptRunIdRef = useRef(0);
  const pipscriptResultCacheRef = useRef<Map<string, CachedPipscriptResult>>(new Map());
  const PIPSCRIPT_CACHE_TTL_MS = 60_000;

  const runPipscript = async (override?: {
    language?: PipscriptLanguage;
    outputType?: PipscriptOutputType;
    code?: string;
    symbol?: string;
  }) => {
    const executionSymbol = override?.symbol ?? symbol;
    const language = override?.language ?? pipscriptLanguage;
    const outputType = override?.outputType ?? pipscriptOutputType;
    const code = override?.code ?? pipscript;

    // Register the script as the active chart script. The engine derives its
    // dependency scope so future symbol changes can be handled generically.
    const scope = inferPipscriptScope(code);
    activePipscriptRef.current = { language, outputType, code, scope };

    const runId = ++pipscriptRunIdRef.current;
    const cacheKey = pipscriptCacheKey(language, outputType, code, executionSymbol, timeframe);
    const cached = pipscriptResultCacheRef.current.get(cacheKey);
    const cacheIsFresh = cached && Date.now() - cached.cachedAt < PIPSCRIPT_CACHE_TTL_MS;

    if (cacheIsFresh) {
      setPipscriptOutput(cached.output);
      pipscriptHasOutputRef.current = true;
      setPipscriptStatus(
        cached.output.type === "table"
          ? `Table ready · ${cached.output.rows.length} rows · refreshing`
          : `Indicator ready · ${cached.output.points.length} points · refreshing`,
      );
    }

    setPipscriptRunning(true);
    if (!cacheIsFresh) setPipscriptStatus("Preparing Pipscript...");

    try {
      type ChartBar = {
        time: number;
        open: number;
        high: number;
        low: number;
        close: number;
        volume: number;
      };

      const loadCurrentChartData = async (): Promise<ChartBar[]> => {
        const response = await apiFetch("/api/history?symbol=" + encodeURIComponent(executionSymbol) + "&timeframe=" + encodeURIComponent(timeframe) + "&limit=800",
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error("Could not load chart data.");
        const raw = await response.json() as ChartBar[];
        return raw.map((row) => ({
          time: Number(row.time),
          open: Number(row.open),
          high: Number(row.high),
          low: Number(row.low),
          close: Number(row.close),
          volume: Number(row.volume || 0),
        }));
      };

      const fetchGatewayData = async (requests: unknown[]): Promise<unknown> => {
        if (!Array.isArray(requests)) {
          throw new Error("data_requests() must return an array.");
        }
        if (requests.length > 40) {
          throw new Error("A maximum of 40 Pipscript data requests is supported.");
        }

        setPipscriptStatus("Loading requested market data...");
        const response = await apiFetch("/api/pipscript/data", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          cache: "no-store",
          body: JSON.stringify({ requests }),
        });
        const payload = await response.json().catch(() => null) as {
          history?: Record<string, unknown>;
          quotes?: Record<string, unknown>;
          errors?: Array<{ name: string; type: string; message: string }>;
        } | null;
        if (!response.ok) {
          throw new Error(String((payload as { detail?: string } | null)?.detail || "Pipscript data request failed."));
        }
        if (payload?.errors?.length) {
          const names = payload.errors
            .map((error) => error.name)
            .filter(Boolean)
            .join(", ");
          setPipscriptStatus(
            `Market data loaded with ${payload.errors.length} skipped request(s)${names ? `: ${names}` : ""}.`,
          );
        }
        return payload;
      };

      let rawOutput: unknown;

      if (language === "javascript") {
        setPipscriptStatus("Loading JavaScript engine...");
        const runner = new Function(
          "data",
          "SYMBOL",
          `"use strict";
${code}
return {
  requests: typeof data_requests === "function" ? data_requests() : null,
  calculate: typeof calculate === "function" ? calculate : null
};`,
        ) as (data: unknown, symbolValue: string) => {
          requests: unknown[] | null;
          calculate: ((value: unknown) => unknown) | null;
        };

        const program = runner(null, executionSymbol);
        let calculationData: unknown;
        if (program.requests !== null) {
          calculationData = await fetchGatewayData(program.requests);
        } else {
          setPipscriptStatus("Loading chart data...");
          calculationData = await loadCurrentChartData();
        }

        if (typeof program.calculate !== "function") {
          throw new Error("Define calculate(data) in your script.");
        }
        setPipscriptStatus("Running JavaScript...");
        rawOutput = program.calculate(calculationData);
      } else {
        setPipscriptStatus("Loading Python runtime...");
        const pyodide = await loadPyodideRuntime();
        const declarationCode = `import json
SYMBOL = ${JSON.stringify(executionSymbol)}
${code}
_requests = data_requests() if "data_requests" in globals() else None
json.dumps(_requests)`;
        const requestJson = String(await pyodide.runPythonAsync(declarationCode));
        const declaredRequests = JSON.parse(requestJson) as unknown[] | null;

        let calculationData: unknown;
        if (declaredRequests !== null) {
          calculationData = await fetchGatewayData(declaredRequests);
        } else {
          setPipscriptStatus("Loading chart data...");
          calculationData = await loadCurrentChartData();
        }

        setPipscriptStatus("Running Python...");
        const serializedData = JSON.stringify(calculationData);
        const pythonCode = `import json
data = json.loads(${JSON.stringify(serializedData)})
if "calculate" not in globals():
    raise RuntimeError("Define calculate(data) in your script.")
_result = calculate(data)
json.dumps(_result)`;
        rawOutput = JSON.parse(String(await pyodide.runPythonAsync(pythonCode)));
      }

      const normalized = normalizePipscriptOutput(rawOutput, outputType);
      if (runId !== pipscriptRunIdRef.current) return;
      pipscriptResultCacheRef.current.set(cacheKey, {
        output: normalized,
        cachedAt: Date.now(),
      });

      if (runId !== pipscriptRunIdRef.current) return;
      setPipscriptOutput(normalized);
      pipscriptHasOutputRef.current = true;
      setPipscriptStatus(
        normalized.type === "table"
          ? `Table ready · ${normalized.rows.length} rows`
          : `Indicator ready · ${normalized.points.length} points`,
      );
    } catch (error) {
      // A stale request must never erase a newer symbol's result.
      if (runId !== pipscriptRunIdRef.current) return;
      setPipscriptOutput(null);
      setPipscriptStatus(error instanceof Error ? error.message : "PIPScript failed.");
    } finally {
      if (runId === pipscriptRunIdRef.current) {
        setPipscriptRunning(false);
      }
    }
  };

  useEffect(() => {
    const active = activePipscriptRef.current;
    if (!active || active.scope !== "symbol") return;

    void runPipscript({
      language: active.language,
      outputType: active.outputType,
      code: active.code,
      symbol,
    });
  }, [symbol, selectedAccountId]);

  const savePipscript = () => {
    const value = window.prompt(
      "Save PIPScript as:",
      savedPipscripts.find((item) => item.id === selectedSavedPipscriptId)?.name ?? "",
    );
    const name = String(value ?? "").trim().replace(/\\s+/g, " ").slice(0, 40);
    if (!name) return;

    const existing = savedPipscripts.find((item) => item.name.toLowerCase() === name.toLowerCase());
    if (existing && existing.id !== selectedSavedPipscriptId) {
      if (!window.confirm(`"${name}" already exists. Overwrite it?`)) return;
    }

    const id = (existing?.id ?? selectedSavedPipscriptId) || `pipscript-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const saved: SavedPipscript = {
      id,
      name,
      language: pipscriptLanguage,
      outputType: pipscriptOutputType,
      code: pipscript,
      updatedAt: Date.now(),
    };

    setSavedPipscripts((current) => {
      const next = current.filter((item) => item.id !== id);
      const result = [saved, ...next];
      localStorage.setItem(PIPSCRIPT_STORAGE_KEY, JSON.stringify(result));
      return result;
    });
    setSelectedSavedPipscriptId(id);
    setPipscriptStatus(`Saved "${name}".`);
  };

  const applySavedPipscript = async () => {
    const saved = savedPipscripts.find((item) => item.id === selectedSavedPipscriptId);
    if (!saved) {
      setPipscriptStatus("Select a saved PIPScript first.");
      return;
    }

    setPipscriptLanguage(saved.language);
    setPipscriptOutputType(saved.outputType);
    setPipscript(saved.code);
    setPipscriptStatus(`Applying "${saved.name}"...`);
    await runPipscript({
      language: saved.language,
      outputType: saved.outputType,
      code: saved.code,
    });
  };

  const deleteSavedPipscript = () => {
    const saved = savedPipscripts.find((item) => item.id === selectedSavedPipscriptId);
    if (!saved) {
      setPipscriptStatus("Select a saved PIPScript first.");
      return;
    }
    if (!window.confirm(`Delete saved PIPScript "${saved.name}"?`)) return;

    setSavedPipscripts((current) => {
      const next = current.filter((item) => item.id !== saved.id);
      localStorage.setItem(PIPSCRIPT_STORAGE_KEY, JSON.stringify(next));
      return next;
    });
    setSelectedSavedPipscriptId("");
    setPipscriptStatus(`Deleted "${saved.name}".`);
  };

  const selectSavedPipscript = (id: string) => {
    setSelectedSavedPipscriptId(id);
    const saved = savedPipscripts.find((item) => item.id === id);
    if (saved) {
      setPipscriptStatus(`Selected "${saved.name}". Press APPLY to run it.`);
    }
  };

  const loadPipscript = () => {
    const saved = savedPipscripts.find((item) => item.id === selectedSavedPipscriptId);
    if (saved) {
      setPipscriptLanguage(saved.language);
      setPipscriptOutputType(saved.outputType);
      setPipscript(saved.code);
      setPipscriptStatus(`Loaded "${saved.name}" into the editor.`);
      return;
    }
    setPipscriptStatus("Select a saved PIPScript first.");
  };

  useEffect(() => {
    let cancelled = false;
    let firstLoad = true;
    let requestInFlight = false;

    // Never carry the previous symbol's quote into the new symbol header.
    setQuote(null);
    setQuoteLoading(true);
    setQuoteError(false);

    const loadQuote = async () => {
      if (requestInFlight) return;
      requestInFlight = true;
      try {
        const response = await apiFetch("/api/quote?symbol=" + encodeURIComponent(chartApiSymbol || symbol),
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error("quote request failed");

        const data = await response.json() as {
          last: number; change: number; change_percent: number;
          open?: number; high?: number; low?: number; volume?: number;
          bid?: number; ask?: number;
        };

        if (!cancelled) {
          setQuote(data);
          setQuoteError(false);
        }
      } catch {
        if (!cancelled) setQuoteError(true);
      } finally {
        if (!cancelled && firstLoad) {
          setQuoteLoading(false);
          firstLoad = false;
        }
        requestInFlight = false;
      }
    };

    void loadQuote();
    const timer = window.setInterval(() => void loadQuote(), 5000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [symbol, chartApiSymbol]);

  // Primary market data has no broker WebSocket dependency. Watchlist quotes
  // are refreshed by the HTTP polling effect below.
  useEffect(() => {
    if (!watchlist.length) {
      setLiveQuotes({});
      return;
    }

    let cancelled = false;
    let requestInFlight = false;

    const loadWatchlistQuotes = async () => {
      if (requestInFlight) return;
      requestInFlight = true;
      try {
        const symbols = watchlist.map((item) => item.symbol).join(",");
        const response = await apiFetch("/api/quotes?symbols=" + encodeURIComponent(symbols),
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error("watchlist quote request failed");

        const data = await response.json() as Array<{
          symbol: string;
          last: number;
          change: number;
          change_percent: number;
          volume?: number | null;
          bid?: number | null;
          ask?: number | null;
        }>;

        if (cancelled) return;

        setLiveQuotes((current) => {
          const next = { ...current };
          for (const item of data) {
            if (!item.symbol) continue;
            next[item.symbol] = {
              ...next[item.symbol],
              last: Number(item.last),
              change: Number(item.change ?? 0),
              change_percent: Number(item.change_percent ?? 0),
              volume: item.volume == null ? next[item.symbol]?.volume : Number(item.volume),
              bid: item.bid == null ? next[item.symbol]?.bid : Number(item.bid),
              ask: item.ask == null ? next[item.symbol]?.ask : Number(item.ask),
            };
          }
          return next;
        });
      } catch {
        // Keep existing values when a polling request fails.
      } finally {
        requestInFlight = false;
      }
    };

    void loadWatchlistQuotes();
    const refreshMs = watchlist.length > 250 ? 15000 : 5000;
    const timer = window.setInterval(() => void loadWatchlistQuotes(), refreshMs);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [watchlist]);

  const selected = useMemo(
    () =>
      watchlist.find((item) => item.symbol === symbol) ??
      watchlist[0] ??
      { symbol: symbol || "BHARTIARTL", price: "—", change: "—" },
    [symbol, watchlist],
  );

  useEffect(() => {
    const query = search.trim();

    if (!query) {
      setSearchResults([]);
      setSearchLoading(false);
      return;
    }

    let cancelled = false;
    setSearchLoading(true);

    const timer = window.setTimeout(async () => {
      try {
        const response = await apiFetch("/api/symbols/search?q=" + encodeURIComponent(query) + "&limit=12",
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error("symbol search failed");

        const data = await response.json() as Array<{
          symbol: string;
          name: string;
          exchange: string;
          api_symbol: string;
        }>;

        if (!cancelled) setSearchResults(data);
      } catch {
        if (!cancelled) setSearchResults([]);
      } finally {
        if (!cancelled) setSearchLoading(false);
      }
    }, 220);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [search]);

  const filteredWatchlist = watchlist.filter((item) =>
    item.symbol.toLowerCase().includes(watchSearch.trim().toLowerCase()),
  );

  const liveSelected = liveQuotes[selected.symbol];
  const hasLiveData = Boolean(quote || liveSelected);
  const price = quote?.last ?? liveSelected?.last ?? 0;
  const changePercent = quote?.change_percent ?? liveSelected?.change_percent ?? 0;
  const changeAmount = quote?.change ?? liveSelected?.change ?? 0;
  const open = quote?.open;
  const high = quote?.high;
  const low = quote?.low;
  const previousClose = quote ? quote.last - quote.change : undefined;

  const selectSymbol = (next: string, apiSymbol?: string) => {
    const cleanSymbol = next.trim().toUpperCase();
    if (!cleanSymbol) return;
    setSymbol(cleanSymbol);
    setSearch(cleanSymbol);
    setChartApiSymbol(apiSymbol?.trim().toUpperCase() || null);
    const active = activePipscriptRef.current;
    if (active) {
      void runPipscript({
        language: active.language,
        outputType: active.outputType,
        code: active.code,
        symbol: apiSymbol?.trim().toUpperCase() || cleanSymbol,
      });
    }
  };

  const submitSearch = () => {
    const query = search.trim().toUpperCase();
    const watchMatch = watchlist.find((item) => item.symbol === query);

    if (watchMatch) {
      selectSymbol(watchMatch.symbol, watchMatch.apiSymbol);
      setSearchOpen(false);
      return;
    }

    const result = searchResults[0];
    if (result) {
      selectSymbol(result.symbol, result.api_symbol);
      setSearchOpen(false);
    }
  };

  const openSearchResult = (result: {
    symbol: string;
    name: string;
    exchange: string;
    api_symbol: string;
  }) => {
    selectSymbol(result.symbol, result.api_symbol);
    setSearchOpen(false);
    setWatchImportMessage(
      watchlist.some((item) => item.symbol === result.symbol)
        ? "Opened " + result.symbol
        : result.symbol + " opened — use ADD to put it in " + activeWatchlistName,
    );
    window.setTimeout(() => setWatchImportMessage(""), 3000);
  };

  const addSearchResult = (result: {
    symbol: string;
    name: string;
    exchange: string;
    api_symbol: string;
  }) => {
    if (watchlist.some((item) => item.symbol === result.symbol)) {
      openSearchResult(result);
      return;
    }

    if (watchlist.length >= MAX_WATCHLIST_SIZE) {
      setWatchImportMessage("Watchlist limit reached: 1000 symbols");
      window.setTimeout(() => setWatchImportMessage(""), 2500);
      return;
    }

    setWatchlist((current) => [
      ...current,
      { symbol: result.symbol, price: "—", change: "—", apiSymbol: result.api_symbol },
    ]);
    selectSymbol(result.symbol, result.api_symbol);
    setSearchOpen(false);
    setWatchImportMessage("Added " + result.symbol + " to " + activeWatchlistName);
    window.setTimeout(() => setWatchImportMessage(""), 2500);
  };

  const selectWatchItem = (item: WatchItem) => {
    // Market-data symbols are resolved by the primary provider. Do not call
    // the removed broker/FYERS symbol-resolution endpoint.
    selectSymbol(item.symbol, item.apiSymbol);
  };


  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = watchWidth;
    const move = (moveEvent: globalThis.PointerEvent) => {
      setWatchWidth(Math.min(480, Math.max(240, startWidth + startX - moveEvent.clientX)));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };

  if (!authReady || !authenticated) {
    const signingUp = authMode === "signup";

    if (passwordResetMode !== "login" || passwordResetToken) {
      return (
        <main className="auth-screen">
          <form
            className="auth-card"
            onSubmit={passwordResetMode === "request" ? requestPasswordReset : passwordResetMode === "reset" ? confirmPasswordReset : undefined}
          >
            <div className="auth-brand">PIPSGOX</div>
            <div className="auth-title">
              {passwordResetMode === "reset" ? "Set a new password" : "Check your inbox"}
            </div>
            <div className="auth-subtitle">
              {passwordResetMode === "reset"
                ? "Choose a new password for your PIPSGOX account."
                : "If the email address belongs to an account, password reset instructions will be sent to it."}
            </div>

            {passwordResetMode === "request" ? (
              <>
                <label>
                  Email address
                  <input
                    type="email"
                    value={passwordResetEmail}
                    onChange={(e) => setPasswordResetEmail(e.target.value)}
                    autoComplete="email"
                    placeholder="you@example.com"
                    required
                    autoFocus
                  />
                </label>
                <div className="auth-note">For security, PIPSGOX uses the same response whether or not an account exists for the email address.</div>
                {authError && <div className="auth-error">{authError}</div>}
                <button className="auth-submit" type="submit" disabled={authBusy}>
                  {authBusy ? "SENDING..." : "Send Reset Instructions"}
                </button>
              </>
            ) : passwordResetMode === "reset" ? (
              <>
                <label>
                  New password
                  <input
                    type="password"
                    value={passwordResetPassword}
                    onChange={(e) => setPasswordResetPassword(e.target.value)}
                    autoComplete="new-password"
                    minLength={12}
                    required
                    autoFocus
                  />
                </label>
                <label>
                  Confirm new password
                  <input
                    type="password"
                    value={passwordResetConfirmPassword}
                    onChange={(e) => setPasswordResetConfirmPassword(e.target.value)}
                    autoComplete="new-password"
                    minLength={12}
                    required
                  />
                </label>
                <div className="auth-note">Use a strong password of at least 12 characters. All existing PIPSGOX sessions will be signed out after the reset.</div>
                {authError && <div className="auth-error">{authError}</div>}
                <button className="auth-submit" type="submit" disabled={authBusy}>
                  {authBusy ? "RESETTING..." : "Reset Password"}
                </button>
              </>
            ) : (
              <>
                <div className="auth-note">{passwordResetMessage}</div>
                {authError && <div className="auth-error">{authError}</div>}
              </>
            )}

            <button className="auth-secondary-button" type="button" disabled={authBusy} onClick={returnToSignIn}>
              Back to Sign In
            </button>
          </form>
        </main>
      );
    }

    return (
      <main className="auth-screen">
        <form className="auth-card" onSubmit={totpChallengeId && !signingUp ? submitTotpLogin : submitAuth}>
          <div className="auth-brand">PIPSGOX</div>
          <div className="auth-title">{signingUp ? "Create your PIPSGOX account" : "Sign in to PIPSGOX"}</div>
          <div className="auth-subtitle">
            {signingUp
              ? "Create your PIPSGOX account. Broker connections are optional and can be added later from inside the app."
              : "Sign in to PIPSGOX. Broker connections are optional and are used only for trading."}
          </div>

          <div className="auth-mode-switch" role="tablist" aria-label="Authentication mode">
            <button
              type="button"
              className={authMode === "login" ? "active" : ""}
              onClick={() => {
                setAuthMode("login");
                setSignupWithPasskeyMode(false);
                setAuthError("");
                setTotpChallengeId("");
                setTotpChallengeExpiresAt(null);
                setTotpCode("");
                setTotpUsingRecoveryCode(false);
              }}
              disabled={authBusy}
            >
              Sign In
            </button>
            <button
              type="button"
              className={authMode === "signup" ? "active" : ""}
              onClick={() => {
                setAuthMode("signup");
                setSignupWithPasskeyMode(false);
                setAuthError("");
                setTotpChallengeId("");
                setTotpChallengeExpiresAt(null);
                setTotpCode("");
                setTotpUsingRecoveryCode(false);
              }}
              disabled={authBusy}
            >
              Create Account
            </button>
          </div>

          {totpChallengeId && !signingUp ? (
            <>
              <div className="auth-totp-title">
                {totpUsingRecoveryCode ? "Recovery code" : "Two-step verification"}
              </div>
              <div className="auth-totp-note">
                {totpUsingRecoveryCode
                  ? "Enter one unused recovery code from the set you saved when you enabled the authenticator."
                  : "Enter the 6-digit code from your authenticator app."}
                {totpChallengeSecondsLeft !== null
                  ? ` Verification session expires in ${Math.floor(totpChallengeSecondsLeft / 60)}:${String(totpChallengeSecondsLeft % 60).padStart(2, "0")}.`
                  : ""}
              </div>
              <label>
                {totpUsingRecoveryCode ? "Recovery code" : "Authenticator code"}
                <input
                  type="text"
                  value={totpCode}
                  onChange={(e) => setTotpCode(
                    totpUsingRecoveryCode
                      ? e.target.value.toUpperCase().replace(/[^A-Z0-9-]/g, "").slice(0, 20)
                      : e.target.value.replace(/\D/g, "").slice(0, 6)
                  )}
                  inputMode={totpUsingRecoveryCode ? "text" : "numeric"}
                  autoComplete={totpUsingRecoveryCode ? "off" : "one-time-code"}
                  maxLength={totpUsingRecoveryCode ? 20 : 6}
                  placeholder={totpUsingRecoveryCode ? "XXXX-XXXX-XXXX" : "000000"}
                  autoFocus
                  required
                />
              </label>
              {authError && <div className="auth-error">{authError}</div>}
              <button className="auth-submit" type="submit" disabled={authBusy}>
                {authBusy ? "VERIFYING..." : (totpUsingRecoveryCode ? "Use Recovery Code" : "Verify Code")}
              </button>
              <button
                className="auth-secondary-button"
                type="button"
                disabled={authBusy}
                onClick={() => {
                  setTotpUsingRecoveryCode((value) => !value);
                  setTotpCode("");
                  setAuthError("");
                }}
              >
                {totpUsingRecoveryCode ? "Use Authenticator Code" : "Use a Recovery Code"}
              </button>
              <button
                className="auth-secondary-button"
                type="button"
                disabled={authBusy}
                onClick={() => {
                  setTotpChallengeId("");
                  setTotpChallengeExpiresAt(null);
                  setTotpCode("");
                  setTotpUsingRecoveryCode(false);
                  setAuthError("");
                }}
              >
                Back to Sign In
              </button>
            </>
          ) : (
            <>
              <label>
                Username
                <input
                  value={authUsername}
                  onChange={(e) => setAuthUsername(e.target.value)}
                  autoComplete="username"
                  minLength={3}
                  maxLength={64}
                  required
                />
              </label>

              {signingUp && signupWithPasskeyMode ? (
                <>
                  <div className="auth-note">
                    Create a passwordless PIPSGOX account. Your passkey is the account sign-in credential. Use your device fingerprint, face unlock, PIN, or security key.
                  </div>
                  {authError && <div className="auth-error">{authError}</div>}
                  <button className="auth-passkey-button" type="button" disabled={!authReady || authBusy || !passkeySupported()} onClick={() => void createAccountWithPasskey()}>
                    <span>⌁</span>
                    {authBusy ? "CREATING ACCOUNT..." : "Create Account with Passkey"}
                    <em>RECOMMENDED</em>
                  </button>
                  <button className="auth-secondary-button" type="button" disabled={authBusy} onClick={() => { setSignupWithPasskeyMode(false); setAuthError(""); }}>
                    Use Password Instead
                  </button>
                </>
              ) : (
                <>
                  <label>
                    Password
                    <input
                      type="password"
                      value={authPassword}
                      onChange={(e) => setAuthPassword(e.target.value)}
                      autoComplete={signingUp ? "new-password" : "current-password"}
                      minLength={12}
                      required
                    />
                  </label>
                  {signingUp && (
                    <label>
                      Confirm password
                      <input type="password" value={authConfirmPassword} onChange={(e) => setAuthConfirmPassword(e.target.value)} autoComplete="new-password" minLength={12} required />
                    </label>
                  )}
                  {signingUp && <div className="auth-note">Use a strong password of at least 12 characters. Broker connection is optional and can be added later from Account.</div>}
                  {authError && <div className="auth-error">{authError}</div>}
                  <button className="auth-submit" type="submit" disabled={!authReady || authBusy}>
                    {authBusy
                      ? (signingUp ? "CREATING ACCOUNT..." : "SIGNING IN...")
                      : (signingUp ? "Create Account" : "Sign In")}
                  </button>
                  {signingUp && (
                    <button className="auth-passkey-button" type="button" disabled={!authReady || authBusy || !passkeySupported()} onClick={() => { setSignupWithPasskeyMode(true); setAuthError(""); }}>
                      <span>⌁</span>
                      Create Account with Passkey
                      <em>RECOMMENDED</em>
                    </button>
                  )}
                  {!signingUp && (
                    <button
                      className="auth-secondary-button"
                      type="button"
                      disabled={authBusy}
                      onClick={() => {
                        setPasswordResetMode("request");
                        setPasswordResetToken("");
                        setPasswordResetMessage("");
                        setPasswordResetEmail("");
                        setAuthError("");
                      }}
                    >
                      Forgot Password?
                    </button>
                  )}
                  {!signingUp && (
                    <button
                      className="auth-passkey-button"
                      type="button"
                      disabled={!authReady || authBusy || !passkeySupported()}
                      onClick={() => void signInWithPasskey()}
                    >
                      <span>⌁</span>
                      {authBusy ? "AUTHENTICATING..." : "Sign in with Passkey"}
                      <em>RECOMMENDED</em>
                    </button>
                  )}
                </>
              )}
            </>
          )}
        </form>
      </main>
    );
  }

  return (
    <main className={`app ${dark ? "theme-dark" : "theme-light"}`}>
      <div className="menu-bar">
        <div className="menu-left">
          <span className="brand">PIPSGOX</span>
        </div>
        <div className="menu-center">PIPSGOX WEB TERMINAL</div>
        <div className="menu-right">
          <button onClick={() => setAccountOpen(true)}>Account</button>
          <button onClick={() => setSecurityOpen(true)}>Security</button>
          <button onClick={() => { setOrderMessage(""); setOrderOpen(true); }}>Trade</button>
          <button onClick={() => void loadAccountFunds()}>{accountFundsLoading ? "Funds..." : "Funds"}</button>
          <button onClick={() => void loadPositions()}>{positionsLoading ? "Positions..." : "Positions"}</button>
          <button onClick={() => void loadOrdersAndTrades("orders")}>{ordersLoading ? "Orders..." : "Orders"}</button>
          <button onClick={() => void loadHoldings()}>{holdingsLoading ? "Holdings..." : "Holdings"}</button>
          <button onClick={() => setStatusOpen(true)}>Status</button>
          <button>Help</button>
          <button onClick={() => void logout()}>Logout</button>
          <span className="status-dot" /> {selectedAccountId ? "Broker data: " + ((brokerAccounts.find((item) => item.id === selectedAccountId)?.broker || "broker").toUpperCase()) : "Select broker account"}
        </div>
      </div>

      <header className="topbar">
        <button className="top-command">MARKET</button>
        <button className="top-command">WATCHLIST</button>
        <div className="account-selector">
          <span className="account-selector-label">ACCOUNT</span>
          <select
            value={selectedAccountId ?? ""}
            onChange={(event) => { setAppReady(false); setSelectedAccountId(event.target.value ? Number(event.target.value) : null); }}
            aria-label="Trading account"
          >
            {!brokerAccounts.length && <option value="">No account</option>}
            {brokerAccounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.account_name} · {account.broker.toUpperCase()}
              </option>
            ))}
          </select>
        </div>

        <div className="top-search-wrap">
          <div className="top-search">
            <input
              value={search}
              placeholder={selected.symbol}
              aria-label="Search NSE symbol"
              onFocus={() => setSearchOpen(true)}
              onChange={(event) => {
                setSearch(event.target.value);
                setSearchOpen(true);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") submitSearch();
                if (event.key === "Escape") setSearchOpen(false);
              }}
            />
            <button onClick={submitSearch}>GO</button>
          </div>

          {searchOpen && search.trim() && (
            <div className="symbol-search-results">
              {searchLoading ? (
                <div className="symbol-search-empty">Searching NSE symbols...</div>
              ) : searchResults.length ? (
                searchResults.map((result) => (
                  <div key={result.api_symbol} className="symbol-search-row">
                    <button className="symbol-search-main" onClick={() => openSearchResult(result)}>
                      <strong>{result.symbol}</strong>
                      <span>{result.name}</span>
                    </button>
                    <button className="symbol-search-add" onClick={() => addSearchResult(result)} aria-label={"Add " + result.symbol + " to watchlist"}>
                      +
                    </button>
                  </div>
                ))
              ) : (
                <div className="symbol-search-empty">No NSE symbols found</div>
              )}
            </div>
          )}
        </div>

        <div className="top-spacer" />

        <div className="top-control-hub" aria-label="Chart and workspace controls">
          <div className="top-control-group top-timeframe-group">
            {(["D", "W", "M"] as Timeframe[]).map((item) => (
              <button
                key={item}
                className={"top-hub-button top-timeframe-button " + (timeframe === item ? "active" : "")}
                onClick={() => setTimeframe(item)}
                aria-label={"Timeframe " + item}
              >
                {item}
              </button>
            ))}
          </div>

          <span className="top-hub-divider" />

          <div className="top-control-group top-chart-type-group">
            {([
              ["candles", "candle", "Candle"],
              ["bars", "bar", "Bar"],
              ["line", "line", "Line"],
            ] as Array<[ChartType, UiIconName, string]>).map(([type, icon, label]) => (
              <button
                key={type}
                className={"top-hub-button top-icon-button " + (chartType === type ? "active" : "")}
                onClick={() => setChartType(type)}
                title={label}
                aria-label={label}
              >
                <UiIcon name={icon} />
              </button>
            ))}
          </div>

          <span className="top-hub-divider" />

          <button
            className={"top-hub-button top-label-button " + (panel === "indicators" ? "active" : "")}
            onClick={() => setPanel("indicators")}
            title="Indicators"
            aria-label="Indicators"
          >
            <UiIcon name="indicator" />
            <span>Indicators</span>
          </button>

          <button
            className={"top-hub-button top-label-button " + (panel === "pipscript" ? "active" : "")}
            onClick={() => setPanel("pipscript")}
            title="Pipscript"
            aria-label="Pipscript"
          >
            <UiIcon name="pipscript" />
            <span>Pipscript</span>
          </button>

          <span className="top-hub-divider" />

          <button
            className="top-hub-button top-label-button"
            onClick={() => setDark((value) => !value)}
            title={dark ? "Switch to light theme" : "Switch to dark theme"}
            aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
          >
            <UiIcon name="sun" />
            <span>{dark ? "Light" : "Dark"}</span>
          </button>

          <button
            className={"top-hub-button top-label-button " + (panel === "settings" ? "active" : "")}
            onClick={() => setPanel("settings")}
            title="Settings"
            aria-label="Settings"
          >
            <UiIcon name="settings" />
            <span>Settings</span>
          </button>
        </div>
      </header>

      <section className="workspace">
        <section className="chart-workspace">
          <div className="chart-container">
            <Chart
              chartType={chartType}
              dark={dark}
              symbol={symbol}
              timeframe={timeframe}
              range={chartRange}
              activeDrawingTool={activeDrawingTool}
              drawingCommand={drawingCommand}
              chartTheme={chartSettings.theme}
              chartColors={chartSettings.colors}
              showGrid={chartSettings.showGrid}
              showCrosshair={chartSettings.showCrosshair}
              showVolume={chartSettings.showVolume}
              showVwap={chartSettings.showVwap}
              show52WeekHigh={chartSettings.show52WeekHigh}
              show52WeekLow={chartSettings.show52WeekLow}
              showPreviousClose={chartSettings.showPreviousClose}
              previousClose={previousClose}
              pipscriptOutput={pipscriptOutput}
            />

            <div
              className={"drawing-toolbar" + (drawingToolbarOpen ? "" : " is-hidden")}
              aria-label="Drawing tools"
              style={{ left: drawingToolbarPosition.x, top: drawingToolbarPosition.y }}
              onPointerDown={startDrawingToolbarDrag}
            >
              {drawingToolbarOpen && (
                <>
                  <span className="drawing-toolbar-grip" title="Drag toolbar" aria-label="Drag toolbar">⠿</span>
                  <button type="button" className={activeDrawingTool === "horizontalRay" ? "active" : ""} onClick={() => setActiveDrawingTool("horizontalRay")} title="Horizontal Ray" aria-label="Horizontal Ray"><DrawingIcon name="horizontalRay" /></button>
                  <button type="button" className={activeDrawingTool === "trendline" ? "active" : ""} onClick={() => setActiveDrawingTool("trendline")} title="Trendline" aria-label="Trendline"><DrawingIcon name="trendline" /></button>
                  <button type="button" className={activeDrawingTool === "rectangle" ? "active" : ""} onClick={() => setActiveDrawingTool("rectangle")} title="Rectangle" aria-label="Rectangle"><DrawingIcon name="rectangle" /></button>
                  <span className="drawing-toolbar-divider" />
                  <button type="button" className={activeDrawingTool === "long" ? "active" : ""} onClick={() => setActiveDrawingTool("long")} title="Long Position" aria-label="Long Position"><DrawingIcon name="long" /></button>
                  <button type="button" className={activeDrawingTool === "short" ? "active" : ""} onClick={() => setActiveDrawingTool("short")} title="Short Position" aria-label="Short Position"><DrawingIcon name="short" /></button>
                  <button type="button" className={activeDrawingTool === "arrow" ? "active" : ""} onClick={() => setActiveDrawingTool("arrow")} title="Arrow" aria-label="Arrow"><DrawingIcon name="arrow" /></button>
                  <button type="button" className={activeDrawingTool === "brush" ? "active" : ""} onClick={() => setActiveDrawingTool("brush")} title="Brush" aria-label="Brush"><DrawingIcon name="brush" /></button>
                  <button type="button" className={activeDrawingTool === "text" ? "active" : ""} onClick={() => setActiveDrawingTool("text")} title="Text" aria-label="Text"><DrawingIcon name="text" /></button>
                  <span className="drawing-toolbar-divider" />
                  <button type="button" onClick={() => setDrawingCommand({ type: "delete", nonce: Date.now() })} title="Delete Last Drawing" aria-label="Delete Last Drawing"><DrawingIcon name="delete" /></button>
                  <button type="button" onClick={() => setDrawingCommand({ type: "clear", nonce: Date.now() })} title="Clear Drawings" aria-label="Clear Drawings"><DrawingIcon name="clear" /></button>
                </>
              )}
              <button type="button" className="drawing-toolbar-toggle" onClick={() => setDrawingToolbarOpen((value) => !value)} title={drawingToolbarOpen ? "Hide drawing tools" : "Show drawing tools"} aria-label={drawingToolbarOpen ? "Hide drawing tools" : "Show drawing tools"}>
                {drawingToolbarOpen ? "‹" : "›"}
              </button>
            </div>
            <div className="chart-header-overlay">
              <span className="chart-header-symbol">
                {symbol} · {timeframe} · NSE
              </span>
              <span className={`chart-header-price ${changePercent < 0 ? "negative" : "positive"}`}>
                {hasLiveData ? price.toFixed(2) : "—"}
              </span>
              <span className={`chart-header-change ${changePercent < 0 ? "negative" : "positive"}`}>
                {hasLiveData
                  ? `${changeAmount >= 0 ? "+" : ""}${changeAmount.toFixed(2)} (${changePercent.toFixed(2)}%)`
                  : "No data"}
              </span>
              {hasLiveData && (
                <span className="chart-header-ohlc">
                  O <b>{open != null ? open.toFixed(2) : "—"}</b>
                  H <b>{high != null ? high.toFixed(2) : "—"}</b>
                  L <b>{low != null ? low.toFixed(2) : "—"}</b>
                  C <b>{price.toFixed(2)}</b>
                </span>
              )}
            </div>
          </div>

          <div className="bottom-bar">
            {(["1D", "5D", "1M", "3M", "6M", "YTD", "1Y", "5Y", "ALL"] as ChartRange[]).map((preset) => (
              <button
                key={preset}
                className={chartRange === preset ? "active" : ""}
                onClick={() => selectChartRange(preset)}
              >
                {preset}
              </button>
            ))}
            <span className="bottom-spacer" />
          </div>
        </section>

        {watchOpen ? (
          <aside className="watchlist" style={{ width: watchWidth }}>
            <div className="watch-resize-handle" onPointerDown={startResize} />
            <div className="watch-tabs">
              <select
                className="watchlist-selector"
                value={activeWatchlistName}
                onChange={(event) => {
                  const name = event.target.value;
                  setActiveWatchlistName(name);
                  const first = watchlists[name]?.[0]?.symbol;
                  const firstItem = watchlists[name]?.[0];
                  if (firstItem) selectSymbol(firstItem.symbol, firstItem.apiSymbol);
                }}
                aria-label="Select watchlist"
              >
                {Object.keys(watchlists).map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <button
                className={watchPanel === "watchlist" ? "active" : ""}
                onClick={() => setWatchPanel("watchlist")}
              >
                WATCHLIST
              </button>
              <button
                className={watchPanel === "44ma" ? "active" : ""}
                onClick={() => setWatchPanel("44ma")}
              >
                44 MA
              </button>
              <button
                className={watchPanel === "movers" ? "active" : ""}
                onClick={() => setWatchPanel("movers")}
              >
                MOVERS
              </button>
            </div>
            <div className="watch-header">
              <strong>{activeWatchlistName}</strong>
              <span>{watchlist.length} / {MAX_WATCHLIST_SIZE}</span>
              <div className="watch-spacer" />
              <div className="watch-actions" aria-label="Watchlist actions">
                <button onClick={() => setWatchDialog({ type: "add", value: "" })} title="Add symbol" aria-label="Add symbol"><UiIcon name="add" /></button>
                <button onClick={() => setWatchDialog({ type: "import", value: "" })} title="Import watchlist" aria-label="Import watchlist"><UiIcon name="import" /></button>
                <button onClick={() => setWatchDialog({ type: "export", value: "" })} title="Export watchlist" aria-label="Export watchlist"><UiIcon name="export" /></button>
                <button onClick={() => setWatchDialog({ type: "new", value: "" })} title="New watchlist" aria-label="New watchlist"><UiIcon name="new" /></button>
                <button onClick={() => setWatchDialog({ type: "delete", value: "" })} title="Delete watchlist" aria-label="Delete watchlist"><UiIcon name="delete" /></button>
                <button onClick={() => setWatchOpen(false)} title="Hide watchlist" aria-label="Hide watchlist"><UiIcon name="hide" /></button>
              </div>
              <input
                ref={watchImportRef}
                className="watch-import-input"
                type="file"
                accept=".csv,.txt,text/csv,text/plain"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void importWatchlist(file);
                }}
              />
            </div>
            {watchPanel === "watchlist" ? (
              <>
                <input className="watch-search" placeholder="Search symbols..." value={watchSearch}
                  onChange={(event) => setWatchSearch(event.target.value)} />
                {watchImportMessage && <div className="watch-message">{watchImportMessage}</div>}
                <div className="watch-columns"><span>SYMBOL</span><span>LAST</span><span>CHANGE %</span><span>VOL</span><span>BID / ASK</span><span></span></div>
                <div className="watch-items">
                  {filteredWatchlist.map((item) => {
                const liveQuote = liveQuotes[item.symbol];
                return (
                <div
                  key={item.symbol}
                  className={`watch-row ${item.symbol === symbol ? "selected" : ""}`}
                  draggable
                  onDragStart={() => setDraggedSymbol(item.symbol)}
                  onDragEnd={() => setDraggedSymbol(null)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => dropWatchSymbol(item.symbol)}
                >
                  <button className="watch-row-main" onClick={() => void selectWatchItem(item)}>
                    <span className="watch-symbol">{item.symbol}</span>
                    <span className="watch-price">{liveQuote ? liveQuote.last.toFixed(2) : "—"}</span>
                    <span className={`watch-change ${(liveQuote?.change_percent ?? Number(item.change.replace("%", ""))) < 0 ? "negative" : "positive"}`}>
                      {liveQuote ? `${liveQuote.change_percent >= 0 ? "+" : ""}${liveQuote.change_percent.toFixed(2)}%` : "—"}
                    </span>
                    <span className={`watch-volume ${liveQuote?.volume != null && liveQuote.volume <= 10000 ? "low-volume" : ""}`}>
                      {formatWatchVolume(liveQuote?.volume)}
                    </span>
                    <span className="watch-bid-ask">
                      <span className="watch-bid-value">{formatWatchQuoteValue(liveQuote?.bid)}</span>
                      <span className="watch-bid-ask-separator"> / </span>
                      <span className="watch-ask-value">{formatWatchQuoteValue(liveQuote?.ask)}</span>
                    </span>
                  </button>
                  <button className="watch-remove" onClick={() => removeWatchSymbol(item.symbol)} aria-label={`Remove ${item.symbol}`}>×</button>
                </div>
                );
                  })}
                </div>
                <div className="watch-footer">
                  {watchlist.length ? "Click a symbol to load chart" : "Watchlist empty — use symbol search or ADD"}
                </div>
              </>
            ) : watchPanel === "44ma" ? (
              <>
                <div className="watch-panel-title">
                  <strong>44 MA</strong>
                  <span>{ma44Scan?.message || "Automatic scanner"}</span>
                </div>
                <input className="watch-search" placeholder="Search scan results..." value={ma44Search}
                  onChange={(event) => setMa44Search(event.target.value)} />
                <div className="ma44-mode-tabs">
                  <button className={ma44Mode === "live" ? "active" : ""} onClick={() => setMa44Mode("live")}>LIVE SCAN</button>
                  <button className={ma44Mode === "eod" ? "active" : ""} onClick={() => setMa44Mode("eod")}>EOD SCAN</button>
                </div>
                <div className="ma44-status">
                  <span>{ma44Scan?.status || "CONNECTING"}</span>
                  <small>{ma44Scan ? `${ma44Scan.results.length} matches · ${ma44Scan.universe_count.toLocaleString("en-IN")} stocks` : "Loading..."}</small>
                </div>
                {ma44Mode === "eod" && ma44Scan && ma44Scan.stage !== "COMPLETE" && ma44Scan.stage !== "ERROR" && (
                  <div className="ma44-loader">
                    <div className="ma44-loader-head">
                      <strong>{ma44Scan.stage === "SCANNING_EOD" ? "EOD SCAN" : "PREPARING EOD SCAN"}</strong>
                      <span>{Math.round(ma44Scan.progress ?? 0)}%</span>
                    </div>
                    <div className="ma44-progress-track">
                      <div className="ma44-progress-fill" style={{ width: (Math.min(100, Math.max(0, ma44Scan.progress ?? 0)) + "%") }} />
                    </div>
                    <div className="ma44-loader-message">{ma44Scan.message}</div>
                    <div className="ma44-loader-count">
                      {ma44Scan.processed.toLocaleString("en-IN")} / {ma44Scan.total.toLocaleString("en-IN")} stocks processed
                    </div>
                  </div>
                )}
                <div className="ma44-columns">
                  <span>SYMBOL</span><span>CLOSED</span><span>CHANGE %</span><span>44 MA</span>
                </div>
                <div className="watch-items ma44-items">
                  {(ma44Scan?.results || []).filter((item) => item.symbol.includes(ma44Search.trim().toUpperCase())).map((item) => (
                    <button key={item.symbol} className="ma44-row" onClick={() => void selectSymbol(item.symbol)}>
                      <span>{item.symbol}</span>
                      <span>{item.closed.toFixed(2)}</span>
                      <span className={item.change_percent < 0 ? "negative" : "positive"}>{item.change_percent >= 0 ? "+" : ""}{item.change_percent.toFixed(2)}%</span>
                      <span className={item.ma_distance < 0 ? "negative" : "positive"}>{item.ma_distance >= 0 ? "+" : ""}{item.ma_distance.toFixed(2)}%</span>
                    </button>
                  ))}
                  {!(ma44Scan?.results || []).some((item) => item.symbol.includes(ma44Search.trim().toUpperCase())) && (
                    <div className="ma44-empty">
                      <span>{ma44Scan?.status === "LIVE" ? "Scanning..." : "No matching stocks"}</span>
                      <small>{ma44Scan?.error || ma44Scan?.message || "Results will appear automatically."}</small>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <div className="watch-items">
                <div className="ma44-empty">
                  <span>Movers</span>
                  <small>Existing movers view will remain available here.</small>
                </div>
              </div>
            )}
          </aside>
        ) : (
          <button className="watch-open" onClick={() => setWatchOpen(true)}>SHOW WATCHLIST</button>
        )}
      </section>

      {watchDialog && (
        <div className="watch-dialog-backdrop" onClick={() => setWatchDialog(null)}>
          <section className="watch-dialog" onClick={(event) => event.stopPropagation()}>
            <strong className="watch-dialog-title">
              {watchDialog.type === "add" ? "Add symbol" : watchDialog.type === "new" ? "New watchlist" : watchDialog.type === "delete" ? "Delete watchlist" : watchDialog.type === "export" ? "Export watchlist" : "Import watchlist"}
            </strong>
            <p className="watch-dialog-subtitle">
              {watchDialog.type === "add" ? "Enter an NSE symbol to add." : watchDialog.type === "new" ? "Create a new watchlist." : watchDialog.type === "delete" ? `Delete “${activeWatchlistName}” and its symbols?` : watchDialog.type === "export" ? `Export ${watchlist.length} symbols as CSV.` : "Choose a CSV or text watchlist file."}
            </p>
            {(watchDialog.type === "add" || watchDialog.type === "new") && (
              <input
                autoFocus
                className="watch-dialog-input"
                value={watchDialog.value}
                placeholder={watchDialog.type === "add" ? "e.g. RELIANCE" : "Watchlist name"}
                onChange={(event) => setWatchDialog((current) => current ? { ...current, value: event.target.value } : current)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") setWatchDialog(null);
                  if (event.key === "Enter") watchDialog.type === "add" ? addWatchSymbol(watchDialog.value) : createWatchlist(watchDialog.value);
                }}
              />
            )}
            <div className="watch-dialog-actions">
              <button className="watch-dialog-cancel" onClick={() => setWatchDialog(null)}>Cancel</button>
              {watchDialog.type === "delete" ? (
                <button className="watch-dialog-danger" onClick={deleteWatchlist}>Delete</button>
              ) : watchDialog.type === "export" ? (
                <button className="watch-dialog-primary" onClick={exportWatchlist}>Export</button>
              ) : watchDialog.type === "import" ? (
                <button className="watch-dialog-primary" onClick={() => watchImportRef.current?.click()}>Choose file</button>
              ) : (
                <button className="watch-dialog-primary" onClick={() => watchDialog.type === "add" ? addWatchSymbol(watchDialog.value) : createWatchlist(watchDialog.value)}>
                  {watchDialog.type === "add" ? "Add" : "Create"}
                </button>
              )}
            </div>
          </section>
        </div>
      )}

      {statusOpen && (
        <div className="modal-backdrop status-modal-backdrop" onClick={() => setStatusOpen(false)}>
          <section className="status-modal" onClick={(event) => event.stopPropagation()}>
            <StatusCenter accountId={selectedAccountId} embedded onClose={() => setStatusOpen(false)} />
          </section>
        </div>
      )}

      {securityOpen && (
        <SecuritySettings onClose={() => setSecurityOpen(false)} />
      )}

      {accountOpen && (
        <BrokerConnections
          onClose={() => {
            setAccountOpen(false);
            void apiFetch("/api/broker/accounts")
              .then(async (response) => {
                const payload = await response.json().catch(() => []);
                if (!response.ok || !Array.isArray(payload)) return;
                setBrokerAccounts(payload);
                setSelectedAccountId((current) => {
                  if (current && payload.some((account) => account.id === current)) return current;
                  const connected = payload.find((account) => account.status === "connected");
                  return connected?.id ?? null;
                });
              })
              .catch(() => {});
          }}
          onDisconnectAll={disconnectAllBrokers}
        />
      )}

      {positionsOpen && (
        <div className="modal-backdrop" onClick={() => setPositionsOpen(false)}>
          <section className="modal positions-modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <strong>OPEN POSITIONS</strong>
              <button onClick={() => setPositionsOpen(false)}>CLOSE</button>
            </div>
            <div className="positions-panel">
              {positionsError ? <div className="funds-error">{positionsError}</div> : positions.length ? (
                <div className="positions-table-wrap">
                  <table className="positions-table">
                    <thead><tr><th>SYMBOL</th><th>QTY</th><th>AVG</th><th>PRODUCT</th><th>SIDE</th><th>P&L</th></tr></thead>
                    <tbody>
                      {positions.map((item, index) => {
                        const pnl = positionNumber(item.pnl);
                        return <tr key={String(item.security_id || item.symbol || index)}>
                          <td><strong>{String(item.symbol || "—")}</strong><small>{String(item.exchange || "")}</small></td>
                          <td>{positionNumber(item.quantity).toLocaleString("en-IN")}</td>
                          <td>₹{positionNumber(item.cost_price || item.buy_avg).toFixed(2)}</td>
                          <td>{String(item.product || "—")}</td>
                          <td>{String(item.side || "—")}</td>
                          <td className={pnl < 0 ? "negative" : pnl > 0 ? "positive" : ""}>₹{pnl.toFixed(2)}</td>
                        </tr>;
                      })}
                    </tbody>
                  </table>
                </div>
              ) : <div className="positions-empty">No open positions.</div>}
              <button className="funds-refresh" disabled={positionsLoading} onClick={() => void loadPositions()}>
                {positionsLoading ? "REFRESHING..." : "REFRESH"}
              </button>
            </div>
          </section>
        </div>
      )}

      {holdingsOpen && (
        <div className="modal-backdrop" onClick={() => setHoldingsOpen(false)}>
          <section className="modal holdings-modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <strong>HOLDINGS</strong>
              <button onClick={() => setHoldingsOpen(false)}>CLOSE</button>
            </div>
            <div className="holdings-panel">
              {holdingsError ? <div className="funds-error">{holdingsError}</div> : holdings.length ? (
                <div className="holdings-table-wrap">
                  <table className="holdings-table">
                    <thead><tr><th>SYMBOL</th><th>QTY</th><th>AVAILABLE</th><th>AVG PRICE</th><th>INVESTED</th><th>CURRENT</th><th>P&L</th></tr></thead>
                    <tbody>
                      {holdings.map((item, index) => {
                        const pnl = item.pnl == null ? null : Number(item.pnl);
                        return <tr key={String(item.security_id || item.symbol || index)}>
                          <td><strong>{String(item.symbol || "—")}</strong><small>{String(item.exchange || "")}{item.isin ? " · " + String(item.isin) : ""}</small></td>
                          <td>{Number(item.quantity || 0).toLocaleString("en-IN")}</td>
                          <td>{Number(item.available_quantity || 0).toLocaleString("en-IN")}</td>
                          <td>₹{Number(item.average_price || 0).toFixed(2)}</td>
                          <td>₹{Number(item.invested_value || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                          <td>{item.current_value == null ? "—" : "₹" + Number(item.current_value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                          <td className={pnl != null && pnl < 0 ? "negative" : pnl != null && pnl > 0 ? "positive" : ""}>{pnl == null ? "—" : "₹" + pnl.toFixed(2)}</td>
                        </tr>;
                      })}
                    </tbody>
                  </table>
                </div>
              ) : <div className="holdings-empty">{holdingsLoading ? "Loading holdings..." : "No holdings found."}</div>}
              <button className="funds-refresh" disabled={holdingsLoading} onClick={() => void loadHoldings()}>
                {holdingsLoading ? "REFRESHING..." : "REFRESH"}
              </button>
            </div>
          </section>
        </div>
      )}

      {ordersOpen && (
        <div className="modal-backdrop" onClick={() => setOrdersOpen(false)}>
          <section className="modal orders-modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <strong>{ordersTab === "orders" ? "ORDERS" : "TRADEBOOK"}</strong>
              <button onClick={() => setOrdersOpen(false)}>CLOSE</button>
            </div>
            <div className="orders-panel">
              <div className="orders-tabs">
                <button className={ordersTab === "orders" ? "active" : ""} onClick={() => void loadOrdersAndTrades("orders")}>ORDERS</button>
                <button className={ordersTab === "trades" ? "active" : ""} onClick={() => void loadOrdersAndTrades("trades")}>TRADES</button>
              </div>
              {ordersError && <div className="funds-error">{ordersError}</div>}
              {ordersTab === "orders" ? (
                orders.length ? (
                  <div className="orders-table-wrap">
                    <table className="orders-table">
                      <thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>QTY</th><th>FILLED</th><th>TYPE</th><th>PRICE</th><th>STATUS</th><th></th></tr></thead>
                      <tbody>
                        {orders.map((item, index) => {
                          const orderId = String(item.order_id || index);
                          const status = String(item.status || "—");
                          const cancellable = ["PENDING", "TRANSIT", "OPEN", "PART_TRADED", "PARTIALLY_FILLED"].includes(status.toUpperCase());
                          return <tr key={orderId}>
                            <td>{String(item.created_at || "—")}</td>
                            <td><strong>{String(item.symbol || "—")}</strong><small>{String(item.exchange || "")}</small></td>
                            <td className={String(item.side).toUpperCase() === "BUY" ? "positive" : "negative"}>{String(item.side || "—")}</td>
                            <td>{Number(item.quantity || 0).toLocaleString("en-IN")}</td>
                            <td>{Number(item.filled_quantity || 0).toLocaleString("en-IN")}</td>
                            <td>{String(item.order_type || "—")}</td>
                            <td>₹{Number(item.average_price || item.price || 0).toFixed(2)}</td>
                            <td><span className={"order-status " + status.toLowerCase().replaceAll("_", "-")}>{status}</span></td>
                            <td>{cancellable && <button className="order-cancel" disabled={cancellingOrderId === orderId} onClick={() => void cancelBrokerOrder(orderId)}>{cancellingOrderId === orderId ? "..." : "CANCEL"}</button>}</td>
                          </tr>;
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : <div className="orders-empty">{ordersLoading ? "Loading orders..." : "No orders for today."}</div>
              ) : (
                trades.length ? (
                  <div className="orders-table-wrap">
                    <table className="orders-table">
                      <thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>QTY</th><th>PRICE</th><th>PRODUCT</th><th>ORDER ID</th></tr></thead>
                      <tbody>
                        {trades.map((item, index) => <tr key={String(item.trade_id || index)}>
                          <td>{String(item.traded_at || "—")}</td>
                          <td><strong>{String(item.symbol || "—")}</strong><small>{String(item.exchange || "")}</small></td>
                          <td className={String(item.side).toUpperCase() === "BUY" ? "positive" : "negative"}>{String(item.side || "—")}</td>
                          <td>{Number(item.quantity || 0).toLocaleString("en-IN")}</td>
                          <td>₹{Number(item.price || 0).toFixed(2)}</td>
                          <td>{String(item.product || "—")}</td>
                          <td>{String(item.order_id || "—")}</td>
                        </tr>)}
                      </tbody>
                    </table>
                  </div>
                ) : <div className="orders-empty">{ordersLoading ? "Loading trades..." : "No trades for today."}</div>
              )}
              <button className="funds-refresh" disabled={ordersLoading} onClick={() => void loadOrdersAndTrades(ordersTab)}>
                {ordersLoading ? "REFRESHING..." : "REFRESH"}
              </button>
            </div>
          </section>
        </div>
      )}

      {accountFundsOpen && (
        <div className="modal-backdrop" onClick={() => setAccountFundsOpen(false)}>
          <section className="modal funds-modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <strong>ACCOUNT FUNDS</strong>
              <button onClick={() => setAccountFundsOpen(false)}>CLOSE</button>
            </div>
            <div className="funds-panel">
              <div className="funds-account">
                {selectedAccountId ? (brokerAccounts.find((item) => item.id === selectedAccountId)?.account_name || "Selected account") : "No account"}
              </div>
              {accountFundsError ? (
                <div className="funds-error">{accountFundsError}</div>
              ) : accountFunds ? (
                <div className="funds-grid">
                  <div className="fund-card primary"><span>AVAILABLE</span><strong>₹{(fundNumber(["available"]) ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                  <div className="fund-card"><span>SOD LIMIT</span><strong>₹{(fundNumber(["sod_limit"]) ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                  <div className="fund-card"><span>UTILIZED</span><strong>₹{(fundNumber(["utilized"]) ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                  <div className="fund-card"><span>COLLATERAL</span><strong>₹{(fundNumber(["collateral"]) ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                  <div className="fund-card"><span>RECEIVABLE</span><strong>₹{(fundNumber(["receivable"]) ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                  <div className="fund-card"><span>WITHDRAWABLE</span><strong>₹{(fundNumber(["withdrawable"]) ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></div>
                </div>
              ) : (
                <div className="funds-error">No funds data returned.</div>
              )}
              <button className="funds-refresh" disabled={accountFundsLoading} onClick={() => void loadAccountFunds()}>
                {accountFundsLoading ? "REFRESHING..." : "REFRESH"}
              </button>
            </div>
          </section>
        </div>
      )}

      {orderOpen && (
        <div className="modal-backdrop" onClick={() => setOrderOpen(false)}>
          <section className="modal order-modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header"><strong>PLACE ORDER</strong><button onClick={() => setOrderOpen(false)}>CLOSE</button></div>
            <div className="order-form">
              <div className="order-account-note">{selectedAccountId ? "Account: " + (brokerAccounts.find((item) => item.id === selectedAccountId)?.account_name || selectedAccountId) : "No trading account selected"}</div>
              <div className="order-symbol-row"><span>SYMBOL</span><strong>{symbol}</strong></div>
              <div className="order-side-row">
                <button className={orderSide === "BUY" ? "order-side active-buy" : "order-side"} onClick={() => setOrderSide("BUY")}>BUY</button>
                <button className={orderSide === "SELL" ? "order-side active-sell" : "order-side"} onClick={() => setOrderSide("SELL")}>SELL</button>
              </div>
              <label>Order Type<select value={orderType} onChange={(event) => setOrderType(event.target.value as typeof orderType)}><option value="MARKET">MARKET</option><option value="LIMIT">LIMIT</option><option value="STOP_LOSS">STOP LOSS</option><option value="STOP_LOSS_MARKET">STOP LOSS MARKET</option></select></label>
              <label>Product<select value={orderProduct} onChange={(event) => setOrderProduct(event.target.value as typeof orderProduct)}><option value="INTRADAY">INTRADAY</option><option value="CNC">CNC</option><option value="MARGIN">MARGIN</option><option value="MTF">MTF</option></select></label>
              <label>Quantity<input type="number" min="1" step="1" value={orderQuantity} onChange={(event) => setOrderQuantity(event.target.value)} /></label>
              {(orderType === "LIMIT" || orderType === "STOP_LOSS") && <label>Limit Price<input type="number" min="0" step="0.05" value={orderPrice} onChange={(event) => setOrderPrice(event.target.value)} /></label>}
              {(orderType === "STOP_LOSS" || orderType === "STOP_LOSS_MARKET") && <label>Trigger Price<input type="number" min="0" step="0.05" value={orderTriggerPrice} onChange={(event) => setOrderTriggerPrice(event.target.value)} /></label>}
              <button className={orderSide === "BUY" ? "order-submit buy" : "order-submit sell"} disabled={orderSubmitting || !selectedAccountId} onClick={() => void submitOrder()}>{orderSubmitting ? "SUBMITTING..." : orderSide + " " + symbol}</button>
              {orderMessage && <div className="order-message">{orderMessage}</div>}
              <div className="order-warning">Live order: this sends the request to the selected broker account.</div>
            </div>
          </section>
        </div>
      )}

      {panel && (
        <div className="modal-backdrop" onClick={() => setPanel(null)}>
          <section className={`modal ${panel === "pipscript" ? "pipscript-modal" : ""}`} onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <strong>{panel === "pipscript" ? "PIPSGOX PIPSCRIPT BUILDER" : panel === "settings" ? "CHART SETTINGS" : "INDICATORS"}</strong>
              <button onClick={() => setPanel(null)}>CLOSE</button>
            </div>

            {panel === "indicators" ? (
              <div className="indicator-panel">
                <p>Select technical studies and optional overlays for the active chart.</p>

                <div className="indicator-section-title">CORE CHART INDICATORS</div>
                <label className="indicator-toggle-row">
                  <span>MA 44 <small>core</small></span>
                  <input
                    type="checkbox"
                    checked={chartSettings.showMa44}
                    onChange={(event) => updateChartSettings({ showMa44: event.target.checked })}
                  />
                  <i />
                </label>
                <div className="indicator-row indicator-row-static">
                  <span>MA 50</span><span>ON</span>
                </div>
                <div className="indicator-row indicator-row-static">
                  <span>MA 200</span><span>ON</span>
                </div>
                <label className="indicator-toggle-row">
                  <span>Volume</span>
                  <input
                    type="checkbox"
                    checked={chartSettings.showVolume}
                    onChange={(event) => updateChartSettings({ showVolume: event.target.checked })}
                  />
                  <i />
                </label>

                <div className="indicator-section-title optional">OPTIONAL CHART OVERLAYS</div>
                <p className="indicator-help">These stay hidden unless you select them.</p>

                <label className="indicator-toggle-row">
                  <span>VWAP <small>intraday</small></span>
                  <input
                    type="checkbox"
                    checked={chartSettings.showVwap}
                    onChange={(event) => updateChartSettings({ showVwap: event.target.checked })}
                  />
                  <i />
                </label>

                <label className="indicator-toggle-row">
                  <span>52W High</span>
                  <input
                    type="checkbox"
                    checked={chartSettings.show52WeekHigh}
                    onChange={(event) => updateChartSettings({ show52WeekHigh: event.target.checked })}
                  />
                  <i />
                </label>

                <label className="indicator-toggle-row">
                  <span>52W Low</span>
                  <input
                    type="checkbox"
                    checked={chartSettings.show52WeekLow}
                    onChange={(event) => updateChartSettings({ show52WeekLow: event.target.checked })}
                  />
                  <i />
                </label>

                <label className="indicator-toggle-row">
                  <span>Previous Close</span>
                  <input
                    type="checkbox"
                    checked={chartSettings.showPreviousClose}
                    onChange={(event) => updateChartSettings({ showPreviousClose: event.target.checked })}
                  />
                  <i />
                </label>

              </div>
            ) : panel === "pipscript" ? (
              <div className="builder-panel">
                <div className="builder-toolbar">
                  <select
                    className="pipscript-saved-select"
                    value={selectedSavedPipscriptId}
                    onChange={(event) => selectSavedPipscript(event.target.value)}
                    aria-label="Saved PIPScripts"
                  >
                    <option value="">Saved PIPScripts...</option>
                    {savedPipscripts.map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </select>
                  <select
                    value={pipscriptLanguage}
                    onChange={(event) => {
                      const language = event.target.value as PipscriptLanguage;
                      setPipscriptLanguage(language);
                      if (language === "python") {
                        setPipscript(
                          "def calculate(data):\n    values = []\n    for row in data:\n        values.append({\"time\": row[\"time\"], \"value\": row[\"close\"]})\n    return {\"type\": \"line\", \"name\": \"Close Script\", \"values\": values}",
                        );
                      } else {
                        setPipscript(
                          "function calculate(data) {\n  return {\n    type: \"line\",\n    name: \"Close Script\",\n    values: data.map(row => ({ time: row.time, value: row.close }))\n  };\n}",
                        );
                      }
                    }}
                  >
                    <option value="python">Python</option>
                    <option value="javascript">JavaScript</option>
                  </select>
                  <select value={pipscriptOutputType} onChange={(event) => setPipscriptOutputType(event.target.value as PipscriptOutputType)}>
                    <option value="indicator">Indicator</option>
                    <option value="table">Table</option>
                  </select>
                  <span className="builder-spacer" />
                  <button onClick={loadPipscript} disabled={!selectedSavedPipscriptId}>LOAD</button>
                  <button onClick={savePipscript}>SAVE</button>
                  <button onClick={() => void applySavedPipscript()} disabled={!selectedSavedPipscriptId || pipscriptRunning}>
                    APPLY
                  </button>
                  <button onClick={deleteSavedPipscript} disabled={!selectedSavedPipscriptId}>DELETE</button>
                  <button className="builder-run" onClick={() => void runPipscript()} disabled={pipscriptRunning}>
                    {pipscriptRunning ? "RUNNING..." : "RUN"}
                  </button>
                </div>
                <div className="builder-hint">
                  <code>calculate(data)</code> receives the current chart candles by default. Add <code>data_requests()</code> to fetch multiple symbols/timeframes through the PIPSGOX Data Gateway. Saved scripts are stored in this browser. <b>APPLY</b> loads and runs the selected script; <b>DELETE</b> removes it.
                </div>
                <textarea value={pipscript} onChange={(event) => setPipscript(event.target.value)} spellCheck={false} />
                <div className="builder-output">
                  <strong>Output</strong>
                  <span>{pipscriptStatus}</span>
                </div>
              </div>
            ) : (
              <div className="settings-panel">
                <div className="settings-section">
                  <div className="settings-section-title">CHART THEME</div>
                  <label className="settings-field">
                    <span>Preset</span>
                    <select value={chartSettings.theme} onChange={(event) => applyChartPreset(event.target.value as ChartTheme)}>
                      <option value="pipsgox">PIPSGOX Dark</option>
                      <option value="classic">Classic Dark</option>
                      <option value="light">Clean Light</option>
                    </select>
                  </label>
                  <div className="settings-theme-save">
                    <select value="" onChange={(event) => loadChartTheme(event.target.value)}>
                      <option value="">Load saved theme</option>
                      {Object.keys(savedChartThemes).map((name) => <option key={name} value={name}>{name}</option>)}
                    </select>
                    <input value={chartThemeName} onChange={(event) => setChartThemeName(event.target.value)} placeholder="Theme name" maxLength={24} />
                    <button onClick={saveChartTheme}>SAVE</button>
                  </div>
                </div>
                <div className="settings-section">
                  <div className="settings-section-title">COLORS</div>
                  <div className="settings-color-grid">
                    {([
                      ["background", "Background"], ["grid", "Grid"], ["axis", "Price / date axis"],
                      ["candleUp", "Bullish candle"], ["candleDown", "Bearish candle"],
                      ["volumeUp", "Volume up"], ["volumeDown", "Volume down"],
                      ["ma50", "MA 50"], ["ma200", "MA 200"],
                    ] as Array<[keyof ChartColors, string]>).map(([key, label]) => (
                      <label className="settings-color-field" key={key}>
                        <span>{label}</span>
                        <span className="settings-color-control">
                          <input type="color" value={chartSettings.colors[key]} onChange={(event) => updateChartColor(key, event.target.value)} />
                          <code>{chartSettings.colors[key].toUpperCase()}</code>
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
                <div className="settings-section">
                  <div className="settings-section-title">CHART ELEMENTS</div>
                  <label className="settings-toggle"><span>Grid</span><input type="checkbox" checked={chartSettings.showGrid} onChange={(event) => updateChartSettings({ showGrid: event.target.checked })} /><i /></label>
                  <label className="settings-toggle"><span>Crosshair</span><input type="checkbox" checked={chartSettings.showCrosshair} onChange={(event) => updateChartSettings({ showCrosshair: event.target.checked })} /><i /></label>
                  <label className="settings-toggle"><span>Volume pane</span><input type="checkbox" checked={chartSettings.showVolume} onChange={(event) => updateChartSettings({ showVolume: event.target.checked })} /><i /></label>
                </div>
                <div className="settings-note">Changes apply immediately and are saved in this browser.</div>
              </div>
            )}
          </section>
        </div>
      )}
    </main>
  );
}

type AppErrorBoundaryState = {
  hasError: boolean;
  message: string;
  details: string;
};

class AppErrorBoundary extends Component<{ children: ReactNode }, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { hasError: false, message: "", details: "" };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return {
      hasError: true,
      message: error?.message || "PIPSGOX frontend encountered an unexpected error.",
      details: error?.stack || "",
    };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    let accountId: number | null = null;
    try {
      const raw = localStorage.getItem("pipsgox-selected-account");
      const value = raw ? Number(raw) : NaN;
      accountId = Number.isInteger(value) && value > 0 ? value : null;
    } catch {
      accountId = null;
    }

    void apiFetch("/api/diagnostics/frontend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: accountId,
        message: error?.message || "PIPSGOX frontend render failure.",
        stack: error?.stack || "",
        component_stack: info?.componentStack || "",
      }),
    }).catch(() => undefined);
  }

  render() {
    if (!this.state.hasError) return this.props.children;

    return (
      <main className="startup-screen">
        <div className="startup-failed-card">
          <div className="auth-brand">PIPSGOX</div>
          <div className="startup-failed-icon">×</div>
          <div className="startup-eyebrow">FRONTEND FAILURE</div>
          <h1>APPLICATION FAILED TO RENDER</h1>
          <p className="startup-lead">
            PIPSGOX stopped the main interface because a frontend component crashed. The failure has been recorded in Status Center.
          </p>
          <div className="startup-error-box">
            <strong>PIP-FRONTEND-RENDER-001</strong>
            <span>{this.state.message}</span>
          </div>
          <div className="startup-actions">
            <button className="status-primary" onClick={() => window.location.reload()}>RELOAD PIPSGOX</button>
          </div>
          <details className="startup-advanced">
            <summary>ADVANCED TECHNICAL DETAILS</summary>
            <pre className="diagnostic-technical">{this.state.details || "No stack trace available."}</pre>
          </details>
        </div>
      </main>
    );
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <AppErrorBoundary>
    {window.location.pathname === "/dev" ? <DevConsole /> : <App />}
  </AppErrorBoundary>,
);