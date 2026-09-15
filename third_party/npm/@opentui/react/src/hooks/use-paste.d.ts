import type { PasteEvent } from "@opentui/core";
/**
 * Subscribe to terminal paste events (bracketed paste).
 *
 * @example
 * usePaste((event) => {
 *   console.log("Pasted:", event.text)
 * })
 */
export declare const usePaste: (handler: (event: PasteEvent) => void) => void;
