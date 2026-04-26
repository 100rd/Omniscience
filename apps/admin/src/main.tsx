import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

/*
 * Set the dark theme on <html> BEFORE React paints the first frame.
 *
 * This avoids a flash of the light theme on initial load. The semantic
 * tokens in `index.css` switch via the `.dark` class on the documentElement.
 *
 * Issue #110 ships dark-as-default with no toggle. A subsequent issue will
 * introduce a localStorage-backed toggle and read it here.
 */
document.documentElement.classList.add("dark");

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Root element not found");

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
