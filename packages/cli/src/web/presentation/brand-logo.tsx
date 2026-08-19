/** Harness Code Web 品牌标志：按产品参考图重建的可缩放交织曲线。 */
/** @jsxImportSource react */

/**
 * 渲染透明背景的 Harness 交织标志。
 *
 * 四段独立圆角轨迹保留参考图的断口与穿插关系；渐变只作用于标志本身，
 * 因而在浅色和深色顶栏中都不会出现参考位图的白色方形底。
 */
export function HarnessBrandLogo() {
  return (
    <svg
      className="harness-brand-logo"
      viewBox="0 0 110 110"
      width="22"
      height="22"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="harness-logo-ink" x1="18" y1="12" x2="91" y2="102" gradientUnits="userSpaceOnUse">
          <stop className="harness-logo-ink-start" />
          <stop className="harness-logo-ink-mid" offset="0.52" />
          <stop className="harness-logo-ink-end" offset="1" />
        </linearGradient>
        <linearGradient id="harness-logo-green" x1="15" y1="96" x2="95" y2="14" gradientUnits="userSpaceOnUse">
          <stop stopColor="#3f7d5c" />
          <stop offset="0.55" stopColor="#5f9670" />
          <stop offset="1" stopColor="#78a985" />
        </linearGradient>
      </defs>
      <path d="M45 14C29 5 11 15 11 33c0 7 3 13 8 18l29 29" stroke="url(#harness-logo-ink)" strokeWidth="8.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M62 40l29 29c10 10 8 26-3 33-9 6-20 5-28-1" stroke="url(#harness-logo-ink)" strokeWidth="8.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M29 52l30-30C70 11 86 11 96 22c10 11 9 27 1 37" stroke="url(#harness-logo-green)" strokeWidth="8.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M18 67c-9 11-6 27 5 35 11 8 26 6 35-3l22-22" stroke="url(#harness-logo-green)" strokeWidth="8.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
