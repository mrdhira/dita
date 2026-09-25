import { useSyncExternalStore } from "react";

function subscribe(onChange: () => void) {
  document.addEventListener("visibilitychange", onChange);
  return () => {
    document.removeEventListener("visibilitychange", onChange);
  };
}

export function useDocumentVisible(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => document.visibilityState !== "hidden",
    () => true,
  );
}

/** A poll interval that is off while the tab is hidden, so nothing polls for nobody. */
export function usePollInterval(ms: number): number | false {
  return useDocumentVisible() ? ms : false;
}

export const clock = (at: number) => new Date(at).toLocaleTimeString("en-GB", { hour12: false });
