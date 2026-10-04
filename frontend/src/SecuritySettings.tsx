import { useEffect, useState } from "react";
import { apiFetch } from "./api";
import { passkeySupported, registerPasskey } from "./webauthn";

type PasskeyItem = {
  id: number;
  device_name: string | null;
  created_at: string;
  last_used_at: string | null;
};

type TotpStatus = {
  enabled: boolean;
  configured: boolean;
};

type SessionItem = {
  id: number;
  created_at: string;
  last_seen_at: string | null;
  expires_at: string;
  user_agent: string | null;
  current: boolean;
};

type EmailStatus = {
  email: string | null;
  verified: boolean;
};

type TotpSetup = {
  secret: string;
  otpauth_uri: string;
  qr_code: string | null;
  expires_at: number;
};

type SecuritySettingsProps = {
  onClose: () => void;
};

export function SecuritySettings({ onClose }: SecuritySettingsProps) {
  const [passkeys, setPasskeys] = useState<PasskeyItem[]>([]);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsBusy, setSessionsBusy] = useState(false);
  const [totp, setTotp] = useState<TotpStatus>({ enabled: false, configured: false });
  const [emailStatus, setEmailStatus] = useState<EmailStatus>({ email: null, verified: false });
  const [emailInput, setEmailInput] = useState("");
  const [emailBusy, setEmailBusy] = useState(false);
  const [recoveryRemaining, setRecoveryRemaining] = useState(0);
  const [totpSetup, setTotpSetup] = useState<TotpSetup | null>(null);
  const [totpCode, setTotpCode] = useState("");
  const [totpDisableCode, setTotpDisableCode] = useState("");
  const [recoveryTotpCode, setRecoveryTotpCode] = useState("");
  const [generatedRecoveryCodes, setGeneratedRecoveryCodes] = useState<string[]>([]);
  const [showRecoveryGenerate, setShowRecoveryGenerate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [totpBusy, setTotpBusy] = useState(false);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const loadSecurityData = async () => {
    setLoading(true);
    setError("");
    try {
      const [passkeyResponse, totpResponse, recoveryResponse, emailResponse, sessionsResponse] = await Promise.all([
        apiFetch("/api/auth/passkeys", { cache: "no-store" }),
        apiFetch("/api/auth/totp", { cache: "no-store" }),
        apiFetch("/api/auth/recovery-codes", { cache: "no-store" }),
        apiFetch("/api/auth/email", { cache: "no-store" }),
        apiFetch("/api/security/sessions", { cache: "no-store" }),
      ]);
      const passkeyPayload = await passkeyResponse.json().catch(() => ({}));
      const totpPayload = await totpResponse.json().catch(() => ({}));
      const recoveryPayload = await recoveryResponse.json().catch(() => ({}));
      const emailPayload = await emailResponse.json().catch(() => ({}));
      const sessionsPayload = await sessionsResponse.json().catch(() => ({}));
      if (!passkeyResponse.ok) throw new Error(String(passkeyPayload?.detail || "Could not load passkeys."));
      if (!totpResponse.ok) throw new Error(String(totpPayload?.detail || "Could not load authenticator status."));
      if (!recoveryResponse.ok) throw new Error(String(recoveryPayload?.detail || "Could not load recovery code status."));
      if (!emailResponse.ok) throw new Error(String(emailPayload?.detail || "Could not load email status."));
      if (!sessionsResponse.ok) throw new Error(String(sessionsPayload?.detail || "Could not load active sessions."));
      setPasskeys(Array.isArray(passkeyPayload.passkeys) ? passkeyPayload.passkeys : []);
      setTotp({
        enabled: Boolean(totpPayload.enabled),
        configured: Boolean(totpPayload.configured),
      });
      setRecoveryRemaining(Number(recoveryPayload.remaining || 0));
      const nextEmail = {
        email: emailPayload?.email ? String(emailPayload.email) : null,
        verified: Boolean(emailPayload?.verified),
      };
      setEmailStatus(nextEmail);
      setEmailInput(nextEmail.email || "");
      setSessions(Array.isArray(sessionsPayload.sessions) ? sessionsPayload.sessions : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load security settings.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadSecurityData();
  }, []);

  const revokeSession = async (session: SessionItem) => {
    const label = session.current ? "this device" : "this session";
    if (!window.confirm(`Revoke ${label}? You will be signed out if it is the current session.`)) return;

    setSessionsBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/security/sessions/" + session.id, { method: "DELETE" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not revoke session."));
      if (session.current) {
        window.location.reload();
        return;
      }
      setSessions((items) => items.filter((item) => item.id !== session.id));
      setMessage("Session revoked.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not revoke session.");
    } finally {
      setSessionsBusy(false);
    }
  };

  const logoutOtherSessions = async () => {
    if (!window.confirm("Log out all other active sessions? This will not sign out your current device.")) return;

    setSessionsBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/security/sessions/logout-others", { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not log out other sessions."));
      const count = Number(payload.revoked_sessions || 0);
      setSessions((items) => items.filter((item) => item.current));
      setMessage(count ? `Logged out ${count} other session${count === 1 ? "" : "s"}.` : "No other active sessions were found.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not log out other sessions.");
    } finally {
      setSessionsBusy(false);
    }
  };

  const formatSessionDevice = (userAgent: string | null) => {
    if (!userAgent) return "Unknown browser/device";
    if (/android/i.test(userAgent)) return /chrome/i.test(userAgent) ? "Chrome on Android" : "Android device";
    if (/iphone|ipad/i.test(userAgent)) return /safari/i.test(userAgent) ? "Safari on iPhone/iPad" : "iPhone/iPad";
    if (/windows/i.test(userAgent)) return /edge/i.test(userAgent) ? "Edge on Windows" : /chrome/i.test(userAgent) ? "Chrome on Windows" : "Windows browser";
    if (/macintosh|mac os/i.test(userAgent)) return /chrome/i.test(userAgent) ? "Chrome on macOS" : "macOS browser";
    if (/linux/i.test(userAgent)) return /chrome/i.test(userAgent) ? "Chrome on Linux" : "Linux browser";
    return "Browser/device";
  };

  const formatSessionTime = (value: string | null) => value ? new Date(value).toLocaleString() : "Unknown";

  const saveEmail = async () => {
    const email = emailInput.trim().toLowerCase();
    if (!email || !email.includes("@")) {
      setError("Enter a valid email address.");
      return;
    }

    setEmailBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/auth/email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not send verification email."));
      setEmailStatus({ email: String(payload.email || email), verified: false });
      setMessage("Verification email sent. Open the link in your email to verify this address.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send verification email.");
    } finally {
      setEmailBusy(false);
    }
  };

  const resendEmail = async () => {
    setEmailBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/auth/email/resend", { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not resend verification email."));
      setMessage("Verification email sent again.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resend verification email.");
    } finally {
      setEmailBusy(false);
    }
  };

  const addPasskey = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await registerPasskey();
      setMessage("Passkey added successfully. You can now sign in with your device biometric, PIN, or security key.");
      await loadSecurityData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add passkey.");
    } finally {
      setBusy(false);
    }
  };

  const removePasskey = async (id: number) => {
    if (!window.confirm("Remove this passkey? You will no longer be able to use it to sign in to PIPSGOX.")) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/auth/passkeys/" + id, { method: "DELETE" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not remove passkey."));
      setPasskeys((items) => items.filter((item) => item.id !== id));
      setMessage("Passkey removed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove passkey.");
    } finally {
      setBusy(false);
    }
  };

  const startTotpSetup = async () => {
    setTotpBusy(true);
    setError("");
    setMessage("");
    setTotpSetup(null);
    setTotpCode("");
    try {
      const response = await apiFetch("/api/auth/totp/setup", { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not start authenticator setup."));
      setTotpSetup(payload as TotpSetup);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start authenticator setup.");
    } finally {
      setTotpBusy(false);
    }
  };

  const confirmTotpSetup = async () => {
    const code = totpCode.replace(/\s+/g, "");
    if (!/^\d{6}$/.test(code)) {
      setError("Enter the current 6-digit authenticator code.");
      return;
    }

    setTotpBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/auth/totp/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not confirm authenticator setup."));
      setTotp({ enabled: true, configured: true });
      setTotpSetup(null);
      setTotpCode("");
      setRecoveryRemaining(0);
      setShowRecoveryGenerate(true);
      setMessage("Authenticator app enabled. Generate your recovery codes and store them somewhere safe.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not confirm authenticator setup.");
    } finally {
      setTotpBusy(false);
    }
  };

  const generateRecoveryCodes = async () => {
    const code = recoveryTotpCode.replace(/\s+/g, "");
    if (!/^\d{6}$/.test(code)) {
      setError("Enter the current 6-digit authenticator code.");
      return;
    }

    setRecoveryBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/auth/recovery-codes/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not generate recovery codes."));
      const codes = Array.isArray(payload.codes) ? payload.codes.map(String) : [];
      if (codes.length !== 10) throw new Error("The server did not return the expected recovery code set.");
      setGeneratedRecoveryCodes(codes);
      setRecoveryRemaining(codes.length);
      setRecoveryTotpCode("");
      setShowRecoveryGenerate(false);
      setMessage("New recovery codes generated. The previous recovery codes are now invalid.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not generate recovery codes.");
    } finally {
      setRecoveryBusy(false);
    }
  };

  const disableTotp = async () => {
    const code = totpDisableCode.replace(/\s+/g, "");
    if (!/^\d{6}$/.test(code)) {
      setError("Enter the current 6-digit authenticator code.");
      return;
    }
    if (!window.confirm("Disable the authenticator app? Existing recovery codes will also be invalidated.")) return;

    setTotpBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await apiFetch("/api/auth/totp/disable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(String(payload?.detail || "Could not disable authenticator."));
      setTotp({ enabled: false, configured: false });
      setRecoveryRemaining(0);
      setTotpDisableCode("");
      setGeneratedRecoveryCodes([]);
      setShowRecoveryGenerate(false);
      setMessage("Authenticator app disabled and recovery codes invalidated.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disable authenticator.");
    } finally {
      setTotpBusy(false);
    }
  };

  const copyText = async (value: string, successMessage: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setMessage(successMessage);
    } catch {
      setError("Could not copy to clipboard. You can select and copy the value manually.");
    }
  };

  const copyRecoveryCodes = async () => {
    await copyText(generatedRecoveryCodes.join("\n"), "All recovery codes copied.");
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section className="modal security-modal" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <strong>SECURITY</strong>
          <button onClick={onClose}>CLOSE</button>
        </div>

        <div className="security-panel">
          <div className="security-heading">
            <div>
              <div className="security-title">Active Sessions</div>
              <div className="security-description">
                Review where your PIPSGOX account is signed in. Session tokens and IP addresses are never shown.
              </div>
            </div>
            <span className="security-status enabled">{sessions.length} ACTIVE</span>
          </div>

          {loading ? (
            <div className="security-empty">Loading active sessions...</div>
          ) : sessions.length === 0 ? (
            <div className="security-empty">No active sessions.</div>
          ) : (
            <div className="security-session-list">
              {sessions.map((session) => (
                <div className={"security-session" + (session.current ? " current" : "")} key={session.id}>
                  <div className="security-session-copy">
                    <div>
                      <strong>{formatSessionDevice(session.user_agent)}</strong>
                      {session.current && <span className="security-session-current">CURRENT</span>}
                    </div>
                    <span>Last active {formatSessionTime(session.last_seen_at)}</span>
                    <span>Created {formatSessionTime(session.created_at)}</span>
                    <span>Expires {formatSessionTime(session.expires_at)}</span>
                    <span>IP address protected</span>
                  </div>
                  <button className={session.current ? "security-danger" : "security-secondary"} disabled={sessionsBusy} onClick={() => void revokeSession(session)}>
                    {session.current ? "SIGN OUT" : "REVOKE"}
                  </button>
                </div>
              ))}
            </div>
          )}

          {sessions.length > 1 && (
            <button className="security-secondary" disabled={sessionsBusy} onClick={() => void logoutOtherSessions()}>
              {sessionsBusy ? "WORKING..." : "LOG OUT OTHER SESSIONS"}
            </button>
          )}

          <div className="security-note">
            Sessions expire automatically after 12 hours. Revoking a session takes effect immediately.
          </div>

          <div className="security-divider" />
          <div className="security-heading">
            <div>
              <div className="security-title">Passkeys</div>
              <div className="security-description">
                Use your Android fingerprint, face unlock, device PIN, or a compatible security key to sign in without typing your password.
              </div>
            </div>
            <span className="security-recommended">RECOMMENDED</span>
          </div>

          {!passkeySupported() && (
            <div className="security-warning">
              Passkeys are not available in this browser/origin. Use HTTPS or a supported localhost origin.
            </div>
          )}

          {error && <div className="security-error">{error}</div>}
          {message && <div className="security-success">{message}</div>}

          <button className="security-primary" disabled={busy || !passkeySupported()} onClick={() => void addPasskey()}>
            {busy ? "WORKING..." : "ADD PASSKEY"}
          </button>

          <div className="security-section-label">REGISTERED PASSKEYS</div>
          {loading ? (
            <div className="security-empty">Loading passkeys...</div>
          ) : passkeys.length === 0 ? (
            <div className="security-empty">No passkeys registered yet.</div>
          ) : (
            <div className="security-passkey-list">
              {passkeys.map((passkey) => (
                <div className="security-passkey" key={passkey.id}>
                  <div>
                    <strong>{passkey.device_name || "Passkey"}</strong>
                    <span>Added {new Date(passkey.created_at).toLocaleString()}</span>
                    {passkey.last_used_at && <span>Last used {new Date(passkey.last_used_at).toLocaleString()}</span>}
                  </div>
                  <button disabled={busy} onClick={() => void removePasskey(passkey.id)}>REMOVE</button>
                </div>
              ))}
            </div>
          )}

          <div className="security-note">
            PIPSGOX never receives or stores your fingerprint, face data, or device PIN. Your device/authenticator performs that verification locally.
          </div>

          <div className="security-divider" />

          <div className="security-heading">
            <div>
              <div className="security-title">Email Address</div>
              <div className="security-description">
                Add an email address for account verification and future password recovery.
              </div>
            </div>
            <span className={emailStatus.verified ? "security-status enabled" : "security-status"}>
              {emailStatus.verified ? "VERIFIED" : emailStatus.email ? "UNVERIFIED" : "NOT SET"}
            </span>
          </div>

          <div className="security-note">
            PIPSGOX sends the verification email for you. Email delivery settings are managed by the PIPSGOX server and are never entered here.
          </div>

          <div className="security-code-row">
            <input
              type="email"
              value={emailInput}
              onChange={(event) => setEmailInput(event.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              disabled={emailBusy}
              aria-label="Email address"
            />
            <button disabled={emailBusy} onClick={() => void saveEmail()}>
              {emailBusy ? "SENDING..." : emailStatus.email ? "VERIFY EMAIL" : "ADD EMAIL"}
            </button>
          </div>

          {emailStatus.email && !emailStatus.verified && (
            <div className="security-recovery-generate">
              <span>Verification is required before this address can be used for account recovery.</span>
              <button className="security-link-button" disabled={emailBusy} onClick={() => void resendEmail()}>
                RESEND VERIFICATION EMAIL
              </button>
            </div>
          )}

          <div className="security-divider" />

          <div className="security-heading">
            <div>
              <div className="security-title">Authenticator App</div>
              <div className="security-description">
                Add a TOTP authenticator as an optional second factor for password sign-in. Codes change every 30 seconds.
              </div>
            </div>
            <span className={totp.enabled ? "security-status enabled" : "security-status"}>
              {totp.enabled ? "ENABLED" : "OFF"}
            </span>
          </div>

          {!totp.enabled && !totpSetup && (
            <button className="security-secondary" disabled={totpBusy} onClick={() => void startTotpSetup()}>
              {totpBusy ? "PREPARING..." : "SET UP AUTHENTICATOR"}
            </button>
          )}

          {totpSetup && (
            <div className="security-totp-setup">
              <div className="security-totp-step">
                <strong>1. Add PIPSGOX to your authenticator app</strong>
                <span>Scan the QR code, or enter the setup key manually.</span>
              </div>

              {totpSetup.qr_code && (
                <div className="security-qr-wrap">
                  <img src={totpSetup.qr_code} alt="PIPSGOX authenticator setup QR code" />
                </div>
              )}

              <div className="security-secret-wrap">
                <span>SETUP KEY</span>
                <code>{totpSetup.secret}</code>
                <button onClick={() => void copyText(totpSetup.secret, "Setup key copied.")}>COPY KEY</button>
              </div>

              <div className="security-totp-step">
                <strong>2. Verify the current 6-digit code</strong>
                <span>After adding the account, enter the code shown by your authenticator.</span>
              </div>

              <div className="security-code-row">
                <input
                  value={totpCode}
                  onChange={(event) => setTotpCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  placeholder="000000"
                  aria-label="Authenticator code"
                />
                <button disabled={totpBusy} onClick={() => void confirmTotpSetup()}>
                  {totpBusy ? "VERIFYING..." : "VERIFY & ENABLE"}
                </button>
              </div>

              <button className="security-link-button" disabled={totpBusy} onClick={() => { setTotpSetup(null); setTotpCode(""); }}>
                Cancel setup
              </button>
            </div>
          )}

          {totp.enabled && (
            <>
              <div className="security-totp-enabled">
                <div className="security-enabled-copy">
                  <strong>Authenticator protection is active.</strong>
                  <span>Password sign-in requires your current 6-digit authenticator code. Passkey sign-in remains available separately.</span>
                </div>
                <div className="security-recovery-summary">
                  <span>RECOVERY CODES</span>
                  <strong>{recoveryRemaining} remaining</strong>
                </div>
                {recoveryRemaining === 0 && (
                  <div className="security-warning">
                    No unused recovery codes are available. Generate a new recovery code set before you need one.
                  </div>
                )}
                <button className="security-secondary" disabled={recoveryBusy} onClick={() => { setShowRecoveryGenerate(true); setRecoveryTotpCode(""); setError(""); }}>
                  {recoveryRemaining > 0 ? "GENERATE NEW RECOVERY CODES" : "GENERATE RECOVERY CODES"}
                </button>
                {showRecoveryGenerate && (
                  <div className="security-recovery-generate">
                    <span>Confirm with your current authenticator code to replace the existing recovery codes.</span>
                    <div className="security-code-row">
                      <input
                        value={recoveryTotpCode}
                        onChange={(event) => setRecoveryTotpCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        maxLength={6}
                        placeholder="000000"
                        aria-label="Current authenticator code"
                      />
                      <button disabled={recoveryBusy} onClick={() => void generateRecoveryCodes()}>
                        {recoveryBusy ? "GENERATING..." : "GENERATE"}
                      </button>
                    </div>
                    <button className="security-link-button" disabled={recoveryBusy} onClick={() => { setShowRecoveryGenerate(false); setRecoveryTotpCode(""); }}>
                      Cancel
                    </button>
                  </div>
                )}
                <div className="security-code-row">
                  <input
                    value={totpDisableCode}
                    onChange={(event) => setTotpDisableCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    placeholder="Current code"
                    aria-label="Current authenticator code"
                  />
                  <button className="security-danger" disabled={totpBusy} onClick={() => void disableTotp()}>
                    {totpBusy ? "WORKING..." : "DISABLE"}
                  </button>
                </div>
              </div>

              {generatedRecoveryCodes.length > 0 && (
                <div className="security-recovery-display">
                  <div className="security-recovery-title">
                    <strong>Save these recovery codes</strong>
                    <span>They are shown once. Each code can be used once. Regenerating replaces this entire set.</span>
                  </div>
                  <div className="security-recovery-grid">
                    {generatedRecoveryCodes.map((code) => <code key={code}>{code}</code>)}
                  </div>
                  <button className="security-primary" onClick={() => void copyRecoveryCodes()}>
                    COPY ALL CODES
                  </button>
                  <div className="security-note">Store them offline or in a trusted password manager. Do not share them.</div>
                </div>
              )}
            </>
          )}

          {!totp.enabled && !totpSetup && (
            <div className="security-note">
              Optional. Passkeys remain a separate passwordless sign-in method and are not stored as TOTP secrets.
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
