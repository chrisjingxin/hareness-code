import { mkdtempSync, mkdirSync, readFileSync, symlinkSync, writeFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { expect, test } from "bun:test"
import {
  VENDORED_PACKAGE_SPECS,
  VENDORED_PACKAGE_CONSUMERS,
  directorySha256,
  sha256Hex,
  validateExecutionPlatform,
  validateInstalledVendoredWorkspace,
  validateVendoredWorkspace,
} from "./vendor_policy"

function writeJson(path: string, value: unknown): void {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf-8")
}

function mutateProvenance(root: string, callback: (provenance: Record<string, unknown>) => void): void {
  const path = join(root, "third_party/npm/provenance.json")
  const provenance = JSON.parse(readFileSync(path, "utf-8")) as Record<string, unknown>
  callback(provenance)
  writeJson(path, provenance)
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
      packageJson.devDependencies = { "@types/react": "19.2.14" }
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
    for (const requiredFile of spec.requiredFiles) {
      if (requiredFile === "package.json" || requiredFile === "LICENSE") continue
      const requiredPath = join(packageRoot, requiredFile)
      mkdirSync(dirname(requiredPath), { recursive: true })
      writeFileSync(requiredPath, "fixture release file\n", "utf-8")
    }
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
    normalizations: [
      {
        kind: "workspace-dependency",
        package: "@opentui/core",
        field: "dependencies.bun-ffi-structs",
        before: "0.2.4",
        after: "workspace:*",
      },
      {
        kind: "workspace-dependency",
        package: "@opentui/core",
        field: "optionalDependencies.@opentui/core-win32-x64",
        before: "0.4.3",
        after: "workspace:*",
      },
      {
        kind: "workspace-dependency",
        package: "@opentui/react",
        field: "dependencies.@opentui/core",
        before: "0.4.3",
        after: "workspace:*",
      },
    ],
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

function packageSegments(name: string): string[] {
  return name.startsWith("@") ? name.split("/") : [name]
}

function linkInstalledPackages(root: string): void {
  for (const spec of VENDORED_PACKAGE_SPECS) {
    for (const consumer of VENDORED_PACKAGE_CONSUMERS[spec.name] ?? []) {
      const link = join(root, consumer, "node_modules", ...packageSegments(spec.name))
      mkdirSync(dirname(link), { recursive: true })
      symlinkSync(join(root, spec.path), link, process.platform === "win32" ? "junction" : "dir")
    }
  }
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

test("rejects a vendored package with a missing required entrypoint", () => {
  withFixture((root) => {
    rmSync(join(root, "third_party/npm/bun-ffi-structs/dist/index.js"))
    expect(validateVendoredWorkspace(root).join("\n")).toContain("required file missing")
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

test("rejects a nested registry locator even when the primary record is workspace", () => {
  withFixture((root) => {
    const lock = {
      packages: {
        ...Object.fromEntries(
          VENDORED_PACKAGE_SPECS.map((spec) => [spec.name, [`${spec.name}@workspace:${spec.path}`]]),
        ),
        "@opentui/core@0.4.3": ["@opentui/core@0.4.3", "", {}, VENDORED_PACKAGE_SPECS[0].integrity],
      },
    }
    writeJson(join(root, "bun.lock"), lock)
    expect(validateVendoredWorkspace(root).join("\n")).toContain("@opentui/core: registry fallback")
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

test("rejects missing workspace normalization provenance", () => {
  withFixture((root) => {
    mutateProvenance(root, (provenance) => {
      provenance.normalizations = (provenance.normalizations as unknown[]).slice(1)
    })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("workspace normalization set mismatch")
  })
})

test("rejects incorrect workspace normalization provenance", () => {
  withFixture((root) => {
    mutateProvenance(root, (provenance) => {
      const normalizations = provenance.normalizations as Array<Record<string, unknown>>
      normalizations[0] = { ...normalizations[0], before: "0.2.3" }
    })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("workspace normalization set mismatch")
  })
})

test("rejects extra workspace normalization drift", () => {
  withFixture((root) => {
    mutateProvenance(root, (provenance) => {
      const normalizations = provenance.normalizations as Array<Record<string, unknown>>
      normalizations.push({
        kind: "workspace-dependency",
        package: "@opentui/core",
        field: "dependencies.unexpected",
        before: "1.0.0",
        after: "workspace:*",
      })
    })
    expect(validateVendoredWorkspace(root).join("\n")).toContain("workspace normalization set mismatch")
  })
})

test("accepts the target execution platform", () => {
  expect(validateExecutionPlatform("win32", "x64")).toEqual([])
})

test("rejects a non-Windows execution platform", () => {
  expect(validateExecutionPlatform("darwin", "arm64").join("\n")).toContain("win32/x64")
})

test("does not report an installed resolution before node_modules exists", () => {
  withFixture((root) => expect(validateInstalledVendoredWorkspace(root)).toEqual([]))
})

test("accepts installed package links that resolve to the vendored directories", () => {
  withFixture((root) => {
    linkInstalledPackages(root)
    expect(validateInstalledVendoredWorkspace(root)).toEqual([])
  })
})

test("rejects an installed package link that resolves outside third_party", () => {
  withFixture((root) => {
    linkInstalledPackages(root)
    const link = join(root, "packages/cli/node_modules/react-devtools-core")
    rmSync(link)
    const external = join(root, "registry/react-devtools-core")
    mkdirSync(external, { recursive: true })
    symlinkSync(external, link, process.platform === "win32" ? "junction" : "dir")
    expect(validateInstalledVendoredWorkspace(root).join("\n")).toContain("must resolve to third_party")
  })
})
