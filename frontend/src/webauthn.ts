const base64urlToBuffer = (value: string): ArrayBuffer => {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = window.atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
};

const bufferToBase64url = (value: ArrayBuffer): string => {
  const bytes = new Uint8Array(value);
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
};

export function passkeySupported(): boolean {
  return window.isSecureContext && "credentials" in navigator && "PublicKeyCredential" in window;
}

function registrationOptionsForBrowser(options: Record<string, any>): PublicKeyCredentialCreationOptions {
  return {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    user: {
      ...options.user,
      id: base64urlToBuffer(options.user.id),
    },
    excludeCredentials: Array.isArray(options.excludeCredentials)
      ? options.excludeCredentials.map((item: any) => ({
          ...item,
          id: base64urlToBuffer(item.id),
        }))
      : undefined,
  };
}

function authenticationOptionsForBrowser(options: Record<string, any>): PublicKeyCredentialRequestOptions {
  return {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    allowCredentials: Array.isArray(options.allowCredentials)
      ? options.allowCredentials.map((item: any) => ({
          ...item,
          id: base64urlToBuffer(item.id),
        }))
      : undefined,
  };
}

function credentialToJSON(credential: PublicKeyCredential): Record<string, any> {
  const response = credential.response;
  const base: Record<string, any> = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
    },
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
  };

  if (response instanceof AuthenticatorAttestationResponse) {
    base.response.attestationObject = bufferToBase64url(response.attestationObject);
    const transports = response.getTransports?.();
    if (transports?.length) base.response.transports = transports;
  } else if (response instanceof AuthenticatorAssertionResponse) {
    base.response.authenticatorData = bufferToBase64url(response.authenticatorData);
    base.response.signature = bufferToBase64url(response.signature);
    base.response.userHandle = response.userHandle ? bufferToBase64url(response.userHandle) : null;
  }

  return base;
}

async function jsonRequest(path: string, body?: unknown): Promise<Record<string, any>> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(String(payload?.detail || "Passkey request failed."));
  }
  return payload;
}

export async function registerPasskey(): Promise<void> {
  if (!passkeySupported()) {
    throw new Error("Passkeys require a secure HTTPS connection and a browser with WebAuthn support.");
  }

  const options = await jsonRequest("/api/auth/passkey/register/options");
  const credential = await navigator.credentials.create({
    publicKey: registrationOptionsForBrowser(options),
  });

  if (!(credential instanceof PublicKeyCredential)) {
    throw new Error("Passkey registration was cancelled.");
  }

  await jsonRequest("/api/auth/passkey/register/verify", {
    credential: credentialToJSON(credential),
  });
}

export async function loginWithPasskey(): Promise<Record<string, any>> {
  if (!passkeySupported()) {
    throw new Error("Passkeys require a secure HTTPS connection and a browser with WebAuthn support.");
  }

  const options = await jsonRequest("/api/auth/passkey/login/options");
  const credential = await navigator.credentials.get({
    publicKey: authenticationOptionsForBrowser(options),
  });

  if (!(credential instanceof PublicKeyCredential)) {
    throw new Error("Passkey sign-in was cancelled.");
  }

  return jsonRequest("/api/auth/passkey/login/verify", {
    credential: credentialToJSON(credential),
  });
}


export async function signupWithPasskey(username: string): Promise<Record<string, any>> {
  if (!passkeySupported()) {
    throw new Error("Passkeys require a secure HTTPS connection and a browser with WebAuthn support.");
  }
  const options = await jsonRequest("/api/auth/passkey/signup/options", { username });
  const credential = await navigator.credentials.create({
    publicKey: registrationOptionsForBrowser(options),
  });
  if (!(credential instanceof PublicKeyCredential)) {
    throw new Error("Passkey signup was cancelled.");
  }
  return jsonRequest("/api/auth/passkey/signup/verify", {
    credential: credentialToJSON(credential),
  });
}
