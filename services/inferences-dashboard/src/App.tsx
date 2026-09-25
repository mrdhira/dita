import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, RouterProvider, createBrowserRouter } from "react-router";
import { Layout } from "./components/Layout";
import { shouldRetry } from "./lib/errors";
import { DecidePage } from "./pages/DecidePage";
import { DecisionPage } from "./pages/DecisionPage";
import { EvalPage } from "./pages/EvalPage";
import { HistoryPage } from "./pages/HistoryPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { TemplatesPage } from "./pages/TemplatesPage";

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
      { index: true, element: <Navigate to="/decide" replace /> },
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
