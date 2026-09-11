/** 兼容别名文件：把 AgentGateway 相关类型与实现以旧名转发，老 import 不用改。 */

export type {
  AgentGateway as InteractiveAgentPort,
  InteractiveRunCompletion,
  AgentGatewayStartRunInput,
  InteractiveAgentRun,
} from "./ports/agent-gateway"

export { AgentClientGateway as AgentClientInteractiveAdapter } from "../infrastructure/agent-client-gateway"
