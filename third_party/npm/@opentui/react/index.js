// @bun
import {
  AppContext,
  Fragment,
  baseComponents,
  componentCatalogue,
  createPortal,
  createRoot,
  extend,
  flushSync,
  getComponentCatalogue,
  jsxDEV,
  useAppContext
} from "./chunk-rm53pqj2.js";
import"./chunk-2mx7fq49.js";
// src/hooks/use-blur.ts
import { useEffect } from "react";

// src/hooks/use-event.ts
import { useCallback, useLayoutEffect, useRef } from "react";
function useEffectEvent(handler) {
  const handlerRef = useRef(handler);
  useLayoutEffect(() => {
    handlerRef.current = handler;
  });
  return useCallback((...args) => {
    const fn = handlerRef.current;
    return fn(...args);
  }, []);
}

// src/hooks/use-renderer.ts
var useRenderer = () => {
  const { renderer } = useAppContext();
  if (!renderer) {
    throw new Error("Renderer not found.");
  }
  return renderer;
};

// src/hooks/use-blur.ts
var useBlur = (handler) => {
  const renderer = useRenderer();
  const stableHandler = useEffectEvent(handler);
  useEffect(() => {
    renderer.on("blur", stableHandler);
    return () => {
      renderer.off("blur", stableHandler);
    };
  }, [renderer]);
};
// src/hooks/use-focus.ts
import { useEffect as useEffect2 } from "react";
var useFocus = (handler) => {
  const renderer = useRenderer();
  const stableHandler = useEffectEvent(handler);
  useEffect2(() => {
    renderer.on("focus", stableHandler);
    return () => {
      renderer.off("focus", stableHandler);
    };
  }, [renderer]);
};
// src/hooks/use-keyboard.ts
import { useEffect as useEffect3 } from "react";
var useKeyboard = (handler, options = { release: false }) => {
  const { keyHandler } = useAppContext();
  const stableHandler = useEffectEvent(handler);
  useEffect3(() => {
    keyHandler?.on("keypress", stableHandler);
    if (options?.release) {
      keyHandler?.on("keyrelease", stableHandler);
    }
    return () => {
      keyHandler?.off("keypress", stableHandler);
      if (options?.release) {
        keyHandler?.off("keyrelease", stableHandler);
      }
    };
  }, [keyHandler, options.release]);
};
// src/hooks/use-paste.ts
import { useEffect as useEffect4 } from "react";
var usePaste = (handler) => {
  const { keyHandler } = useAppContext();
  const stableHandler = useEffectEvent(handler);
  useEffect4(() => {
    keyHandler?.on("paste", stableHandler);
    return () => {
      keyHandler?.off("paste", stableHandler);
    };
  }, [keyHandler]);
};
// src/hooks/use-resize.ts
import { useEffect as useEffect5 } from "react";
var useOnResize = (callback) => {
  const renderer = useRenderer();
  const stableCallback = useEffectEvent(callback);
  useEffect5(() => {
    renderer.on("resize", stableCallback);
    return () => {
      renderer.off("resize", stableCallback);
    };
  }, [renderer]);
  return renderer;
};
// src/hooks/use-selection.ts
import { useEffect as useEffect6 } from "react";
var useSelectionHandler = (handler) => {
  const renderer = useRenderer();
  const stableHandler = useEffectEvent(handler);
  useEffect6(() => {
    renderer.on("selection", stableHandler);
    return () => {
      renderer.off("selection", stableHandler);
    };
  }, [renderer]);
};
// src/hooks/use-terminal-dimensions.ts
import { useState } from "react";
var useTerminalDimensions = () => {
  const renderer = useRenderer();
  const [dimensions, setDimensions] = useState({
    width: renderer.width,
    height: renderer.height
  });
  const cb = (width, height) => {
    setDimensions({ width, height });
  };
  useOnResize(cb);
  return dimensions;
};
// src/hooks/use-timeline.ts
import { engine, Timeline } from "@opentui/core";
import { useEffect as useEffect7 } from "react";
var useTimeline = (options = {}) => {
  const timeline = new Timeline(options);
  useEffect7(() => {
    if (!options.autoplay) {
      timeline.play();
    }
    engine.register(timeline);
    return () => {
      timeline.pause();
      engine.unregister(timeline);
    };
  }, []);
  return timeline;
};
// src/plugins/slot.tsx
import {
  createSlotRegistry
} from "@opentui/core";
import React, { Fragment as Fragment2, useEffect as useEffect8, useMemo, useRef as useRef2, useState as useState2 } from "react";
function createReactSlotRegistry(renderer, context, options = {}) {
  return createSlotRegistry(renderer, "react:slot-registry", context, options);
}
function renderPluginFailurePlaceholder(registry, pluginFailurePlaceholder, failure, pluginId, slot) {
  if (!pluginFailurePlaceholder) {
    return null;
  }
  try {
    return pluginFailurePlaceholder(failure);
  } catch (error) {
    registry.reportPluginError({
      pluginId,
      slot,
      phase: "error_placeholder",
      source: "react",
      error
    });
    return null;
  }
}

class PluginErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { failure: null };
  }
  componentDidCatch(error) {
    const failure = this.props.registry.reportPluginError({
      pluginId: this.props.pluginId,
      slot: this.props.slotName,
      phase: "render",
      source: "react",
      error
    });
    this.setState({ failure });
  }
  componentDidUpdate(previousProps) {
    if (previousProps.resetToken !== this.props.resetToken && this.state.failure) {
      this.setState({ failure: null });
    }
  }
  render() {
    if (this.state.failure) {
      const placeholder = renderPluginFailurePlaceholder(this.props.registry, this.props.pluginFailurePlaceholder, this.state.failure, this.props.pluginId, this.props.slotName);
      if (placeholder === null || placeholder === undefined || placeholder === false) {
        return this.props.fallbackOnFailure ?? null;
      }
      return placeholder;
    }
    return this.props.children;
  }
}
function getSlotProps(props) {
  const {
    children: _children,
    mode: _mode,
    name: _name,
    registry: _registry,
    pluginFailurePlaceholder: _pluginFailurePlaceholder,
    ...slotProps
  } = props;
  return slotProps;
}
function createSlot(registry, options = {}) {
  return function BoundSlot(props) {
    return /* @__PURE__ */ jsxDEV(Slot, {
      ...props,
      registry,
      pluginFailurePlaceholder: options.pluginFailurePlaceholder
    }, undefined, false, undefined, this);
  };
}
function Slot(props) {
  const [version, setVersion] = useState2(0);
  const registry = props.registry;
  const slotName = String(props.name);
  const renderFailuresByPluginRef = useRef2(new Map);
  const pendingRenderReportsRef = useRef2(new Map);
  useEffect8(() => {
    return registry.subscribe(() => {
      setVersion((current) => current + 1);
    });
  }, [registry]);
  useEffect8(() => {
    if (pendingRenderReportsRef.current.size === 0) {
      return;
    }
    const pendingReports = [...pendingRenderReportsRef.current.values()];
    pendingRenderReportsRef.current.clear();
    for (const report of pendingReports) {
      const failure = registry.reportPluginError({
        pluginId: report.pluginId,
        slot: report.slot,
        phase: "render",
        source: "react",
        error: report.error
      });
      renderFailuresByPluginRef.current.set(`${report.slot}:${report.pluginId}:render`, failure);
    }
  });
  const entries = useMemo(() => registry.resolveEntries(props.name), [registry, props.name, version]);
  const slotProps = getSlotProps(props);
  const renderEntry = (entry, fallbackOnFailure) => {
    const key = `${slotName}:${entry.id}`;
    const failureKey = `${slotName}:${entry.id}:render`;
    try {
      const rendered = entry.renderer(registry.context, slotProps);
      renderFailuresByPluginRef.current.delete(failureKey);
      pendingRenderReportsRef.current.delete(failureKey);
      return /* @__PURE__ */ jsxDEV(PluginErrorBoundary, {
        registry,
        pluginFailurePlaceholder: props.pluginFailurePlaceholder,
        pluginId: entry.id,
        slotName,
        resetToken: version,
        fallbackOnFailure,
        children: rendered
      }, key, false, undefined, this);
    } catch (error) {
      const normalizedError = error instanceof Error ? error : typeof error === "string" ? new Error(error) : new Error(String(error));
      const lastFailure = renderFailuresByPluginRef.current.get(failureKey);
      const isSameFailure = lastFailure && lastFailure.error.message === normalizedError.message;
      if (!isSameFailure) {
        const queued = pendingRenderReportsRef.current.get(failureKey);
        if (!queued || queued.error.message !== normalizedError.message) {
          pendingRenderReportsRef.current.set(failureKey, {
            pluginId: entry.id,
            slot: slotName,
            error: normalizedError
          });
        }
      }
      const failure = isSameFailure && lastFailure ? lastFailure : {
        pluginId: entry.id,
        slot: slotName,
        phase: "render",
        source: "react",
        error: normalizedError,
        timestamp: Date.now()
      };
      renderFailuresByPluginRef.current.set(failureKey, failure);
      const placeholder = renderPluginFailurePlaceholder(registry, props.pluginFailurePlaceholder, failure, entry.id, slotName);
      if (placeholder === null || placeholder === undefined || placeholder === false) {
        return fallbackOnFailure ?? null;
      }
      return /* @__PURE__ */ jsxDEV(Fragment2, {
        children: placeholder
      }, key, false, undefined, this);
    }
  };
  if (entries.length === 0) {
    return /* @__PURE__ */ jsxDEV(Fragment, {
      children: props.children
    }, undefined, false, undefined, this);
  }
  if (props.mode === "single_winner") {
    const winner = entries[0];
    if (!winner) {
      return /* @__PURE__ */ jsxDEV(Fragment, {
        children: props.children
      }, undefined, false, undefined, this);
    }
    const rendered = renderEntry(winner, props.children);
    if (rendered === null || rendered === undefined || rendered === false) {
      return /* @__PURE__ */ jsxDEV(Fragment, {
        children: props.children
      }, undefined, false, undefined, this);
    }
    return /* @__PURE__ */ jsxDEV(Fragment, {
      children: rendered
    }, undefined, false, undefined, this);
  }
  if (props.mode === "replace") {
    if (entries.length === 1) {
      const rendered = renderEntry(entries[0], props.children);
      if (rendered === null || rendered === undefined || rendered === false) {
        return /* @__PURE__ */ jsxDEV(Fragment, {
          children: props.children
        }, undefined, false, undefined, this);
      }
      return /* @__PURE__ */ jsxDEV(Fragment, {
        children: rendered
      }, undefined, false, undefined, this);
    }
    const renderedEntries = entries.map((entry) => renderEntry(entry));
    const hasPluginOutput = renderedEntries.some((node) => node !== null && node !== undefined && node !== false);
    if (!hasPluginOutput) {
      return /* @__PURE__ */ jsxDEV(Fragment, {
        children: props.children
      }, undefined, false, undefined, this);
    }
    return /* @__PURE__ */ jsxDEV(Fragment, {
      children: renderedEntries
    }, undefined, false, undefined, this);
  }
  return /* @__PURE__ */ jsxDEV(Fragment, {
    children: [
      props.children,
      entries.map((entry) => renderEntry(entry))
    ]
  }, undefined, true, undefined, this);
}
// src/time-to-first-draw.tsx
import { TimeToFirstDrawRenderable } from "@opentui/core";
import { createElement } from "react";
extend({ "time-to-first-draw": TimeToFirstDrawRenderable });
var TimeToFirstDraw = (props) => {
  return createElement("time-to-first-draw", props);
};

// src/index.ts
import { createElement as createElement2 } from "react";
export {
  useTimeline,
  useTerminalDimensions,
  useSelectionHandler,
  useRenderer,
  usePaste,
  useOnResize,
  useKeyboard,
  useFocus,
  useBlur,
  useAppContext,
  getComponentCatalogue,
  flushSync,
  extend,
  createSlot,
  createRoot,
  createReactSlotRegistry,
  createPortal,
  createElement2 as createElement,
  componentCatalogue,
  baseComponents,
  TimeToFirstDraw,
  Slot,
  AppContext
};
