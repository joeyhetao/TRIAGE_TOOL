# -*- coding: utf-8 -*-
"""
LLM API 客户端 — 无 Flask 依赖，可在任何线程调用。
纯 stdlib（urllib），不依赖 requests，适配离线内网环境。

公开接口：
  init(base_dir)                               — 加载 llm_config.json + 环境变量
  is_configured() -> bool                      — endpoint + model 均非空则 True
  call_llm(messages, temperature, max_tokens)  — 含超时重试，失败返回 ""
  call_llm_verbose(messages, ...)              — 同上，返回 (result, error_str) 供调试
  call_llm_with_cache(messages, ...)           — 内存缓存包装（md5 key，TTL 可配置）
  reload_config() -> bool                      — 热重载 llm_config.json，返回是否已配置
  get_config() -> dict | None                  — 当前配置快照（用于 P3 参数读取）
  save_config(cfg: dict) -> None               — 写入 llm_config.json 并热重载

支持 API 格式（自动检测）：
  OpenAI 兼容格式  — endpoint 不含 "anthropic"
    请求头: Authorization: Bearer <api_key>
    响应解析: choices[0].message.content
  Anthropic 格式  — endpoint 含 "anthropic"
    请求头: x-api-key: <api_key>, anthropic-version: 2023-06-01
    响应解析: content[0].text
"""
import json
import time
import hashlib
import threading
import os
import urllib.request
import urllib.error
from pathlib import Path

_lock     = threading.Lock()
_config   = None    # dict | None（未配置时为 None）
_cache    = {}      # md5[:16] -> (result_str, expire_ts)
_base_dir = None    # Path

_DEFAULTS = {
    'timeout':               30,
    'context_window':        100000,
    'p3_max_lines':          2500,
    'p3_chars_per_token':    4,
    'cache_ttl':             3600,
    'llm_max_retries':       2,
    'llm_retry_delay':       1,
    'kb_review_mode':        'fast',
    'kb_review_window_size': 20,
    'kb_review_step_size':   10,
    'kb_review_batch_size':  50,
}


def init(base_dir: Path) -> None:
    """加载配置文件，应用环境变量覆盖。在 BASE_DIR 确定后调用一次。"""
    global _base_dir
    _base_dir = Path(base_dir)
    reload_config()


def reload_config() -> bool:
    """重载 llm_config.json + 环境变量覆盖。返回是否已配置。"""
    global _config
    raw = _load_file()
    _apply_env(raw)
    configured = bool(raw.get('endpoint') and raw.get('model'))
    with _lock:
        if configured:
            for k, v in _DEFAULTS.items():
                raw.setdefault(k, v)
            _config = raw
        else:
            _config = None
    return configured


def _load_file() -> dict:
    if _base_dir is None:
        return {}
    p = _base_dir / 'llm_config.json'
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _apply_env(cfg: dict) -> None:
    """环境变量覆盖（优先级高于文件）。"""
    for env_key, cfg_key in [
        ('LLM_ENDPOINT', 'endpoint'),
        ('LLM_API_KEY',  'api_key'),
        ('LLM_MODEL',    'model'),
        ('LLM_TIMEOUT',  'timeout'),
    ]:
        val = os.environ.get(env_key)
        if val is not None:
            cfg[cfg_key] = int(val) if cfg_key == 'timeout' else val


def is_configured() -> bool:
    with _lock:
        return _config is not None


def get_config():
    """返回当前配置字典快照，未配置时返回 None。"""
    with _lock:
        return dict(_config) if _config else None


def save_config(cfg: dict) -> None:
    """将配置写入 llm_config.json，然后热重载。"""
    if _base_dir is None:
        raise RuntimeError('llm_client 未初始化')
    p = _base_dir / 'llm_config.json'
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    reload_config()


# ── HTTP 层（纯 stdlib，绕过系统代理）──────────────────────────

def _is_anthropic_endpoint(endpoint: str) -> bool:
    """根据 endpoint URL 判断是否为 Anthropic API 格式。"""
    return 'anthropic' in endpoint.lower()


def _normalize_endpoint(endpoint: str, use_anthropic: bool) -> str:
    """
    Anthropic 格式的完整调用路径必须以 /v1/messages 结尾。
    用户常见误操作：只填写了 base URL（如 .../api/anthropic），自动补全。
    """
    if not use_anthropic:
        return endpoint
    clean = endpoint.rstrip('/')
    if not clean.endswith('/messages'):
        clean = clean + '/v1/messages'
    return clean


