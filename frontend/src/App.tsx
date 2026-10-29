import { useEffect } from "react";
import { Privacy } from "./pages/Privacy";
import { Terms } from "./pages/Terms";
import { Workspace } from "./pages/Workspace";
import { linkProps, useRoute, type Route } from "./router";

const TITLES: Record<Route, string> = { "/": "ScaleTrace", "/privacy": "Privacy policy | ScaleTrace", "/terms": "Terms and conditions | ScaleTrace" };

function NavLink({ to, children }: { to: Route; children: string }) {
  const route = useRoute();
  return (
    <a {...linkProps(to)} aria-current={route === to ? "page" : undefined}>
      {children}
    </a>
  );
}

export function App() {
  const route = useRoute();
  useEffect(() => {
    document.title = route ? TITLES[route] : "Page not found | ScaleTrace";
  }, [route]);

  return (
    <>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" {...linkProps("/")}>
            <img src="/favicon.svg" alt="" />
            ScaleTrace <small>scalability diagnosis for FastAPI backends</small>
          </a>
          <nav className="nav" aria-label="Main">
            <NavLink to="/">Analysis</NavLink>
            <NavLink to="/privacy">Privacy</NavLink>
            <NavLink to="/terms">Terms</NavLink>
          </nav>
        </div>
      </header>
      <main id="main" className="page" tabIndex={-1}>
        {route === "/" && <Workspace />}
        {route === "/privacy" && <Privacy />}
        {route === "/terms" && <Terms />}
        {route === null && (
          <div className="panel doc">
            <h1>Page not found</h1>
            <p>
              This page does not exist. Go to the <a {...linkProps("/")}>analysis</a>.
            </p>
          </div>
        )}
      </main>
      <footer className="footer">
        <div className="footer-inner">
          <span>ScaleTrace research prototype, version 0.1.0. Results are diagnostic candidates, not guarantees.</span>
          <nav aria-label="Legal">
            <a {...linkProps("/privacy")}>Privacy policy</a>
            <a {...linkProps("/terms")}>Terms and conditions</a>
          </nav>
        </div>
      </footer>
    </>
  );
}
