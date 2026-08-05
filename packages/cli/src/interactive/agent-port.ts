/** Interactive Core 的 remote-owned seam 兼容别名转发与导向文件。 */

export type {
  AgentGateway as InteractiveAgentPort,
  InteractiveRunCompletion,
  AgentGatewayStartRunInput,
  InteractiveAgentRun,
} from "./ports/agent-gateway"

export { AgentClientGateway as AgentClientInteractiveAdapter } from "../infrastructure/agent-client-gateway"
