import { useEffect, useState } from "react";
import { workspaceLocation, type WorkspaceId } from "./workspacePaths";

const workspaces: Record<WorkspaceId, { label: string; title: string; description: string }> = {
  "incident-snapshot": { label: "Incidents", title: "Incident operations", description: "Admit incidents, inspect investigations, and review simulator recovery." },
  "incident-evidence": { label: "Evidence and trace", title: "Evidence and trace", description: "Follow the selected incident from source evidence to durable effects." },
  "queue-health": { label: "Queue health", title: "Queue health", description: "Inspect tenant-scoped backlog, age, and unresolved effects." },
  "experience-review": { label: "Experience review", title: "Experience review", description: "Inspect failed experience projections and record authorized follow-up." },
  "replay-lab": { label: "Replay lab", title: "Replay lab", description: "Compare two deterministic strategies on the same simulator fixture." },
};

export function useWorkspace() {
  const [hash, setHash] = useState(() => window.location.hash);
  const { workspace, target } = workspaceLocation(hash);

  useEffect(() => {
    const update = () => setHash(window.location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);

  useEffect(() => {
    if (!target) return;
    const section = document.getElementById(target);
    if (!section || section.closest("[hidden]")) return;
    section.focus({ preventScroll: true });
    if (workspace === target) {
      window.scrollTo({ top: 0 });
      if (section.getBoundingClientRect().top < window.innerHeight) return;
    }
    section.scrollIntoView({ block: "start" });
  }, [target, workspace]);

  return { ...workspaces[workspace], id: workspace };
}

export function WorkbenchNavigation({ active }: { active: string }) {
  return <aside className="workbench-rail">
    <a className="workbench-brand" href="#incident-snapshot">
      <span className="mark" aria-hidden="true"><i /><i /><i /></span>
      <span><strong>OpsBlaze</strong><small>Operations console</small></span>
    </a>
    <nav aria-label="Workbench sections">
      <a href="#incident-snapshot" aria-current={active === "incident-snapshot" ? "location" : undefined}>
        <span className="nav-icon register" aria-hidden="true" />Incidents
      </a>
      <a href="#incident-evidence" aria-current={active === "incident-evidence" ? "location" : undefined}>
        <span className="nav-icon trace" aria-hidden="true" />Evidence and trace
      </a>
      <a href="#queue-health" aria-current={active === "queue-health" ? "location" : undefined}>
        <span className="nav-icon queue" aria-hidden="true" />Queue health
      </a>
      <a href="#experience-review" aria-current={active === "experience-review" ? "location" : undefined}>
        <span className="nav-icon review" aria-hidden="true" />Experience review
      </a>
      <a href="#replay-lab" aria-current={active === "replay-lab" ? "location" : undefined}>
        <span className="nav-icon compare" aria-hidden="true" />Replay lab
      </a>
      <a href="#recent-incidents" className="history-link">Recent incidents</a>
    </nav>
    <div className="rail-scope">
      <strong>Community Edition</strong>
      <small>Simulator recovery only</small>
    </div>
  </aside>;
}
