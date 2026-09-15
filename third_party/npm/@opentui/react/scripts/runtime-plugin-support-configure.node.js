const errorMessage = "@opentui/react/runtime-plugin-support/configure is Bun-only and is not available in Node.js. Use Bun to import this entrypoint."

export function ensureRuntimePluginSupport() {
  throw new Error(errorMessage)
}

throw new Error(errorMessage)
