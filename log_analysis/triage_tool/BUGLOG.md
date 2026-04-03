# Bug 记录文档

供软件工程师排查和参考历史问题。

---

## BUG-025 切换 Tab 后返回，进度日志丢失只见转圈

**发现日期**：2026-04-03
**状态**：已修复

### 现象

分析进行中切换到「查询知识库」等其他 Tab，再切回「上传文件」或「指定路径」Tab，`progressWrap` 进度区不见了，只能看到分析按钮上的 `btnSpinner` 还在转，进度日志全部消失。

### 根因分析

`switchMode()` 在切换到非分析 Tab 时会将 `progressWrap` 设为 `display:none`（正确行为，避免空进度区残留），但切回分析 Tab 时只恢复了 `analyzeBtnRow` 的显示，没有重新显示 `progressWrap`。

SSE 连接（EventSource）是网络层对象，隐藏 DOM 不会中断它，`progLogs` 内容持续被 `_updateProgressUI` 写入，日志并未丢失——只是容器 div 隐藏了。

```javascript
// 修复前：切回分析 Tab 时未恢复 progressWrap
if (mode !== 'upload' && mode !== 'path') {
  document.getElementById('progressWrap').style.display = 'none';
}
// 没有对应的 else 分支
```

### 修复方案

在 `switchMode` 增加 `else if` 分支：切回分析 Tab 时，若 `analyzeBtn` 仍处于 `disabled`（即分析进行中），将 `progressWrap` 恢复为可见。

```javascript
// 修复后
if (mode !== 'upload' && mode !== 'path') {
  document.getElementById('progressWrap').style.display = 'none';
} else if (document.getElementById('analyzeBtn').disabled) {
  document.getElementById('progressWrap').style.display = '';
}
```

**为何用 `analyzeBtn.disabled` 作为判断依据**：分析开始时按钮被 disable，分析结束（完成/失败/超时）时由 `_resetAnalyzeBtn()` 统一恢复 enable，与进度区生命周期完全对齐，无需额外状态变量。

### 涉及文件

- `templates/index.html`：`switchMode()` 函数（约第 268 行）

---

## BUG-017 _store 并发访问竞态条件导致去重详情页信息丢失

**发现日期**：2026-04-01
**状态**：已修复

### 现象

路径模式分析大量日志完成后，汇总栏去重报错数字（如 UVM_ERROR 6条）显示正常，但点击进入 `/errors?level=xxx` 详情页时列表为空。偶发，非必现。

### 根因分析

`_store` 字典无任何同步机制，后台分析线程（`_run_analysis`）调用 `_set_results` 写入时，Flask 请求线程可能同时调用 `_get_results` 执行读取 + TTL 过期清理（`del _store[k]`），导致两种竞态：

1. **写入与读取并发**：后台线程写入 `_store[sid]` 期间，请求线程读到部分写入的数据（Python dict 赋值为原子操作，但 TTL 清理的 `del` + `get` 组合不原子），导致 `entry` 为 `None`，返回空结果。
2. **TTL 清理误删**：后台线程写入后 `ts` 未及时更新，或清理窗口恰好卡在写入前，`del _store[k]` 将刚写入的 sid 误删。

`/result` 页面通常在后台线程完成后首次加载（数据已稳定），而 `/errors` 是二次点击，若恰好另一用户的会话触发了 TTL 清理，同一次 `_get_results` 调用中 `del _store[k]` 遍历时可能波及当前 sid。

### 修复方案

新增 `_store_lock = threading.Lock()`，在 `_get_results` 和 `_set_results` 中用 `with _store_lock` 保护所有对 `_store` 的读写和 TTL 清理操作。锁持有时间极短（内存操作），性能影响可忽略。

```python
# 修复前
def _get_results(sid):
    stale = [k for k, v in list(_store.items()) if ...]
    for k in stale: del _store[k]   # 无锁
    return ...

def _set_results(sid, results, db_path):
    _store[sid] = {...}              # 无锁

# 修复后
_store_lock = threading.Lock()

def _get_results(sid):
    with _store_lock:
        stale = [...]
        for k in stale: del _store[k]
        return ...

def _set_results(sid, results, db_path):
    with _store_lock:
        _store[sid] = {...}
```

