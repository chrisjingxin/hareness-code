import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { expect, test } from "bun:test"
import {
  VENDORED_PACKAGE_SPECS,
  directorySha256,
  sha256Hex,
  validateVendoredWorkspace,
} from "./vendor_policy"

function writeJson(path: string, value: unknown): void {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf-8")
}

function createFixture(): string {
  const root = mkdtempSync(join(tmpdir(), "harness-vendor-policy-"))
  mkdirSync(join(root, "packages/cli"), { recursive: true })
  mkdirSync(join(root, "third_party/npm/@opentui"), { recursive: true })

  writeJson(join(root, "package.json"), {
    name: "fixture",
    workspaces: ["packages/*", "third_party/npm/*", "third_party/npm/@opentui/*"],
  })
  writeJson(join(root, "packages/cli/package.json"), {
    dependencies: {
      "@opentui/core": "workspace:*",
      "@opentui/react": "workspace:*",
    },
    devDependencies: {
      "react-devtools-core": "workspace:*",
    },
  })
  mkdirSync(join(root, "patches"), { recursive: true })
  const patchSource = "- ws: ^7\n+ ws: 7.5.10\n"
  writeFileSync(join(root, "patches/react-devtools-core@7.0.1.patch"), patchSource, "utf-8")

  for (const spec of VENDORED_PACKAGE_SPECS) {
    const packageRoot = join(root, spec.path)
    mkdirSync(packageRoot, { recursive: true })
    const packageJson: Record<string, unknown> = {
      name: spec.name,
      version: spec.version,
      license: "MIT",
    }
    if (spec.name === "@opentui/core") {
      packageJson.dependencies = { "bun-ffi-structs": "workspace:*" }
      packageJson.optionalDependencies = { "@opentui/core-win32-x64": "workspace:*" }
    }
    if (spec.name === "@opentui/react") {
      packageJson.dependencies = { "@opentui/core": "workspace:*" }
    }
    if (spec.name === "react-devtools-core") {
      packageJson.dependencies = { ws: "7.5.10" }
    }
    if (spec.name === "@opentui/core-win32-x64") {
      packageJson.os = ["win32"]
      packageJson.cpu = ["x64"]
      writeFileSync(join(packageRoot, "opentui.dll"), "fixture dll\n", "utf-8")
    }
    writeJson(join(packageRoot, "package.json"), packageJson)
    writeFileSync(join(packageRoot, "LICENSE"), "MIT\n", "utf-8")
  }

  const provenancePackages = Object.fromEntries(
    VENDORED_PACKAGE_SPECS.map((spec) => [spec.name, {
      name: spec.name,
      version: spec.version,
      path: spec.path,
      tarball: spec.tarball,
      integrity: spec.integrity,
      license: "MIT",
      directorySha256: directorySha256(join(root, spec.path)),
    }]),
  )
  writeJson(join(root, "third_party/npm/provenance.json"), {
    schemaVersion: 1,
    target: { os: "win32", cpu: "x64" },
    packages: provenancePackages,
    patches: {
      "react-devtools-core": {
        source: "patches/react-devtools-core@7.0.1.patch",
        sourceSha256: sha256Hex(patchSource),
      },
    },
  })

  writeJson(join(root, "bun.lock"), {
    packages: Object.fromEntries(
      VENDORED_PACKAGE_SPECS.map((spec) => [spec.name, [`${spec.name}@workspace:${spec.path}`]]),
    ),
  })
  return root
}

function withFixture(callback: (root: string) => void): void {
  const root = createFixture()
  try {
    callback(root)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

test("accepts complete vendored Windows x64 workspace provenance", () => {
  withFixture((root) => expect(validateVendoredWorkspace(root)).toEqual([]))
})

test("rejects a vendored package with the wrong version", () => {
  withFixture((root) => {
    const packageJson = join(root, "third_party/npm/bun-ffi-structs/package.json")
    writeJson(packageJson, { name: "bun-ffi-structs", version: "0.2.3", license: "MIT" })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("version mismatch")
  })
})

test("rejects a package with a non-MIT license declaration", () => {
  withFixture((root) => {
    writeJson(join(root, "third_party/npm/bun-ffi-structs/package.json"), {
      name: "bun-ffi-structs",
      version: "0.2.4",
      license: "GPL-3.0",
    })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("license must be MIT")
  })
})

test("rejects tampered vendored files", () => {
  withFixture((root) => {
    writeFileSync(join(root, "third_party/npm/bun-ffi-structs/README.md"), "tampered\n", "utf-8")
    expect(validateVendoredWorkspace(root).join("\n")).toContain("directory hash mismatch")
  })
})

test("ignores Bun workspace installation output in the release directory hash", () => {
  withFixture((root) => {
    mkdirSync(join(root, "third_party/npm/bun-ffi-structs/node_modules/generated"), { recursive: true })
    writeFileSync(join(root, "third_party/npm/bun-ffi-structs/node_modules/generated/link.txt"), "install output\n", "utf-8")
    expect(validateVendoredWorkspace(root)).toEqual([])
  })
})

test("rejects a Windows target package without opentui.dll", () => {
  withFixture((root) => {
    rmSync(join(root, "third_party/npm/@opentui/core-win32-x64/opentui.dll"))
    expect(validateVendoredWorkspace(root).join("\n")).toContain("missing opentui.dll")
  })
})

test("rejects a target package published for the wrong platform", () => {
  withFixture((root) => {
    writeJson(join(root, "third_party/npm/@opentui/core-win32-x64/package.json"), {
      name: "@opentui/core-win32-x64",
      version: "0.4.3",
      license: "MIT",
      os: ["darwin"],
      cpu: ["arm64"],
    })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("must target win32/x64")
  })
})

test("rejects registry resolution for any vendored package", () => {
  withFixture((root) => {
    writeJson(join(root, "bun.lock"), {
      packages: {
        ...Object.fromEntries(
          VENDORED_PACKAGE_SPECS.map((spec) => [spec.name, [`${spec.name}@${spec.version}`, "", {}, spec.integrity]]),
        ),
      },
    })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("registry fallback")
  })
})

test("rejects a duplicate root patchedDependencies entry", () => {
  withFixture((root) => {
    writeJson(join(root, "package.json"), {
      name: "fixture",
      workspaces: ["packages/*", "third_party/npm/*", "third_party/npm/@opentui/*"],
      patchedDependencies: { "react-devtools-core@7.0.1": "patches/react-devtools-core@7.0.1.patch" },
    })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("duplicate root patchedDependencies")
  })
})
