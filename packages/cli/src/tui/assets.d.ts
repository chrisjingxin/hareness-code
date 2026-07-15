/** Bun 的 file import 会在 build 时复制资源并返回发布目录中的相对路径。 */
declare module "*.wasm" {
  const assetPath: string
  export default assetPath
}

declare module "*.scm" {
  const assetPath: string
  export default assetPath
}