### 涉及文件

- `app.py`（`_store_lock` 定义，`_get_results`，`_set_results`）

---

## BUG-014 Linux 路径模式多用例扫描：启动慢 + 完成后圆圈持续转动

**发现日期**：2026-03-29
**状态**：已修复

### 现象

在 Linux 系统下，使用"指定路径"模式扫描大量日志用例时出现两个问题：
1. 点击"开始分析"后，有明显等待（无进度条），才开始显示扫描进度
2. 分析完成后，页面不跳转，进度圈持续转动，用户卡在分析页

### 根因分析

**缺陷一：glob 展开阻塞请求处理线程（导致启动慢）**

`/analyze` 路由在返回 `job_id` 之前，同步执行 `glob.glob()` + `Path.is_file()` 遍历所有匹配文件。文件数量多时，这一步本身需要几十秒，期间前端只能看到按钮 Spinner，进度条无法显示，体验上形同卡死。

**缺陷二：Linux TCP close-before-delivery 导致 SSE 最终事件丢失（导致圆圈不停）**

SSE 生成器发出 `phase='done'` 数据后立即 `return`，服务端随即关闭 TCP 连接。Linux TCP 栈会将数据帧和 FIN 打包在同一或相邻帧发出。浏览器 EventSource 实现可能先处理 TCP 关闭事件（触发 `onerror`），再处理缓冲区中的数据（触发 `onmessage`）。`onerror` 先调 `es.close()`，之后 `onmessage` 不再触发，导致 `phase='done'` 的重定向逻辑永远不执行，圆圈持续转动。

### 修复方案

**缺陷一修复**：将 glob 展开移入后台线程。`/analyze` 路由仅做格式校验，立即创建 job（`phase='scanning'`）并返回 `job_id`。后台线程依次完成：扫描文件（scanning）→ 解析日志（parsing）→ 知识库匹配（matching）。前端新增 `scanning` 状态显示"正在扫描文件..."。

**缺陷二修复（双保险）**：
- 后端：SSE 生成器发出 done/error 事件后 `time.sleep(1)` 再关连接，给客户端留足处理时间
- 前端：`onerror` 不再直接 `_resetAnalyzeBtn()`，改为 fetch `/progress_status/<job_id>` 单次轮询真实状态；若状态为 done 则重定向，为 error 则显示错误，否则重置按钮

### 涉及文件

- `app.py`：`_run_analysis` 加 scanning 阶段；`/analyze` 路由不再 glob；SSE 生成器加 1s 延迟；新增 `/progress_status/<job_id>` 路由
- `templates/index.html`：`onerror` 改为 fallback 轮询；`_updateProgressUI` 增加 scanning 状态

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

---

## BUG-015 切换 Tab 后进度区（"分析完成，正在跳转"）残留

**发现日期**：2026-03-30
**状态**：已修复

### 现象

分析完成后进度区显示"分析完成，正在跳转"日志。此时切换到「查询知识库」「添加条目」「解析配置」任意 Tab，进度区依然可见，干扰操作。

### 根因分析

`switchMode()` 函数切换 Tab 时只负责显示/隐藏各模式 div 和按钮行，未对 `progressWrap` 做任何处理。进度区是独立的全局 DOM 节点，不属于任何模式 div，因此切 Tab 不会隐藏它。

### 修复方案

在 `switchMode()` 末尾追加：

```javascript
if (mode !== 'upload' && mode !== 'path') {
  document.getElementById('progressWrap').style.display = 'none';
}
```

切换到非分析 Tab 时立即隐藏进度区；切回「上传文件/指定路径」Tab 时保持原有状态（进度区仍可见，符合预期）。

### 涉及文件

- `templates/index.html`（`switchMode` 函数新增3行）
- `dist/triage_tool.exe`（重新打包）

---

## BUG-024 SSE 进度流 JSON 解析错误被静默吞掉 + 服务端挂死时按钮永久禁用

**发现日期**：2026-04-02
**状态**：已修复

### 现象

