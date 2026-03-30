# LLM 集成方案 v2.0 - 改进讨论记录

> **说明**：本文档记录了对 LLM_INTEGRATION_PLAN.md v2.0 的讨论和优化建议。
> **讨论日期**：2026-03-27

---

## 一、主要改动汇总

### P7 - 知识库语义去重质量检查

**原设计问题：**
- 单一滑动窗口方案（20条），对于10万条 KB 难以在合理时间内完成
- 无导出功能，检查结果无法保存

**改进方案（渐进式 + 导出）：**
1. **双模式选择**：快速检查（窗口=20，约5分钟）vs 深度检查（按类型全量，约15分钟）
2. **渐进式进度展示**：支持实时显示检查进度（当前组、已完成数、预计剩余时间）
3. **导出 Excel 功能**：支持将所有检查结果导出为 Excel 文件，包含3个 sheet：
   - Sheet1：疑似重复对列表
   - Sheet2：条目A完整数据（所有字段）
   - Sheet3：条目B完整数据（所有字段）
4. **按类型分组优化**：深度模式下按错误类型（UVM_FATAL/UVM_ERROR/UVM_WARNING）分组处理，可并行
5. **自动分批适配**：受 Token 限制时自动分批（每批 50~80 条）

**Token 成本对比（10万条 KB）：**
| 方案 | Token 需求 | 耗时 |
|------|-----------|----------|
| 快速检查 | ~136k tokens | ~5分钟 |
| 深度检查 | ~672k tokens | ~15分钟 |
| 全量无分组 | ~2.7M tokens | ~4小时 |

**结论：** 方案 C（渐进式 + 用户可控）在10万条规模下是合理选择，满足不同场景需求。

---

## 二、其他讨论的改进点（未实施）

### 1. P3 - 自定义提取功能优化

**讨论点：** 是否需要让 max_dialog_turns 可配置？

**建议：**
- 在 llm_config.json 中添加可选配置：
  ```json
  {
    "max_dialog_turns": 3,  // 默认 2，0 表示不限制
    ...
  }
  ```
- 优点：让用户根据项目复杂度灵活调整
- 缺点：增加配置复杂度，大多数用户可能不需要修改默认值

---

### 2. 缓存机制缺失

**讨论点：** 是否需要添加 LLM 响应缓存？

**建议：**
- 在 core/llm_client.py 中添加内存缓存层
- 缓存键：prompt_hash（前16位）
- TTL：1小时（可配置）
- 优点：减少重复查询成本，提升响应速度
- 缺点：缓存失效后需要重试，首次查询仍需等待

**参考实现：**
```python
_cache = {}
_cache_ttl = 3600  # 1小时

def call_llm_with_cache(prompt, ...):
    cache_key = hashlib.md5(prompt.encode()).hexdigest()[:16]
    if cache_key in _cache:
        if time.time() - _cache[cache_key]['time'] < _cache_ttl:
            return _cache[cache_key]['result']
    # 命中 LLM 并缓存结果
    result = call_llm(prompt, ...)
    _cache[cache_key] = {'result': result, 'time': time.time()}
    return result
```

---

### 3. 配置热重载

**讨论点：** llm_config.json 修改后是否需要重启服务？

**建议：**
- 添加配置热重载功能，提供管理接口：
  ```python
  @llm_bp.route('/llm/reload_config', methods=['POST'])
  def reload_config():
      llm_client.reload_config()
      app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
      return jsonify({'ok': True})
  ```
- 优点：开发调试时方便，不需要重启服务
- 缺点：需要新增路由和前端 UI 元素

---

### 4. LLM 响应超时重试策略

**讨论点：** 是否考虑指数退避重试？

**建议：**
- 添加重试机制，提升网络容错能力
```python
def call_llm_with_retry(prompt, max_retries=2):
    for attempt in range(max_retries):
        try:
            return call_llm(prompt)
        except TimeoutError:
            if attempt == max_retries - 1:
                return ""  # 最终降级
            time.sleep(1 * (2 ** attempt))  # 1s, 2s
```
- 优点：网络抖动时提高成功率
- 缺点：增加实现复杂度，可能掩盖其他问题

---

### 5. P7 分批优化讨论

**原设计：**
- 快速模式：滑动窗口 20 条，步长 10
- 深度模式：自动分批 50~80 条

**讨论点：** 步长 10 是否合理？是否需要可配置？

**建议：**
- 在 llm_config.json 中添加：
  ```json
  {
    "kb_review_window_size": 20,
    "kb_review_step_size": 10,
    "kb_review_batch_size": 50
  }
  ```
- 优点：让用户根据实际场景调整参数
- 缺点：增加配置复杂度

---

## 三、配置项建议汇总

### 核心配置项（建议添加到 llm_config.json）

```json
{
  // === 现有配置项 ===
  "endpoint": "http://your-llm-server/v1/chat/completions",
  "api_key": "sk-xxx",
  "model": "qwen2.5-7b",
  "timeout": 30,
  "context_window": 8192,

  // === 新增可选配置项 ===
  "max_dialog_turns": 3,              // P3 对话轮次限制
  "kb_review_window_size": 20,        // P7 快速模式窗口大小
  "kb_review_step_size": 10,         // P7 快速模式步长
  "kb_review_batch_size": 50,        // P7 快速模式批次大小
  "kb_review_mode": "progressive",      // P7 默认模式：progressive/full
  "cache_ttl": 3600,                 // LLM 响应缓存时间（秒）
  "llm_max_retries": 2,              // LLM 超时重试次数
  "llm_retry_delay": 1                // LLM 重试延迟基数（秒）
}
```

---

## 四、实施优先级建议

### 必须实施（v2.0 核心功能）
1. P7 渐进式模式选择
2. P7 导出 Excel 功能

### 可选实施（按需）
1. P3 max_dialog_turns 可配置
2. LLM 响应缓存
3. 配置热重载功能
4. LLM 响应超时重试
5. P7 窗口/步长/批次大小可配置

### 暂不实施
1. 无

---

## 五、结论

本文档记录了对 LLM_INTEGRATION_PLAN.md v2.0 的主要讨论和优化，重点是：

1. **P7 功能增强**：将单一滑动窗口改为渐进式（快速+深度）模式选择，添加导出 Excel 功能
2. **其他优化点**：缓存、配置热重载、超时重试等增强建议
3. **配置可配置化**：让更多参数支持通过配置文件调整

建议优先实现 P7 的渐进式+导出版本，其他优化可根据实际使用情况按需添加。