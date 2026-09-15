import { TimeToFirstDrawRenderable } from "@opentui/core";
import type { ExtendedComponentProps } from "./types/components.js";
declare module "@opentui/react" {
    interface OpenTUIComponents {
        "time-to-first-draw": typeof TimeToFirstDrawRenderable;
    }
}
export type TimeToFirstDrawProps = ExtendedComponentProps<typeof TimeToFirstDrawRenderable>;
export declare const TimeToFirstDraw: (props: TimeToFirstDrawProps) => import("react").ReactElement<import("@opentui/core").TimeToFirstDrawOptions & {
    children?: React.ReactNode;
    style?: Partial<Omit<import("@opentui/core").TimeToFirstDrawOptions | undefined, import("@opentui/react").NonStyledProps>> | undefined;
} & import("@opentui/react").ReactProps<TimeToFirstDrawRenderable>, string | import("react").JSXElementConstructor<any>>;
