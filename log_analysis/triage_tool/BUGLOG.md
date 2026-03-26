# Bug 记录文档

供软件工程师排查和参考历史问题。

---

## BUG-013 指定路径模式批量扫描完成后，按钮 Spinner 持续转动不停

**发现日期**：2026-03-26
**状态**：已修复

### 现象

使用"指定路径"模式批量扫描日志，分析完成后页面不跳转，"开始分析"按钮的加载动画（Spinner）持续转动，用户需手动刷新页面才能查看结果。

### 根因分析

存在两处独立缺陷叠加触发。

**缺陷一：后台线程竞态导致前端收到 `redirect: null`**

`_run_analysis` 后台线程在标记任务完成时，`phase` 与 `redirect` 赋值顺序有误：

```python
# 修复前（错误顺序）
job['phase']    = 'done'       # ← 先写 phase
job['redirect'] = '/result'   # ← 后写 redirect
```

SSE 生成器在 Flask 主线程中每 0.3 s 轮询 `_jobs`。若在两条赋值之间读到 job，会推送 `{"phase": "done", "redirect": null}`。前端收到后执行：

```javascript
setTimeout(() => { window.location.href = null; }, 600);
```

`window.location.href = null` 在部分浏览器中静默失败，页面留在原地，Spinner 永远不停。

**缺陷二：`es.onerror` 未重置按钮**

```javascript
// 修复前
es.onerror = function() { es.close(); };
```

SSE 连接因任何原因断开（包括服务端正常关闭流）时，浏览器触发 `onerror`，此处只关闭了 EventSource，未调用 `_resetAnalyzeBtn()`，Spinner 同样持续转动。

### 修复方案

**`app.py`**，交换赋值顺序，确保 `redirect` 在 `phase` 之前写入，消除竞态窗口：

```python
# 修复后（正确顺序）
job['redirect'] = '/result'   # 先写 redirect
job['phase']    = 'done'      # 再写 phase
```

**`templates/index.html`**，两处修改：

```javascript
// 修复一：不依赖 payload 中的 redirect 字段，改用硬编码路径
// 修复前
setTimeout(() => { window.location.href = d.redirect; }, 600);
// 修复后
setTimeout(() => { window.location.href = '/result'; }, 600);

// 修复二：onerror 补充重置按钮，防止连接异常时 Spinner 卡死
// 修复前
es.onerror = function() { es.close(); };
// 修复后
es.onerror = function() { es.close(); _resetAnalyzeBtn(); };
```

### 涉及文件

- `app.py`
- `templates/index.html`

---

## BUG-012 上传临时文件运行期间持续累积，磁盘空间无限增长

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

上传模式下，每次分析都将日志文件保存到 `uploads/` 目录，运行期间文件持续累积，长期不重启服务时磁盘空间无限增长。原有清理仅在**启动时执行一次**（删除24小时以上旧文件），运行中产生的新文件不受清理。

### 根因分析

`/analyze` 路由将上传文件保存到 `uploads/` 后，解析完成即可丢弃（结果已存入内存 `_store`），但原实现未在解析后删除临时文件，完全依赖重启触发清理。

### 修复方案

**`app.py`**，上传模式解析完成后立即删除临时文件，启动时清理作为兜底保留：

```python
# 上传模式：解析完成后立即删除临时文件（结果已存入内存，文件不再需要）
if not path_mode:
    for fp in saved_paths:
        try:
            Path(fp).unlink()
        except OSError:
            pass
```

### 涉及文件

- `app.py`

---

## BUG-011 `parse_log` 一次性加载整个文件，大文件导致内存溢出

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

解析 1GB 以上日志文件时，服务进程内存占用急剧上升（一个 10GB 文件约消耗 20GB 内存），并行解析多个大文件时内存压力成倍叠加，最终触发 OOM 或系统 Swap 导致严重卡顿。

### 根因分析

```python
# 修复前（一次性加载）
lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
```

`read_text()` 将整个文件读入一个字符串，`splitlines()` 再生成一份完整行列表，峰值内存约为文件大小的2倍。

### 修复方案

**`core/log_parser.py`**，改为逐行流式读取，用 `pending` 状态机处理续行逻辑（替代原来的下标前向查找）：

