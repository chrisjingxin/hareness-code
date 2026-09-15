/**
 * Subscribe to terminal window focus events.
 * Fires when the terminal window gains focus.
 *
 * @example
 * useFocus(() => {
 *   console.log("Terminal gained focus")
 * })
 */
export declare const useFocus: (handler: () => void) => void;