def _build_request(endpoint: str, api_key: str, payload: dict,
                   use_anthropic: bool) -> urllib.request.Request:
    """构造 urllib Request 对象。"""
    if use_anthropic:
        headers = {
            'Content-Type':    'application/json',
            'anthropic-version': '2023-06-01',
        }
        if api_key:
            headers['x-api-key'] = api_key
    else:
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return urllib.request.Request(endpoint, data=data, headers=headers, method='POST')


def _http_post(req: urllib.request.Request, timeout: int) -> dict:
    """
    发送 HTTP POST，返回解析后的 JSON dict。
    绕过系统代理（避免 http_proxy 环境变量干扰）。
    HTTP 错误时读取响应体并抛出包含详情的异常。
    """
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:300]
        raise RuntimeError(f'HTTP {e.code} {e.reason}: {body}') from None


def _parse_response(data: dict) -> str:
    """
    解析 LLM 响应体，兼容 OpenAI 与 Anthropic 格式。
    OpenAI:   data['choices'][0]['message']['content']
    Anthropic: data['content'][0]['text']
    失败时抛出 ValueError 含响应字段列表。
    """
    if 'choices' in data:
        return data['choices'][0]['message']['content']
    if 'content' in data:
        parts = data['content']
        if isinstance(parts, list):
            return ''.join(p.get('text', '') for p in parts
                           if isinstance(p, dict) and p.get('type') == 'text')
        return str(parts)
    raise ValueError(f'未知响应格式，字段: {list(data.keys())}')


def _make_payload(model: str, messages: list, temperature: float,
                  max_tokens: int, use_anthropic: bool) -> dict:
    if use_anthropic:
        # Anthropic API：system 消息不能放在 messages 数组里，
        # 必须作为顶层 "system" 字段传递（422 Unprocessable Entity 否则）
        system_text = ''
        filtered = []
        for m in messages:
            if m.get('role') == 'system':
                system_text += m.get('content', '')
            else:
                filtered.append(m)
        payload: dict = {
            'model':      model,
            'messages':   filtered,
            'max_tokens': max_tokens,
        }
        if system_text:
            payload['system'] = system_text
    else:
        payload = {
            'model':       model,
            'messages':    messages,
            'temperature': temperature,
            'max_tokens':  max_tokens,
        }
    return payload


# ── 公开 call_llm 接口 ──────────────────────────────────────

def call_llm_verbose(messages: list, temperature: float = 0.2,
                     max_tokens: int = 400) -> tuple:
    """
    调用 LLM API，含指数退避重试。
    返回 (result_str, error_str)：成功时 error_str 为 None，失败时 result_str 为 ''。
    用于连接测试等需要详细错误信息的场景。
    """
    with _lock:
        cfg = dict(_config) if _config else None
    if not cfg:
        return '', 'LLM 未配置'

    endpoint      = cfg['endpoint']
    api_key       = cfg.get('api_key', '')
    model         = cfg['model']
    timeout       = int(cfg.get('timeout', 30))
    retries       = int(cfg.get('llm_max_retries', 2))
    delay         = float(cfg.get('llm_retry_delay', 1))
    use_anthropic = _is_anthropic_endpoint(endpoint)
    endpoint      = _normalize_endpoint(endpoint, use_anthropic)

    payload = _make_payload(model, messages, temperature, max_tokens, use_anthropic)

    last_error = '未知错误'
    for attempt in range(retries + 1):
        try:
            req  = _build_request(endpoint, api_key, payload, use_anthropic)
            data = _http_post(req, timeout)
            result = _parse_response(data)
            return result, None
        except Exception as e:
            last_error = str(e)
            if attempt < retries:
                time.sleep(delay * (2 ** attempt))

    return '', last_error


def call_llm(messages: list, temperature: float = 0.2,
             max_tokens: int = 400) -> str:
    """
    调用 LLM API，含指数退避重试。失败返回空字符串，不抛异常。
    messages: [{"role": "user/system/assistant", "content": "..."}]
    """
    result, _ = call_llm_verbose(messages, temperature, max_tokens)
    return result


def call_llm_with_cache(messages: list, temperature: float = 0.2,
                        max_tokens: int = 400) -> str:
    """call_llm 的内存缓存包装。cache_ttl=0 禁用缓存，重启清空。"""
    with _lock:
        cfg = dict(_config) if _config else None
    if not cfg:
        return ''
    ttl = int(cfg.get('cache_ttl', 3600))

    key = hashlib.md5(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]

    if ttl > 0:
        with _lock:
            hit = _cache.get(key)
        if hit:
            result, expire = hit
            if time.time() < expire:
                return result

    result = call_llm(messages, temperature, max_tokens)

    if ttl > 0 and result:
        with _lock:
            _cache[key] = (result, time.time() + ttl)

    return result
