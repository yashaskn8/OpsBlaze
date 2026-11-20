"""Browser-only OIDC authorization-code/PKCE session.

The identity provider validates the user's login. OpsBlaze validates the
resulting access token again on every API request. No password flow.
"""
import { UserManager, WebStorageStateStore } from "oidc-client-ts";

const authority = import.meta.env.VITE_OIDC_AUTHORITY?.trim();
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID?.trim();
const scope = import.meta.env.VITE_OIDC_SCOPE?.trim() || "openid profile";

if ((authority && !clientId) || (clientId && !authority)) {
  throw new Error("Both VITE_OIDC_AUTHORITY and VITE_OIDC_CLIENT_ID are required");
}

if (authority && !authority.startsWith("https://") &&
    !(import.meta.env.DEV && authority.startsWith("http://localhost:"))) {
  throw new Error("OIDC authority must use HTTPS");
}

export const oidcManager = authority && clientId
  ? new UserManager({
      authority,
      client_id: clientId,
      redirect_uri: window.location.origin + "/auth/callback",
      response_type: "code",
      scope,
      loadUserInfo: false,
      automaticSilentRenew: false,
      monitorSession: false,
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    })
  : null;

export const productionIdentityMode = oidcManager !== null;

export async function currentAccessToken(): Promise<string | null> {
  if (!oidcManager) return null;
  const user = await oidcManager.getUser();
  if (!user || user.expired || !user.access_token) return null;
  return user.access_token;
}

export async function finishLoginCallback(): Promise<string> {
  if (!oidcManager) throw new Error("OIDC client is not configured");
  const user = await oidcManager.signinRedirectCallback();
  window.history.replaceState(null, "", "/");
  if (user.expired || !user.access_token) {
    throw new Error("Identity provider did not return a valid access token");
  }
  return user.access_token;
}

export async function startLogin(): Promise<void> {
  if (!oidcManager) throw new Error("OIDC client is not configured");
  await oidcManager.signinRedirect();
}

export async function endLocalSession(): Promise<void> {
  await oidcManager?.removeUser();
}
