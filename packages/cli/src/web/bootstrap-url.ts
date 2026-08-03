/** Browser bootstrap URL 的 fragment 解析与 loopback 校验（无 DOM 依赖）。 */

export type BootstrapFragment = {
  endpoint: string
  token: string
  attachmentId: string
  threadId: string | null
}

/** 解析并校验 fragment；endpoint/token/attachment 必填非空，thread 可选。 */
export function parseBootstrapFragment(hash: string): BootstrapFragment | undefined {
  const fragment = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash)
  const endpoint = fragment.get("endpoint")
  const token = fragment.get("token")
  const attachmentId = fragment.get("attachment")
  if (!endpoint || !token || !attachmentId) return undefined
  const rawThread = fragment.get("thread")
  if (rawThread === "") return undefined
  return {
    endpoint,
    token,
    attachmentId,
    threadId: rawThread,
  }
}

/** Agent endpoint 必须是 loopback WebSocket；Bun 页面与 Agent 端口允许不同。 */
export function validateAgentEndpoint(endpoint: string): boolean {
  let url: URL
  try {
    url = new URL(endpoint)
  } catch {
    return false
  }
  return url.protocol === "ws:"
    && url.hostname === "127.0.0.1"
    && url.port !== ""
    && url.username === ""
    && url.password === ""
    && url.search === ""
    && url.hash === ""
    && (url.pathname === "" || url.pathname === "/")
}
