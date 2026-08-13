/** Web 目录信任交互详情面板。 */
/** @jsxImportSource react */

import type { InteractiveInteraction } from "../../interactive/types"

/** 挂起中的目录信任交互卡片 DTO。 */
type DirectoryTrustCard = Extract<InteractiveInteraction, { type: "directory_trust" }>

/** 展示目录信任交互的关键路径信息；决策按钮由 DirectoryTrustForm 渲染。 */
export function DirectoryTrustApproval(props: { interaction: DirectoryTrustCard }) {
  const { interaction } = props
  return (
    <div className="directory-trust-approval" role="region" aria-label="目录信任详情">
      <dl className="directory-trust-meta">
        <div>
          <dt>工具</dt>
          <dd>{interaction.toolName}（{interaction.access === "write" ? "写入" : "读取"}）</dd>
        </div>
        <div>
          <dt>目标路径</dt>
          <dd><code>{interaction.targetPath}</code></dd>
        </div>
        <div>
          <dt>待信任目录</dt>
          <dd><code>{interaction.directory}</code></dd>
        </div>
      </dl>
      {interaction.shadowsWorkspace ? (
        <p className="directory-trust-warning">注意：该目录会遮蔽主工作区内的同名路径。</p>
      ) : null}
      <p className="directory-trust-hint">
        信任后，该目录将与主工作区等价；读写仍按当前审批模式处理。
      </p>
    </div>
  )
}
