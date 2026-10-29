import { useEffect, useState } from "react";

// Minimal history-based router: three routes do not justify a routing dependency.
export type Route = "/" | "/privacy" | "/terms";
const ROUTES: Route[] = ["/", "/privacy", "/terms"];

function current(): Route | null {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  return (ROUTES as string[]).includes(path) ? (path as Route) : null;
}

export function navigate(to: Route) {
  if (window.location.pathname !== to) window.history.pushState({}, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo(0, 0);
}

export function useRoute(): Route | null {
  const [route, setRoute] = useState(current);
  useEffect(() => {
    const onPop = () => setRoute(current());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return route;
}

export function linkProps(to: Route) {
  return {
    href: to,
    onClick: (e: React.MouseEvent<HTMLAnchorElement>) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(to);
    },
  };
}
