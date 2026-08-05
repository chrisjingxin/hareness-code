import { describe, expect, it } from "bun:test"
import { LANGUAGE_CATALOG, resolveLanguage } from "../../src/presentation-shared/language-catalog"

describe("language-catalog", () => {
  it("应该正确解析 canonical 名称与常见别名", () => {
    expect(resolveLanguage("python").canonical).toBe("python")
    expect(resolveLanguage("py").canonical).toBe("python")
    expect(resolveLanguage("ts").canonical).toBe("typescript")
    expect(resolveLanguage("c++").canonical).toBe("cpp")
    expect(resolveLanguage("zsh").canonical).toBe("bash")
    expect(resolveLanguage("jsonc").canonical).toBe("json")
    expect(resolveLanguage("md").canonical).toBe("markdown")
  })

  it("应该将未知语言或空输入安全降级为 plaintext", () => {
    expect(resolveLanguage("unknown_lang").canonical).toBe("plaintext")
    expect(resolveLanguage("").canonical).toBe("plaintext")
    expect(resolveLanguage(null).canonical).toBe("plaintext")
    expect(resolveLanguage(undefined).canonical).toBe("plaintext")
  })

  it("应该确保 canonical 名称唯一且覆盖全部 16 种目标语言", () => {
    const canonicals = LANGUAGE_CATALOG.map(entry => entry.canonical)
    const uniqueCanonicals = new Set(canonicals)
    expect(uniqueCanonicals.size).toBe(canonicals.length)
    expect(canonicals.length).toBe(16)
  })
})
