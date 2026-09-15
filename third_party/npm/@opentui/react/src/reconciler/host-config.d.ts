import type { HostConfig } from "react-reconciler";
import type { Container, HostContext, Instance, Props, PublicInstance, TextInstance, Type } from "../types/host.js";
type ReconcilerExtensions = {
    maySuspendCommitOnUpdate(type: Type, oldProps: Props, newProps: Props): boolean;
    maySuspendCommitInSyncRender(type: Type, props: Props): boolean;
    rendererPackageName: string;
    rendererVersion: string;
};
export declare const hostConfig: HostConfig<Type, Props, Container, Instance, TextInstance, unknown, // SuspenseInstance
unknown, // HydratableInstance
unknown, // FormInstance
PublicInstance, HostContext, unknown, // ChildSet
unknown, // TimeoutHandle
unknown, // NoTimeout
unknown> & ReconcilerExtensions;
export {};
