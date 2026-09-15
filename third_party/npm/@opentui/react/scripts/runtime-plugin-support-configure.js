import { plugin as registerBunPlugin } from "bun";
import * as coreRuntime from "@opentui/core";
import { createRuntimePlugin, } from "@opentui/core/runtime-plugin";
import * as reactRuntime from "react";
import * as reactJsxRuntime from "react/jsx-runtime";
import * as reactJsxDevRuntime from "react/jsx-dev-runtime";
import * as opentuiReactRuntime from "../index.js";
const runtimePluginSupportInstalledKey = "__opentuiReactRuntimePluginSupportInstalled__";
const defaultRuntimeModules = {
    "@opentui/react": opentuiReactRuntime,
    "@opentui/react/jsx-runtime": reactJsxRuntime,
    "@opentui/react/jsx-dev-runtime": reactJsxDevRuntime,
    react: reactRuntime,
    "react/jsx-runtime": reactJsxRuntime,
    "react/jsx-dev-runtime": reactJsxDevRuntime,
};
function normalizeRewriteKey(rewrite) {
    return `${rewrite?.nodeModulesRuntimeSpecifiers ?? true}:${rewrite?.nodeModulesBareSpecifiers ?? false}`;
}
function createRuntimeModules(options) {
    return {
        ...defaultRuntimeModules,
        ...(options?.additional ?? {}),
    };
}
function assertCompatibleInstall(install, modules, options) {
    for (const specifier of Object.keys(modules)) {
        if (!install.specifiers.has(specifier)) {
            throw new Error(`OpenTUI React runtime plugin support is already installed without ${specifier}. Call ensureRuntimePluginSupport({ additional }) from @opentui/react/runtime-plugin-support/configure before importing @opentui/react/runtime-plugin-support.`);
        }
    }
    if (options?.core && options.core !== install.core) {
        throw new Error("OpenTUI React runtime plugin support is already installed with a different core runtime module.");
    }
    if (options?.rewrite && normalizeRewriteKey(options.rewrite) !== install.rewriteKey) {
        throw new Error("OpenTUI React runtime plugin support is already installed with different rewrite options.");
    }
}
export function ensureRuntimePluginSupport(options = {}) {
    const state = globalThis;
    const modules = createRuntimeModules(options);
    const core = options.core ?? coreRuntime;
    const rewriteKey = normalizeRewriteKey(options.rewrite);
    const install = state[runtimePluginSupportInstalledKey];
    if (install) {
        assertCompatibleInstall(install, modules, options);
        return false;
    }
    registerBunPlugin(createRuntimePlugin({
        core,
        additional: modules,
        rewrite: options.rewrite,
    }));
    state[runtimePluginSupportInstalledKey] = {
        specifiers: new Set(Object.keys(modules)),
        core,
        rewriteKey,
    };
    return true;
}
//# sourceMappingURL=runtime-plugin-support-configure.js.map