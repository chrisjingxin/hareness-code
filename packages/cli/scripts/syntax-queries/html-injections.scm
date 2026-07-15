; OpenTUI 0.4.3 的 injectionMapping 以捕获节点类型选择目标 parser。
; style_element 以完整节点交给 CSS parser；script 的 raw_text 则避免把 <script> 标签交给 JavaScript parser。
(style_element) @injection.content
(script_element (raw_text) @injection.content)
