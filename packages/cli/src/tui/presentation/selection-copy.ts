/** TUI 文本选区复制的纯表现层逻辑。 */

export type SelectionCopyInput =
  | { readonly type: "mouse-up"; readonly button: number }
  | { readonly type: "key-down"; readonly name: string; readonly ctrl: boolean }

export type SelectionCopyDependencies = {
  getSelectedText(): string | undefined
  clearSelection(): void
  writeClipboard(text: string): Promise<boolean>
  showToast(message: string, variant: "success" | "error"): void
}

/** 判断一次 TUI 输入是否允许尝试复制当前选区。 */
export function shouldAttemptSelectionCopy(
  platform: NodeJS.Platform,
  input: SelectionCopyInput,
): boolean {
  if (platform === "win32") {
    return input.type === "key-down"
      ? input.ctrl && input.name === "c"
      : input.button === 2
  }
  return input.type === "mouse-up" && input.button === 0
}

/** 开始复制当前非空选区；无选区时同步返回 undefined。 */
export function copyCurrentSelection(
  dependencies: SelectionCopyDependencies,
): Promise<boolean> | undefined {
  const text = dependencies.getSelectedText()
  if (!text) return undefined

  dependencies.clearSelection()
  try {
    return dependencies.writeClipboard(text).then(copied => {
      if (copied) {
        dependencies.showToast("已复制到剪贴板", "success")
        return true
      }
      dependencies.showToast("复制到系统剪贴板失败", "error")
      return false
    }, () => {
      dependencies.showToast("复制到系统剪贴板失败", "error")
      return false
    })
  } catch {
    dependencies.showToast("复制到系统剪贴板失败", "error")
    return Promise.resolve(false)
  }
}
