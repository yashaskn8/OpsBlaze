import assert from "node:assert/strict";
import { test } from "node:test";
import { detail, fetchQueueHealth, fetchDeadLetters } from "../src/api.ts";

test("queue request uses the caller's headers and original read-only path", async (t) => {
  const headers = { Authorization: "Bearer synthetic-test" };
  const payload = { pending_outbox: 3 };
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(payload));
  assert.deepEqual(await fetchQueueHealth(headers), payload);
  assert.equal(fetch.mock.calls.length, 1);
  assert.deepEqual(fetch.mock.calls[0].arguments, ["/v1/operations/queues", { headers }]);
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
