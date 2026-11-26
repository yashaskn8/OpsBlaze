import assert from "node:assert/strict";
import { test } from "node:test";
import { detail, fetchQueueHealth, fetchDeadLetters, fetchInvestigation, finishReview } from "../src/api.ts";
import { consumeIncidentStream } from "../src/incidentStream.ts";
import { advisoryFallbackMessage, handoffGuidance } from "../src/investigationGuidance.ts";

test("queue request uses the caller's headers and original read-only path", async (t) => {
  const headers = { Authorization: "Bearer synthetic-test" };
  const payload = { pending_outbox: 3 };
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(payload));
  assert.deepEqual(await fetchQueueHealth(headers), payload);
  assert.equal(fetch.mock.calls.length, 1);
  assert.deepEqual(fetch.mock.calls[0].arguments, ["/v1/operations/queues", { headers }]);
});

test("investigation requests carry the current scope and reject mismatched snapshots", async (t) => {
  const headers = { "X-Tenant-Id": "synthetic" };
  const signal = new AbortController().signal;
  const payload = { incident: { id: "first" }, steps: [] };
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(payload));
  assert.deepEqual(await fetchInvestigation("first", headers, signal), payload);
  assert.deepEqual(fetch.mock.calls[0].arguments, [
    "/v1/incidents/first/investigation", { headers, signal },
  ]);
  await assert.rejects(fetchInvestigation("other", headers, signal), /selected incident/);
});

test("review completion refetches for success, conflict and forbidden responses", async () => {
  let refreshed = 0;
  const refresh = async () => { refreshed += 1; };
  await finishReview(Response.json({}, { status: 201 }), refresh);
  for (const status of [409, 403]) {
    await assert.rejects(
      finishReview(Response.json({ detail: "Review changed" }, { status }), refresh),
      /Review changed/,
    );
  }
  assert.equal(refreshed, 3);
  await assert.rejects(
    finishReview(Response.json({ detail: "Database offline" }, { status: 503 }), refresh),
    /Database offline/,
  );
  assert.equal(refreshed, 3);
});

test("investigation handoff directs operators to scoped read-only checks", () => {
  assert.match(handoffGuidance("TOOL_UNAVAILABLE"), /read-only observation/);
  assert.match(handoffGuidance("TOOL_BLOCKED"), /authorization and target scope/);
  assert.match(handoffGuidance("BUDGET_EXHAUSTED"), /runbook for escalation/);
  assert.match(handoffGuidance("VERSION_CHANGED"), /current target version/);
  assert.match(handoffGuidance("UNLINKED_EVIDENCE"), /fresh evidence/);
  assert.equal(handoffGuidance("unknown untrusted text"), handoffGuidance(undefined));
  assert.match(advisoryFallbackMessage("PROVIDER_UNAVAILABLE"), /did not return usable guidance/);
  assert.match(advisoryFallbackMessage("BUDGET_DENIED"), /budget was unavailable/);
  assert.equal(advisoryFallbackMessage("unknown untrusted text"), "The advisory result was unavailable.");
});

test("dead-letter request preserves cursor, page limit and caller identity", async (t) => {
  const headers = { "X-Tenant-Id": "synthetic", "X-OpsBlaze-Token": "test-only" };
  const payload = { records: [], next_cursor: null, metadata_only: true };
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(payload));
  assert.deepEqual(await fetchDeadLetters(headers, 42), payload);
  assert.deepEqual(fetch.mock.calls[0].arguments, [
    "/v1/operations/dead-letters?cursor=42&limit=25", { headers },
  ]);
});

test("HTTP failures preserve the API error and never return a successful payload", async (t) => {
  t.mock.method(globalThis, "fetch", async () =>
    Response.json({ detail: "Access denied" }, { status: 403 }));
  await assert.rejects(fetchQueueHealth({}), { message: "Access denied" });
  await assert.rejects(fetchDeadLetters({}, 0), { message: "Access denied" });
  assert.equal(detail(null), "Request failed");
  assert.equal(detail({ detail: ["invalid"] }), '["invalid"]');
});

test("incident stream receives split frames and reports disconnection on clean EOF", async () => {
  const events = [];
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('event: incident\r'));
      controller.enqueue(encoder.encode('\ndata: {"id":1}\r\n\r'));
      controller.enqueue(encoder.encode('\nevent: gap\ndata: {"min_after_id":5}\n\n'));
      controller.close();
    },
  });
  await consumeIncidentStream(stream, new AbortController().signal, {
    onOpen: async () => events.push("live"),
    onFrame: async lines => events.push(lines),
    onClose: () => events.push("stale"),
  });
  assert.deepEqual(events, [
    "live", ['event: incident', 'data: {"id":1}'],
    ['event: gap', 'data: {"min_after_id":5}'], "stale",
  ]);
  assert.equal(stream.locked, false);
});

test("failed snapshot refresh cancels the opened stream before retry", async () => {
  let cancelled = false;
  let closed = false;
  const stream = new ReadableStream({ cancel() { cancelled = true; } });
  await assert.rejects(consumeIncidentStream(stream, new AbortController().signal, {
    onOpen: async () => { throw new Error("snapshot unavailable"); },
    onFrame: async () => assert.fail("failed snapshots must not deliver events"),
    onClose: () => { closed = true; },
  }), /snapshot unavailable/);
  assert.equal(closed, true);
  assert.equal(cancelled, true);
  assert.equal(stream.locked, false);
});

test("oversized complete notification is rejected before handling", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("data: " + "x".repeat(16_384) + "\n\n"));
    },
  });
  await assert.rejects(consumeIncidentStream(stream, new AbortController().signal, {
    onOpen: async () => {},
    onFrame: async () => assert.fail("oversized notification was delivered"),
    onClose: () => {},
  }), /invalid event stream/);
  assert.equal(stream.locked, false);
});
