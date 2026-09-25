import type { ReactNode } from "react";
import { Link } from "react-router";

interface Plan {
  title: string;
  holds: string;
  missing: string;
  today?: ReactNode;
}

/** A section that is not built yet says what it will hold and what it is waiting for. */
function PlannedPage({ title, holds, missing, today }: Plan) {
  return (
    <section aria-label={title} className="max-w-2xl space-y-3 text-sm">
      <h2 className="text-base font-semibold">{title}</h2>
      <p>
        <span className="font-semibold">Will hold: </span>
        {holds}
      </p>
      <p>
        <span className="font-semibold">Missing: </span>
        {missing}
      </p>
      {today && (
        <p>
          <span className="font-semibold">Today: </span>
          {today}
        </p>
      )}
    </section>
  );
}

const link = "text-indigo-700 underline";

export const ModelsPage = () => (
  <PlannedPage
    title="Models"
    holds="every model each service registers, with its pinned revision, its size on disk, and whether it is resident, on disk or not downloaded."
    missing="no worker has a route that lists its registry. It arrives with the read route in a later release."
    today={
      <>
        each service&apos;s resident model is on its own page, under{" "}
        <Link to="/" className={link}>
          Fleet
        </Link>
        .
      </>
    }
  />
);

export const ActivityPage = () => (
  <PlannedPage
    title="Activity"
    holds="recent requests and refusals across the services, from the gateway's own request log."
    missing="there is no log API. The gateway logs JSON and the workers plain text, readable only in Dozzle or with docker logs."
    today={
      <>
        the decisions system-one has made, and their corrections, are under{" "}
        <Link to="/decisions" className={link}>
          History
        </Link>
        .
      </>
    }
  />
);

export const JobsPage = () => (
  <PlannedPage
    title="Jobs"
    holds="long-running work with a state, a start and an end: evaluations now, and training runs once a trainer exists."
    missing="nothing in the repository trains or fine-tunes, so there is no training job to show, and this page will not draw one."
    today={
      <>
        uploaded evaluations and their scores are under{" "}
        <Link to="/eval" className={link}>
          Eval
        </Link>
        .
      </>
    }
  />
);

export const SettingsPage = () => (
  <PlannedPage
    title="Console settings"
    holds="the console's own preferences, such as the theme."
    missing="nothing is configurable yet. Refresh intervals are fixed: Fleet every 5 s, a service every 10 s, its metrics every 30 s, and none while the tab is hidden."
  />
);