```python
# 修复后（流式读取，内存与文件大小无关）
with open(str(path), encoding='utf-8', errors='replace') as f:
    for raw_line in f:
        line = raw_line.rstrip('\n')
        # pending 状态机：遇到续行则缓冲，遇到终止条件则提交
        if pending is not None:
            if (stripped and not _UVM_ANY.search(stripped)
                    and line.startswith(' ') and len(cont_lines) < 3):
                cont_lines.append(stripped)
                continue
            # 续行终止，提交 pending
            ...
        m = _UVM_PATTERN.search(line)
        ...
```

任意大小文件解析期间内存占用仅为常数级（当前续行缓冲最多3行）。`top_errors` 满5条后仍继续扫描全文以统计准确的 FATAL/ERROR/WARNING 总数。

### 涉及文件

- `core/log_parser.py`

---

## BUG-010 `send_file(attachment_filename=...)` 在 Flask 2.0 下报 TypeError

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

在 Flask 2.0+ 环境下，导出 Excel / HTML 报告时抛出 `TypeError: send_file() got an unexpected keyword argument 'attachment_filename'`，报告下载功能完全不可用。

### 根因分析

Flask 2.0 将 `send_file` 的 `attachment_filename` 参数重命名为 `download_name`，旧参数名已被移除。

### 修复方案

**`app.py`**，`export_excel` 和 `export_html` 路由均改为 `download_name`：

```python
# 修复前
return send_file(out_path, as_attachment=True, attachment_filename=fname)

# 修复后
return send_file(out_path, as_attachment=True, download_name=fname)
```

### 涉及文件

- `app.py`

---

## BUG-009 Windows 下强制删除被持有的僵尸锁文件引发 PermissionError

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

在 Windows 环境中，当 `_FileLock` 检测到超时僵尸锁并调用 `os.remove()` 删除时，若对应进程仍持有文件句柄（但崩溃前未关闭），抛出 `PermissionError`，使整个锁等待循环崩溃，知识库写入失败。

### 根因分析

Linux 下文件删除是解除目录项引用，持有文件句柄的进程仍可访问直至句柄关闭。Windows 下文件被任意进程持有时，`os.remove()` 直接返回 `PermissionError`，行为与 Linux 不同。

### 修复方案

**`core/db_manager.py`**，将 `os.remove()` 包裹在 `try/except OSError`，Windows 无法删除时静默跳过，继续等待自然释放：

```python
# 修复前
os.remove(self.lock_path)

# 修复后
try:
    os.remove(self.lock_path)
except OSError:
    pass  # Windows: 文件仍被持有，等待自然释放
```

### 涉及文件

- `core/db_manager.py`

---

## BUG-008 `/writeback` 接口缺乏服务端输入校验，存在注入风险

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

`/writeback` 接口直接将前端 JSON 字段写入知识库，未校验 `level` 合法性，未校验 `reason` 非空，字段长度也无限制，可写入任意内容或超长字段。

### 根因分析

接口信任前端传入数据，未做任何服务端校验。

### 修复方案

**`app.py`**，`writeback` 路由增加三类校验：

```python
VALID_LEVELS = {'UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'}
MAX_LEN = 500

level = data.get('level', '').strip().upper()
if level not in VALID_LEVELS:
    return jsonify({'success': False, 'error': '无效的错误级别'}), 400
reason = data.get('reason', '').strip()
if not reason:
    return jsonify({'success': False, 'error': '报错原因不能为空'}), 400

# 所有字段截断至 500 字符
entry = {
    '错误类型': level,
    '报错原因': reason[:MAX_LEN],
    # ... 其余字段同样 [:MAX_LEN]
}
```

### 涉及文件

- `app.py`

---

## BUG-007 HTML 报告中动态内容未转义，存在 XSS 风险

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

生成的 HTML 报告使用字符串拼接直接插入文件名、错误描述、根因等字段，若日志中含 `<script>` 等 HTML 特殊字符，打开报告时会执行任意脚本。

### 根因分析

`reporter.py` 生成 HTML 时未对动态内容做 HTML 实体转义。

### 修复方案

**`core/reporter.py`**，引入 `html.escape` 并对所有动态字段应用：

