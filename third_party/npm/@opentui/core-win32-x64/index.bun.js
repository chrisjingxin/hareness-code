const module = await import("./opentui.dll", { with: { type: "file" } })

export default module.default
