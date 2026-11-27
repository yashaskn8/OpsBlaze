import assert from "node:assert/strict";
import { test } from "node:test";
import { workspaceLocation } from "../src/workspacePaths.ts";

test("each workspace address has a matching focus destination", () => {
  for (const id of ["incident-snapshot", "incident-evidence", "queue-health", "experience-review", "replay-lab"]) {
    assert.deepEqual(workspaceLocation(`#${id}`), { workspace: id, target: id });
  }
});

test("legacy and nested links open the incident workspace without losing their target", () => {
  for (const id of ["recent-incidents", "investigation-outcome", "workspace-access"]) {
    assert.deepEqual(workspaceLocation(`#${id}`), { workspace: "incident-snapshot", target: id });
  }
});

test("empty and unrecognized fragments leave the default workspace unfocused", () => {
  for (const hash of ["", "#", "#unknown", "#approvals?"]) {
    assert.deepEqual(workspaceLocation(hash), { workspace: "incident-snapshot", target: null });
  }
});
