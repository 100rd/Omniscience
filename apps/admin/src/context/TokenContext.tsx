import {
  createContext,
  useContext,
  useState,
  useEffect,
  ReactNode,
} from "react";
import { ApiClient } from "../api/client";

/**
 * Auth strategy: httpOnly session cookie (set by POST /api/v1/admin/session).
 *
 * The plaintext API token is never stored in localStorage or any JS-readable
 * storage after login.  On mount we probe /health to determine whether the
 * browser already has a valid session cookie.  On login we POST the token
 * to the session endpoint which sets the httpOnly + Secure + SameSite=Strict
 * cookie server-side.  On logout we DELETE the session endpoint to clear both
 * cookies.
 *
 * The in-memory `authenticated` flag is the only UI state kept in React; the
 * actual credential lives exclusively in the httpOnly cookie.
 */

interface TokenContextValue {
  authenticated: boolean;
  client: ApiClient;
  login: (token: string) => Promise<void>;
  logout: () => Promise<void>;
}

const TokenContext = createContext<TokenContextValue | null>(null);

const client = new ApiClient();

export function TokenProvider({ children }: { children: ReactNode }) {
  const [authenticated, setAuthenticated] = useState<boolean>(false);
  const [probed, setProbed] = useState<boolean>(false);

  // Probe on mount to restore session from existing httpOnly cookie.
  useEffect(() => {
    client
      .health()
      .then(() => setAuthenticated(true))
      .catch(() => setAuthenticated(false))
      .finally(() => setProbed(true));
  }, []);

  const login = async (token: string): Promise<void> => {
    await client.createSession(token);
    setAuthenticated(true);
  };

  const logout = async (): Promise<void> => {
    try {
      await client.deleteSession();
    } finally {
      setAuthenticated(false);
    }
  };

  // Don't render children until the session probe completes to avoid
  // a flash of the login screen on page reload with a valid cookie.
  if (!probed) {
    return null;
  }

  return (
    <TokenContext.Provider value={{ authenticated, client, login, logout }}>
      {children}
    </TokenContext.Provider>
  );
}

export function useTokenContext(): TokenContextValue {
  const ctx = useContext(TokenContext);
  if (!ctx) throw new Error("useTokenContext must be used within TokenProvider");
  return ctx;
}
