const workspacePaths = [
  "incident-snapshot", "incident-evidence", "queue-health", "experience-review", "replay-lab",
] as const;

export type WorkspaceId = (typeof workspacePaths)[number];

export function workspaceLocation(hash: string): { workspace: WorkspaceId; target: string | null } {
  const target = hash.slice(1);
  const workspace = workspacePaths.find(id => id === target);
  if (workspace) return { workspace, target: workspace };
  if (["workspace-access", "recent-incidents", "investigation-outcome"].includes(target)) {
    return { workspace: "incident-snapshot", target };
  }
  return { workspace: "incident-snapshot", target: null };
}
