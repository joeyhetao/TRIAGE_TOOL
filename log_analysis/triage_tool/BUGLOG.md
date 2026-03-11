# Bug 记录文档

供软件工程师排查和参考历史问题。

---

## BUG-002 删除文件后重新选择同一文件无法加载

**发现日期**：2026-03-08
**状态**：已修复
**版本**：v1.2

### 现象

首次选择 `sim.log` 并加载成功，点击 ✕ 从列表移除后，再次选择同一个 `sim.log`，文件列表无响应，`change` 事件不触发。

### 根因分析

浏览器对 `<input type="file">` 的 `change` 事件触发条件是：**input 的值发生变化**。
首次选择后 input 内部记录了文件路径；即便从 JS 的 `selectedFiles` 数组中移除了该文件条目，input 本身的值未被清除。再次选择相同文件时，浏览器判断路径未变化，不触发 `change` 事件。

### 修复方案

**`templates/index.html`**，在 `change` 回调中处理完文件后立即重置 input 值：

```javascript
// 修复前
fileInput.addEventListener('change', () => addFiles(fileInput.files));

// 修复后
fileInput.addEventListener('change', () => {
    addFiles(fileInput.files);
    fileInput.value = '';   // 清空，确保再次选同一文件时仍触发 change 事件
});
```

### 注意

模板文件由 Flask 每次请求动态读取，此修复**无需重新打包 exe**，重启 exe 后生效。

### 涉及文件

- `templates/index.html`

---

## BUG-001 文件选择后未加载，点击分析提示"请先选择日志文件"

**发现日期**：2026-03-08
**状态**：已修复
**版本**：v1.1 → v1.2

### 现象

在首页点击"点击选择文件"，选择仿真日志文件后，文件列表区域无任何显示；点击"开始分析"按钮提示"请先选择日志文件"。

### 根因分析

两个独立原因叠加：

**原因1：`accept` 属性过滤导致 `change` 事件不触发**

```html
<!-- 修复前 -->
<input type="file" id="fileInput" multiple accept=".log,.txt" style="display:none">
```

浏览器文件对话框设置了 `accept=".log,.txt"` 过滤器。当用户选择的文件扩展名不在此列表中（如 `.out`、`.rpt`、无扩展名等常见 EDA 仿真日志格式）时，部分浏览器（Chrome/Edge）在关闭对话框后**静默丢弃**选择结果，不触发 `change` 事件，导致 `selectedFiles` 数组始终为空。

服务端解析器（`core/log_parser.py`）本身按文本内容匹配 UVM 正则，不依赖文件扩展名，故前端 `accept` 限制是多余的约束。

**原因2：拖拽区可点击区域过小**

```html
<!-- 修复前 -->
<div class="drop-zone" id="dropZone">
  ...
  <label for="fileInput" class="link-btn">点击选择文件</label>
```

整个拖拽框只有 `<label>` 文字部分响应点击，点击图标或空白区域无反应，用户误操作概率高。

### 修复方案

**`templates/index.html`**：

```html
<!-- 修复后 -->
<div class="drop-zone" id="dropZone" onclick="fileInput.click()">
  <div class="drop-icon">📂</div>
  <div class="drop-text">拖拽日志文件到此处，或 <span class="link-btn">点击选择文件</span></div>
  <div class="drop-hint">支持任意文本日志格式，可多选</div>
  <input type="file" id="fileInput" multiple style="display:none">
</div>
```

- `accept` 改为仅 `.log`，在 JS `addFiles()` 中增加扩展名二次校验，非 .log 文件给出提示并跳过（双重过滤，不依赖浏览器静默丢弃）
- 将 `onclick="fileInput.click()"` 移至 `dropZone` div，整个拖拽区域均可点击触发文件选择
- `<label for="fileInput">` 改为普通 `<span>`，避免与 div 的 onclick 重复触发

### 验证方法

1. 选择 `.log` 文件 → 应正常显示在文件列表
2. 选择无扩展名或 `.out`、`.rpt` 等文件 → 应正常显示在文件列表
3. 点击拖拽区图标和空白处 → 应弹出文件选择对话框
4. 完整流程：选文件 → 开始分析 → 正常跳转结果页

### 涉及文件

- `templates/index.html`（逻辑修改）
- `dist/triage_tool.exe`（重新打包）
