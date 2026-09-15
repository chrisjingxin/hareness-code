/**
 * Subscribe to terminal window blur events.
 * Fires when the terminal window loses focus.
 *
 * @example
 * useBlur(() => {
 *   console.log("Terminal lost focus")
 * })
 */
export declare const useBlur: (handler: () => void) => void;
