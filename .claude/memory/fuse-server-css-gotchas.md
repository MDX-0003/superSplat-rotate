---
name: fuse-server-css-gotchas
description: fuse_server.py CSS 调试陷阱：双花括号导致规则失效；input type=number 不支持 size 属性
metadata:
  type: gotcha
---

## _CSS 变量中的双花括号陷阱

[../tills/server/fuse_server.py](../tills/server/fuse_server.py) 中的 `_CSS` 变量是**普通 Python 字符串**（不是 f-string），其中 modal 相关 CSS 规则用了 `{{` `}}`（双花括号），其余规则用了 `{}`（单花括号）。

双花括号在普通字符串中不会被转义——浏览器收到的就是字面 `{{` 和 `}}`，而非合法的 CSS `{` `}`，导致这些规则**静默失效**。

**受影响的规则**（_CSS 末尾，约 7 条）：
- `.modal-card .ms{...}` — section 容器
- `.modal-card .ms h3{...}` — section 标题
- `.modal-card .fd{...}` — 字段行
- `.modal-card .fd label{...}` — 字段标签
- `.modal-card .fd input[type="text"]{...}` — 输入框
- `.modal-card .fd select{...}` — 下拉框

**不受影响的规则**（单花括号，一直正常工作）：
- `.modal-card{...}` — 卡片容器
- `.modal-card h2{...}` — 标题
- `.modal-card .close{...}` — 关闭按钮

**Bug 表现**：modal 外层容器样式生效（卡片圆角、阴影、宽度），但内部字段布局完全不生效（标签不对齐、字号不放大、间距不增加）。浏览器开发者工具中失效规则直接不出现，极易误判为"样式写了但没起到预期效果"。

**根因**：原作者可能最初把这些 CSS 写在 f-string 里（需要用 `{{` 转义为 `{`），后提取成 `_CSS` 变量但忘记把 `{{` 改回 `{`。参见 [[fuse-server-css-gotchas-user]] 中的 user-level 副本。

**How to apply**：修改 `_CSS` 后，必须用 curl 检查实际输出的 CSS：
```bash
curl -s http://localhost:8081/ | grep -o '\.modal-card \.fd[^{]*{[^}]*}'
```
确认花括号是单层的 `{}`。如果看到 `{{` 就说明转义错误。

## `<input type="number">` 不支持 `size` 属性

HTML 规范：`size` 属性仅对 `type="text/search/tel/url/email/password"` 有效。`type="number"` 上 `size` 被浏览器静默忽略，输入框回退到默认宽度（~20 字符）。

**解决方案**：改用 `type="text"` + `size="N"`，JS 端 `parseFloat()` / `parseInt()` 不受影响。可选加 `inputmode="decimal"` 或 `inputmode="numeric"` 优化移动端键盘。

**注意**：`step` 属性对 `type="text"` 无意义（浏览器忽略），不影响功能。

## 调试方法

当样式修改不生效时，不要只看源码——直接 curl 页面确认浏览器实际收到的 HTML/CSS：
```bash
# 检查 CSS 是否有双花括号
curl -s http://localhost:8081/ | grep -o '\.modal-card \.fd[^{]*{[^}]*}'
# 检查 input 属性和 size
curl -s http://localhost:8081/ | grep -o 'input type="[^"]*"[^>]*' | head -10
# 检查关键 CSS 规则是否出现
curl -s http://localhost:8081/ | grep -o 'flex:0 0 215px'
```
