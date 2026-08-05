/** CryptoIdGenerator 基础设施：基于 crypto.randomUUID() 实现 IdGenerator 接口。 */

import type { IdGenerator } from "../interactive/ports/id-generator"

export class CryptoIdGenerator implements IdGenerator {
  uuid(): string {
    return crypto.randomUUID()
  }
}

export const cryptoIdGenerator = new CryptoIdGenerator()
