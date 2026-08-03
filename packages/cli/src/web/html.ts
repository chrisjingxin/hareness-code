/** Web 表现层 HTML Shell；业务状态与协议处理位于同目录 app.ts。 */

export const webHtml = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Harness Code</title>
  <style>
    :root { color-scheme: light dark; font: 15px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
    body { max-width: 860px; margin: 0 auto; padding: 32px 20px; background: #11110f; color: #e9e6dc; }
    header { display:flex; align-items:baseline; justify-content:space-between; border-bottom:1px solid #38362f; }
    h1 { font-size:18px; font-weight:600; } #status { color:#a6a297; font-size:12px; }
    #messages { min-height:55vh; padding:24px 0; }
    article { white-space:pre-wrap; padding:10px 14px; margin:8px 0; border-left:2px solid #666256; background:#191916; }
    article.user { border-color:#d4a85e; } article.tool { color:#aaa69a; }
    form { display:flex; gap:8px; position:sticky; bottom:0; padding:14px 0; background:#11110f; }
    textarea { flex:1; min-height:62px; resize:vertical; padding:10px; font:inherit; color:inherit; background:#1d1c18; border:1px solid #4b483e; }
    button { padding:0 18px; color:#11110f; background:#d4a85e; border:0; font:inherit; cursor:pointer; }
    button:disabled, textarea:disabled { opacity:.45; cursor:not-allowed; }
    #actions { display:flex; justify-content:flex-end; gap:8px; padding:6px 0; }
    #actions button { background:transparent; color:#d4a85e; border:1px solid #4b483e; }
  </style>
</head>
<body>
  <header><h1>Harness Code · Web</h1><span id="status">连接中</span></header>
  <main id="messages" aria-live="polite"></main>
  <form id="composer"><textarea id="prompt" aria-label="消息" placeholder="输入消息，Enter 发送，Shift+Enter 换行"></textarea><button id="send">发送</button></form>
  <div id="actions"><button id="return">返回 TUI</button></div>
  <script type="module" src="/web/app.js"></script>
</body>
</html>`
