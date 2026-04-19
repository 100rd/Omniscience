import {
  createContext,
  useContext,
  useState,
  useEffect,
  ReactNode,
} from "react";
import { ApiClient } from "../api/client";

const STORAGE_KEY = "omniscience_admin_token";

interface TokenContextValue {
  token: string | null;
  client: ApiClient;
  setToken: (t: string | null) => void;
  logout: () => void;
}

const TokenContext = createContext<TokenContextValue | null>(null);

const client = new ApiClient(null);

export function TokenProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() =>
    localStorage.getItem(STORAGE_KEY)
  );

  useEffect(() => {
    client.setToken(token);
    if (token) {
      localStorage.setItem(STORAGE_KEY, token);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  }, [token]);

  const setToken = (t: string | null) => setTokenState(t);
  const logout = () => setTokenState(null);

  return (
    <TokenContext.Provider value={{ token, client, setToken, logout }}>
      {children}
    </TokenContext.Provider>
  );
}

export function useTokenContext(): TokenContextValue {
  const ctx = useContext(TokenContext);
  if (!ctx) throw new Error("useTokenContext must be used within TokenProvider");
  return ctx;
}
