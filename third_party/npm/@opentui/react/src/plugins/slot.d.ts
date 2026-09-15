import { SlotRegistry, type CliRenderer, type Plugin, type PluginContext, type PluginErrorEvent, type SlotMode, type SlotRegistryOptions } from "@opentui/core";
import type { ReactNode } from "react";
export type { SlotMode };
type SlotMap = Record<string, object>;
export type ReactPlugin<TSlots extends SlotMap, TContext extends PluginContext = PluginContext> = Plugin<ReactNode, TSlots, TContext>;
export type ReactSlotProps<TSlots extends SlotMap, K extends keyof TSlots, TContext extends PluginContext = PluginContext> = {
    registry: SlotRegistry<ReactNode, TSlots, TContext>;
    name: K;
    mode?: SlotMode;
    children?: ReactNode;
    pluginFailurePlaceholder?: (failure: PluginErrorEvent) => ReactNode;
} & TSlots[K];
export type ReactBoundSlotProps<TSlots extends SlotMap, K extends keyof TSlots> = {
    name: K;
    mode?: SlotMode;
    children?: ReactNode;
} & TSlots[K];
export type ReactRegistrySlotComponent<TSlots extends SlotMap, TContext extends PluginContext = PluginContext> = <K extends keyof TSlots>(props: ReactSlotProps<TSlots, K, TContext>) => ReactNode;
export type ReactSlotComponent<TSlots extends SlotMap> = <K extends keyof TSlots>(props: ReactBoundSlotProps<TSlots, K>) => ReactNode;
export interface ReactSlotOptions {
    pluginFailurePlaceholder?: (failure: PluginErrorEvent) => ReactNode;
}
export declare function createReactSlotRegistry<TSlots extends SlotMap, TContext extends PluginContext = PluginContext>(renderer: CliRenderer, context: TContext, options?: SlotRegistryOptions): SlotRegistry<ReactNode, TSlots, TContext>;
export declare function createSlot<TSlots extends SlotMap, TContext extends PluginContext = PluginContext>(registry: SlotRegistry<ReactNode, TSlots, TContext>, options?: ReactSlotOptions): ReactSlotComponent<TSlots>;
export declare function Slot<TSlots extends SlotMap, TContext extends PluginContext = PluginContext, K extends keyof TSlots = keyof TSlots>(props: ReactSlotProps<TSlots, K, TContext>): ReactNode;
