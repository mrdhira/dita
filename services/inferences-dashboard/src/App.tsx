import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, type ComponentType } from "react";
import { RouterProvider, createBrowserRouter } from "react-router";
import { Layout } from "./components/Layout";
import { shouldRetry } from "./lib/errors";
import { FleetPage } from "./pages/FleetPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ActivityPage, JobsPage, ModelsPage, SettingsPage } from "./pages/PlannedPage";

const named = <K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K) =>
  lazy(() => load().then((m) => ({ default: m[name] })));

const ServicePage = named(() => import("./pages/service/ServicePage"), "ServicePage");
const DecidePage = named(() => import("./pages/DecidePage"), "DecidePage");
const DecisionPage = named(() => import("./pages/DecisionPage"), "DecisionPage");
const HistoryPage = named(() => import("./pages/HistoryPage"), "HistoryPage");
const TemplatesPage = named(() => import("./pages/TemplatesPage"), "TemplatesPage");
const EvalPage = named(() => import("./pages/EvalPage"), "EvalPage");

export function makeQueryClient() {
  // A write is never retried: a second decide would be a second prediction.
  return new QueryClient({
    defaultOptions: { queries: { retry: shouldRetry }, mutations: { retry: false } },
  });
}

export const routes = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <FleetPage /> },
      { path: "services/:id", element: <ServicePage /> },
      { path: "services/:id/:tab", element: <ServicePage /> },
      { path: "models", element: <ModelsPage /> },
      { path: "activity", element: <ActivityPage /> },
      { path: "jobs", element: <JobsPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "decide", element: <DecidePage /> },
      { path: "decisions", element: <HistoryPage /> },
      { path: "decisions/:id", element: <DecisionPage /> },
      { path: "templates", element: <TemplatesPage /> },
      { path: "eval", element: <EvalPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

const router = createBrowserRouter(routes);
const client = makeQueryClient();

export function App() {
  return (
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
