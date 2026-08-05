/** IdGenerator Port：定义唯一 ID / 随机 UUID 生成的可注入抽象。 */

export interface IdGenerator {
  /** 生成一个符合规范的唯一 UUID / ID 字符串。 */
  uuid(): string
}