1. SSE 进度流收到非法 JSON 数据时，`catch(_) {}` 静默丢弃，界面无任何反馈，进度条卡住
2. 服务端进程挂死（非正常退出）时，`EventSource` 持续重连，"开始分析"按钮永远禁用，用户只能强制刷新页面

### 根因分析

```javascript
// 修复前
try {
    const d = JSON.parse(event.data);
    ...
} catch(_) {}   // ← 吞掉所有错误
```

`catch` 块为空，JSON 解析失败后流程中断但无任何提示。`EventSource` 本身没有超时机制，`onerror` 仅在 TCP 断开时触发；服务端挂死时 TCP 连接保持，`onerror` 不触发，按钮永久禁用。

### 修复方案

**`templates/index.html`**：

1. 将 `catch(_) {}` 改为 `catch(parseErr) { console.error('[SSE] JSON 解析失败:', parseErr, event.data); }`
2. 添加 30 秒无活动超时计时器（`_resetTimeout`），每收到一条消息重置，超时后关闭连接并提示用户
3. `onerror` 处理器中调用 `clearTimeout(_timeout)` 防止泄漏

```javascript
let _timeout = setTimeout(function() {
    es.close();
    document.getElementById('errMsg').textContent = '分析超时（30秒无响应），请检查服务端状态后重试';
    _resetAnalyzeBtn();
}, 30000);
function _resetTimeout() { clearTimeout(_timeout); _timeout = setTimeout(..., 30000); }
es.onmessage = function(event) {
    _resetTimeout();
    try { ... } catch(parseErr) { console.error('[SSE] JSON 解析失败:', parseErr, event.data); }
};
es.onerror = function() { clearTimeout(_timeout); es.close(); fetch('/progress_status/...') ... };
```

### 涉及文件

- `templates/index.html`（`_connectProgressStream` 函数）

---

## BUG-023 `_get_results()` 直接访问字典字段，结构不完整时抛 KeyError

**发现日期**：2026-04-02
**状态**：已修复

### 现象

`state._store[sid]` 结构不完整时（如存在旧格式条目或手动注入测试数据），`_get_results()` 抛 `KeyError`，导致 `/result`、`/errors`、`/export` 等多个路由返回 HTTP 500。

### 根因分析

```python
# 修复前
entry = _store.get(sid)
return (entry['results'], entry['db_path']) if entry else ([], DB_DEFAULT)
```

`entry` 非 None 但缺少 `results` 或 `db_path` 字段时，直接访问抛 `KeyError`。

### 修复方案

**`state.py`**，改为 `.get()` 并提供默认值：

```python
# 修复后
entry = _store.get(sid)
return (entry.get('results', []), entry.get('db_path', DB_DEFAULT)) if entry else ([], DB_DEFAULT)
```

### 涉及文件

- `state.py`（`_get_results` 函数）

---

## BUG-022 `_jobs` 字典并发清理无锁保护，存在竞态条件

**发现日期**：2026-04-02
**状态**：已修复

### 现象

SSE 端点 `/progress/<job_id>` 清理过期 `_jobs` 条目时无锁保护，与后台分析线程写入 `_jobs[job_id]` 并发，可能导致：
1. `dict changed size during iteration` 异常
2. 正在运行的任务被误删，前端收到"任务不存在"错误
3. 过期条目不清理导致内存持续增长（若 SSE 端点从未被调用）

### 根因分析

```python
# 修复前（无锁）
stale = [k for k, v in list(state._jobs.items())
         if now - v.get('ts', 0) > state._JOBS_TTL]
for k in stale:
    del state._jobs[k]
```

对比：`_store` 的清理在 `_store_lock` 保护下执行，`_jobs` 清理无任何同步保护，不一致。

### 修复方案

**`state.py`**，新增 `_jobs_lock = threading.Lock()` 和 `_cleanup_jobs()` 函数：

```python
_jobs_lock = threading.Lock()

def _cleanup_jobs():
    with _jobs_lock:
        now   = time.time()
        stale = [k for k, v in list(_jobs.items()) if now - v.get('ts', 0) > _JOBS_TTL]
        for k in stale:
            del _jobs[k]
```

