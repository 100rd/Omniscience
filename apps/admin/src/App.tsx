import { BrowserRouter, Routes, Route } from "react-router-dom";
import { TokenProvider, useTokenContext } from "./context/TokenContext";
import { LoginScreen } from "./components/LoginScreen";
import { Layout } from "./components/Layout";
import { ToastContainer } from "./components/ToastContainer";
import { useToast } from "./hooks/useToast";
import { DashboardPage } from "./pages/DashboardPage";
import { SourcesPage } from "./pages/SourcesPage";
import { TokensPage } from "./pages/TokensPage";
import { IngestionPage } from "./pages/IngestionPage";
import { SearchPage } from "./pages/SearchPage";
import { FreshnessPage } from "./pages/FreshnessPage";

function AppInner() {
  const { token } = useTokenContext();
  const { toasts, addToast, removeToast } = useToast();

  if (!token) {
    return <LoginScreen />;
  }

  return (
    <>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route
              path="/sources"
              element={<SourcesPage addToast={addToast} />}
            />
            <Route
              path="/tokens"
              element={<TokensPage addToast={addToast} />}
            />
            <Route
              path="/ingestion"
              element={<IngestionPage addToast={addToast} />}
            />
            <Route
              path="/search"
              element={<SearchPage addToast={addToast} />}
            />
            <Route path="/freshness" element={<FreshnessPage />} />
          </Routes>
        </Layout>
      </BrowserRouter>
      <ToastContainer toasts={toasts} onRemove={removeToast} />
    </>
  );
}

export default function App() {
  return (
    <TokenProvider>
      <AppInner />
    </TokenProvider>
  );
}