```python
from html import escape as h

# 修复前
f'<td>{r["file"]}</td>'
f'<td>{err.get("description","")}</td>'

# 修复后
f'<td>{h(r["file"])}</td>'
f'<td>{h(err.get("description",""))}</td>'
```

### 涉及文件

- `core/reporter.py`

---

## BUG-006 Flask `secret_key` 硬编码导致 Session 可伪造

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

`app.py` 中 `secret_key` 为硬编码固定字符串，攻击者知晓后可伪造任意 Session Cookie，冒充其他用户读取分析结果。

### 根因分析

固定 `secret_key` 等同于无 Cookie 签名保护。

### 修复方案

**`app.py`**，首次启动时用 `secrets.token_bytes(32)` 生成随机密钥并持久化到 `.secret_key` 文件；Linux 下 `chmod 0o600` 限制读取权限：

```python
_key_file = BASE_DIR / '.secret_key'
if _key_file.exists():
    app.secret_key = _key_file.read_bytes()
else:
    _key = secrets.token_bytes(32)
    _key_file.write_bytes(_key)
    if sys.platform != 'win32':
        os.chmod(str(_key_file), 0o600)
    app.secret_key = _key
```

### 涉及文件

- `app.py`

---

## BUG-005 上传文件名未净化，存在路径穿越漏洞

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

上传模式下，构造含 `../` 的文件名（如 `../../etc/passwd.log`）可将文件写到 `uploads/` 目录之外的任意位置。

### 根因分析

`app.py` 直接使用 `f.filename` 拼接保存路径，未做任何净化。

### 修复方案

**`app.py`**，使用 `werkzeug.utils.secure_filename` 净化文件名；空白/纯非法字符文件名回退到随机名：

```python
from werkzeug.utils import secure_filename

safe_name = secure_filename(f.filename) or f'file_{uuid.uuid4().hex[:8]}.log'
save_path = UPLOAD_DIR / f'{sid}_{safe_name}'
```

### 涉及文件

- `app.py`

---

## BUG-004 错误描述多行续行仅拼接1行，描述信息截断

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

UVM 错误描述跨越多行时，解析结果仅包含首行内容，后续关键信息（如具体断言值、堆栈第二行）丢失，导致知识库关键词匹配率下降。

### 根因分析

`log_parser.py` 原实现只向后读取1行续行，且判断条件为"下一行不是 UVM 条目"，未过滤空行，遇到空行后续内容也会被误拼接。

### 修复方案

**`core/log_parser.py`**，最多续读3行，遇到 UVM 条目行、空行或非缩进行停止：

```python
_UVM_ANY = re.compile(r'UVM_(?:ERROR|WARNING|FATAL|INFO)\s', re.IGNORECASE)

extra = []
for j in range(i + 1, min(i + 4, len(lines))):
    next_line = lines[j].strip()
    if not next_line or _UVM_ANY.search(next_line) or not lines[j].startswith(' '):
        break
    extra.append(next_line)
if extra:
    description = description + ' ' + ' '.join(extra)
```

### 涉及文件

- `core/log_parser.py`

---

## BUG-003 `_FileLock` 僵尸锁年龄用 `time.monotonic()` 与 mtime 比较导致判断错误

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

`_FileLock` 超时检测逻辑中，用于判断僵尸锁年龄的时间基准与文件 mtime 时间基准不一致，导致僵尸锁可能永远不被清理，或正常锁被误判为僵尸锁。

### 根因分析

```python
# 修复前（错误）
age = time.monotonic() - os.path.getmtime(self.lock_path)
```

`time.monotonic()` 返回系统单调时钟（相对于系统启动的秒数），`os.path.getmtime()` 返回 Unix 时间戳（1970年起的秒数）。两者相减没有物理意义，数值差异巨大（后者通常大 17 亿），导致 `age > STALE_TIMEOUT(60)` 永远为真，每个锁文件创建后立即被误判为僵尸锁删除。

### 修复方案

**`core/db_manager.py`**，改用 `time.time()` 使两侧时基一致：

```python
# 修复后（正确）
age = time.time() - os.path.getmtime(self.lock_path)
```

### 涉及文件

- `core/db_manager.py`

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
