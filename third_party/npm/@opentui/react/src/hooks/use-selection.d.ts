import type { Selection } from "@opentui/core";
/**
 * Subscribe to text selection events.
 * Fires when the user selects text in the terminal (e.g., via mouse drag).
 *
 * @example
 * useSelectionHandler((selection) => {
 *   const text = selection.getSelectedText()
 *   console.log("Selected:", text)
 * })
 */
export declare const useSelectionHandler: (handler: (selection: Selection) => void) => void;
