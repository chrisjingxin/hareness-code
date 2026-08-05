/** Clock Port：定义时间与耗时计算的可注入抽象。 */

export interface Clock {
  /** 获取当前时间戳（毫秒）。 */
  now(): number
  /** 计算两个时间戳之间的持续毫秒数。 */
  duration(startMs: number, endMs: number): number
}
