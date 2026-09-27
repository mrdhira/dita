import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderAt } from "../test/render";
import { ActivityPage, JobsPage, ModelsPage, SettingsPage } from "./PlannedPage";

describe("the sections not built yet", () => {
  it.each([
    ["Models", <ModelsPage key="m" />, "no worker has a route that lists its registry"],
    ["Activity", <ActivityPage key="a" />, "there is no log API"],
    ["Jobs", <JobsPage key="j" />, "nothing in the repository trains"],
    ["Console settings", <SettingsPage key="s" />, "nothing is configurable yet"],
  ])("%s says what it will hold and what is missing", (title, element, missing) => {
    renderAt("/x", "/x", element);
    const section = screen.getByRole("region", { name: title });
    expect(section.textContent).toContain("Will hold:");
    expect(section.textContent).toContain(`Missing: ${missing}`);
    expect(section.querySelector("table, ul, ol, button")).toBeNull();
  });
});
