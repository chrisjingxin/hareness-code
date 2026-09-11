/**
 * Interactive Core 的 Ports 公共出口：类型（intent/snapshot/outcome）与运行期
 * 依赖接口（Gateway、Clock、Scheduler 等）都从这里取，Feature 与 Adapter 不直接
 * import 各具体模块路径。
 */
export * from "./agent-gateway"
export * from "./clock"
export * from "./scheduler"
export * from "./id-generator"
export * from "./prompt-history-store"
export * from "../types"
