// @bun
import {
  createRoot
} from "./chunk-rm53pqj2.js";
import"./chunk-2mx7fq49.js";

// src/test-utils.ts
import { createTestRenderer } from "@opentui/core/testing";
import { act } from "react";
function setIsReactActEnvironment(isReactActEnvironment) {
  globalThis.IS_REACT_ACT_ENVIRONMENT = isReactActEnvironment;
}
async function testRender(node, testRendererOptions) {
  let root = null;
  setIsReactActEnvironment(true);
  const testSetup = await createTestRenderer({
    ...testRendererOptions,
    onDestroy() {
      act(() => {
        if (root) {
          root.unmount();
          root = null;
        }
      });
      testRendererOptions.onDestroy?.();
      setIsReactActEnvironment(false);
    }
  });
  root = createRoot(testSetup.renderer);
  act(() => {
    if (root) {
      root.render(node);
    }
  });
  return testSetup;
}
export {
  testRender
};
