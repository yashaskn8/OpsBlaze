import assert from "node:assert/strict";
import { test } from "node:test";
import { replaceLineagePage, evidenceProjectionWarning } from "../src/lineagePages.ts";

function page(incident_id, kind, index, next_cursor = String(index + 1)) {
  return { incident_id, kind, next_cursor, nodes: Array.from({ length: 50 }, (_, i) => `${index}:${i}`) };
}

test("repeated pagination retains only the latest bounded page per kind", () => {
  let state = { incidentId: "a", pages: {} };
  for (let i = 0; i < 100; i++) {
    state = replaceLineagePage(state, page("a", "evidence", i));
  }
  assert.equal(state.pages.evidence.nodes.length, 50);
  assert.equal(state.pages.evidence.nodes[0], "99:0");
  assert.equal(state.pages.evidence.next_cursor, "100");
});

test("separate kinds remain available and terminal cursor stays null", () => {
  const start = replaceLineagePage({ incidentId: "a", pages: {} }, page("a", "effect", 0));
  const next = replaceLineagePage(start, page("a", "evidence", 1, null));
  assert.deepEqual(next.pages.effect, start.pages.effect);
  assert.equal(next.pages.evidence.next_cursor, null);
  assert.equal(start.pages.evidence, undefined);
});

test("an incident change discards all previously retained pages", () => {
  const start = replaceLineagePage({ incidentId: "a", pages: {} }, page("a", "effect", 0));
  const next = replaceLineagePage(start, page("b", "evidence", 1));
  assert.equal(next.incidentId, "b");
  assert.deepEqual(Object.keys(next.pages), ["evidence"]);
});

test("projection warnings never hide drift behind a matched page", () => {
  assert.match(evidenceProjectionWarning(["MATCHED", "STALE_OR_MISSING"]), /stale or missing/);
  assert.match(evidenceProjectionWarning(["NOT_CHECKED_TRUNCATED", "STALE_OR_MISSING"]), /stale or missing/);
});

test("truncated or absent projection status is not treated as verified", () => {
  assert.match(evidenceProjectionWarning(["MATCHED", "NOT_CHECKED_TRUNCATED"]), /not checked/);
  assert.match(evidenceProjectionWarning([undefined]), /unavailable/);
  assert.equal(evidenceProjectionWarning(["MATCHED", "NOT_APPLICABLE"]), null);
});