**`blueprints/analysis.py`**：
- SSE 生成器中改为调用 `state._cleanup_jobs()`
- `/analyze` 路由在创建新任务前调用 `state._cleanup_jobs()`，并以 `with state._jobs_lock:` 保护任务字典写入

### 涉及文件

- `state.py`（新增 `_jobs_lock`、`_cleanup_jobs`）
- `blueprints/analysis.py`（`progress_stream`、`analyze`）

---

## BUG-021 `update_entry`/`delete_entry` 无异常处理，知识库文件损坏时返回 HTTP 500

**发现日期**：2026-04-02
**状态**：已修复

### 现象

知识库 Excel 文件损坏（如写入中途崩溃）或被独占时，`update_entry()` 和 `delete_entry()` 的 `openpyxl.load_workbook()` 抛出未捕获异常，穿透到 Flask 路由层，返回无任何错误提示的 HTTP 500。

### 根因分析

```python
# 修复前（无异常处理）
with _thread_lock:
    with _FileLock(db_path):
        wb = openpyxl.load_workbook(db_path)   # ← 损坏文件直接抛异常
        ...
```

### 修复方案

**`core/db_manager.py`**，在两个函数中包裹 try/except，抛出语义化 `ValueError`：

```python
# 修复后
try:
    wb = openpyxl.load_workbook(db_path)
except Exception as e:
    raise ValueError(f'无法读取知识库文件（可能已损坏或被独占）: {e}')
```

调用方（`blueprints/kb.py`）已有 `try/except Exception` 包裹，会将 `ValueError` 转为带 message 的 JSON 错误响应。

### 涉及文件

- `core/db_manager.py`（`update_entry`、`delete_entry`）

---

## BUG-020 `load_db()` 未做行列数边界检查，用户手动编辑 Excel 缺列时抛 IndexError

**发现日期**：2026-04-02
**状态**：已修复

### 现象

用户手动编辑 `error_db.xlsx` 时误删列，导致某些数据行的实际列数少于表头列数。`load_db()` 重试 3 次后向上抛出 `RuntimeError`，知识库读取功能完全不可用。

### 根因分析

```python
# 修复前（无边界检查）
entry = {headers[i]: (row[i] if row[i] is not None else '')
         for i in range(len(headers))}
# 若 len(row) < len(headers)，row[i] 抛 IndexError
```

### 修复方案

**`core/db_manager.py`**，加入列数边界检查：

```python
# 修复后
entry = {headers[i]: (row[i] if i < len(row) and row[i] is not None else '')
         for i in range(len(headers))}
```

缺失列自动填充空字符串，知识库仍可正常读取，不影响正常条目。

### 涉及文件

- `core/db_manager.py`（`load_db` 函数）

---

## BUG-019 `_sort_by_date()` 空日期排序方向与注释相悖，无日期条目排在最前

**发现日期**：2026-04-02
**状态**：已修复

### 现象

`_sort_by_date()` 用于对知识库命中条目按录入日期降序排列，注释明确"缺失日期排最后"。实际行为相反：无录入日期的条目排在最前，成为首选展示给用户的匹配结果。

### 根因分析

```python
# 修复前
key=lambda e: str(e.get('录入日期', '') or '')
```

`''`（空字符串）的字典序小于任何日期字符串（如 `'2024-01-01'`），`reverse=True` 降序排列后，空字符串反而排在最前。

### 修复方案

**`core/matcher.py`**，将空值替换为最小日期占位符：

```python
# 修复后
key=lambda e: str(e.get('录入日期', '') or '0000-00-00')
```

`'0000-00-00'` 字典序小于任何合法日期，`reverse=True` 后确保无日期条目排在最后。

### 涉及文件

- `core/matcher.py`（`_sort_by_date` 函数）

---

## BUG-018 `parse_logs()` 单文件解析失败导致整批任务中断

**发现日期**：2026-04-02
**状态**：已修复

### 现象

批量分析 1000 个日志文件时，若其中一个文件权限不足、已被删除或路径断开，`parse_logs()` 立即抛出未捕获异常，终止整个批次，其余所有文件的解析结果全部丢失。

### 根因分析

```python
# 修复前
for future in as_completed(future_to):
    i, fp = future_to[future]
    r = future.result()   # ← parse_log() 内部异常穿透至此，整批中断
    results[i] = r
```

