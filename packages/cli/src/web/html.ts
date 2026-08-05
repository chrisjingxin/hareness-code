/** Web 安全 HTML shell：不内嵌业务脚本、样式或凭据数据。 */

export const webHtml = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>Harness Code</title>
  <link rel="stylesheet" href="/web/app.css">
</head>
<body>
  <div id="root"><main class="web-static-state"><p>正在连接 Harness Code…</p></main></div>
  <script type="module" src="/web/app.js"></script>
</body>
</html>`
