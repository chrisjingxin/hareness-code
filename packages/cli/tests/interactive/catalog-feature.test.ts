import { expect, test } from "bun:test"
import { makeHarness, flush } from "./harness"

test("catalog 单项失败只影响对应 catalog", async () => {
  const harness = makeHarness()
  harness.port.listThreads = async () => {
    harness.calls.push("threads.list")
    throw new Error("threads unavailable")
  }
  try {
    await harness.controller.dispatch({ type: "catalog.refresh", catalog: "threads" })
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.catalogs.threads.status).toBe("error")
    expect(snapshot.catalogs.skills.status).toBe("ready")
    expect(snapshot.catalogs.models.status).toBe("idle")
    expect(snapshot.catalogs.agents.status).toBe("idle")
    expect(snapshot.currentThreadId).toBeNull()
  } finally {
    await harness.controller.close()
  }
})