`parse_log()` 内部的 `open()` 调用无 try/except，权限不足、文件消失等均会抛出未捕获异常。

### 修复方案

**`core/log_parser.py`**，新增 `_error_result()` 占位符函数，`parse_logs()` 捕获单文件异常后返回占位结果：

```python
def _error_result(filepath: str, error_msg: str) -> dict:
    return {
        'file': Path(filepath).name, 'filepath': str(filepath),
        'statistics': {'UVM_WARNING': 0, 'UVM_ERROR': 0, 'UVM_FATAL': 0},
        'status': 'fail', 'pass_found': False,
        'top_errors': [], 'all_errors': [],
        'error': error_msg,
    }

# parse_logs() 中
try:
    r = future.result()
except Exception as e:
    r = _error_result(fp, str(e))
results[i] = r
```

调用方可通过检查 `result.get('error')` 字段识别失败条目，其余文件正常返回。

### 涉及文件

- `core/log_parser.py`（`_error_result` 新增，`parse_logs` 修改）

---

## BUG-016 打开「解析配置」Tab 后页面卡死（无限循环）+ 额外关键词含空格导致 ID 非法

**发现日期**：2026-03-30
**状态**：已修复

### 现象

1. 在 Windows（或任何平台）打开「解析配置」Tab 后，页面长时间无响应（浏览器弹出"页面无响应"对话框）
2. 含空格的默认关键词（如 `JVP TEST FAILED`）在关键词配置列表中无法正常编辑/删除

### 根因分析

**Bug 1（无限循环，主因）**

`_populateLevelSelects()` 中：
```javascript
while (dl.options.length > 3) dl.remove(3);
```
`dl` 是 `<datalist>` 元素。`<datalist>` 没有 `remove(index)` 方法；调用 `dl.remove(3)` 实际调用了继承自 `HTMLElement` 的 `remove()` 方法（无参数，忽略传入的 `3`），将整个 datalist 元素从 DOM 中移除。但 `dl.options.length` 在 datalist 脱离 DOM 后不会减少（内存中选项仍存在），导致 `while` 条件永远为真，进入**无限循环**。

触发时机：页面加载时 fetch `/extra_patterns` 会调用 `_populateLevelSelects`，此时 `dl.options.length=3`（初始），loop 不进入，正常追加若干 options（length 变为 3+N）。之后用户打开「解析配置」Tab 触发 `kwRender`，再次调用 `_populateLevelSelects`，此时 `dl.options.length > 3`，进入死循环。

**Bug 2（非法 HTML ID）**

`kwRender` 使用 `kw` 直接拼接元素 ID：`id="kwrow-JVP TEST FAILED"`。HTML ID 不允许包含空格；`document.getElementById('kwrow-JVP TEST FAILED')` 在各浏览器行为不一致，导致编辑/删除按钮失效。此外，后端验证正则 `^[A-Z0-9_]+$` 拒绝含空格的关键词（而默认值中就含 `JVP TEST FAILED`），造成验证规则与实际数据不一致。

### 修复方案

**Bug 1**：将 `dl.remove(3)` 改为 `dl.options[3].remove()`，移除的是第 4 个 `<option>` 子元素而非 datalist 本身。

**Bug 2**：
- `kwRender` 改用数组下标（`i`）作为元素 ID（`id="kwrow-0"`, `id="kwrow-1"` 等），避免关键词内容出现在 ID 中；引入模块变量 `_kwPatterns` 存储当前列表，供 `kwStartEdit/kwCancelEdit/kwSaveEdit/kwDelete` 通过下标取回原始关键词值
- `app.py` 后端验证改为 `^[A-Z0-9_ ]+$`，允许关键词中包含空格（支持 `JVP TEST FAILED` 等多词关键词）

### 涉及文件

- `templates/index.html`（`_populateLevelSelects`、`kwRender`、`kwStartEdit`、`kwCancelEdit`、`kwSaveEdit`、`kwDelete`）
- `app.py`（`extra_patterns_add`、`extra_patterns_update` 验证正则和错误文字）
- `dist/triage_tool.exe`（重新打包）
