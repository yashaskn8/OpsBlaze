import {
  InMemoryWebStorage, UserManager, WebStorageStateStore
} from "oidc-client-ts";

const authority = (import.meta.env.VITE_OIDC_AUTHORITY || "").trim();
const clientId = (import.meta.env.VITE_OIDC_CLIENT_ID || "").trim();
const scope = (import.meta.env.VITE_OIDC_SCOPE || "").trim();

export const usingOidc = Boolean(authority || clientId || scope);
const valid = usingOidc && Boolean(authority && clientId && scope)
  && (() => {
    try {
      const url = new URL(authority);
      return url.protocol === "https:" && !url.username && !url.password && !url.hash;
    } catch {
      return false;
    }
  })();

// All state needed for a redirect roundtrip goes into sessionStorage.
// Access tokens themselves remain in the in-memory user store.
export const authManager = valid ? new UserManager({
  authority,
  client_id: clientId,
  redirect_uri: window.location.origin + "/auth/callback",
  post_logout_redirect_uri: window.location.origin + "/",
  response_type: "code",
  scope,
  loadUserInfo: false,
  automaticSilentRenew: false,
  monitorSession: false,
  userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
  stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
}) : null;

export const oidcConfigError = usingOidc && !valid
  ? "OIDC requires an HTTPS authority, public client ID, and requested scopes."
  : "";
