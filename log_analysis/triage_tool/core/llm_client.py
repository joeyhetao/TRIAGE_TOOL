# -*- coding: utf-8 -*-
"""
LLM API 客户端 — 无 Flask 依赖，可在任何线程调用。
纯 stdlib（urllib），不依赖 requests，适配离线内网环境。

配置文件 llm_config.json 现支持多 profile，schema：
  {
    "active_profile": "GLM-4.7",
    "profiles": [
      {"name": "GLM-4.7", "endpoint": "...", "api_key": "...", "model": "...",
       "timeout": 60, "context_window": 100000, "p3_max_lines": 2500, ...},
      {"name": "Qwen-Max", ...},
    ]
  }

向后兼容：旧版扁平格式（无 profiles 键）会在首次加载时自动迁移成单 profile。

公开接口（旧）：
  init(base_dir)                               — 加载并迁移 llm_config.json + 环境变量
  is_configured() -> bool                      — 当前激活 profile 的 endpoint + model 均非空
  call_llm(messages, temperature, max_tokens)  — 含超时重试，失败返回 ""
  call_llm_verbose(messages, ...)              — 同上，返回 (result, error_str) 供调试
  call_llm_with_cache(messages, ...)           — 内存缓存包装（md5 key，TTL 可配置）
  reload_config() -> bool                      — 热重载文件，返回是否已配置
  get_config() -> dict | None                  — 当前激活 profile 字段快照
  save_config(cfg: dict) -> None               — 用 cfg 字段更新当前激活 profile 并热重载

公开接口（新增 — 多 profile 管理）：
  get_all_profiles() -> list                   — 全部 profile 列表（不脱敏）
  get_active_profile_name() -> str             — 当前激活 profile 名
  add_profile(profile: dict) -> str            — 添加新 profile，返回错误信息（''=成功）
  update_profile(name, fields) -> str          — 更新指定 profile（含 rename）
  delete_profile(name) -> str                  — 删除 profile（拒绝删最后一个）
  activate_profile(name) -> str                — 切换激活 + 热重载

支持 API 格式（自动检测）：
  OpenAI 兼容格式  — endpoint 不含 "anthropic"
  Anthropic 格式  — endpoint 含 "anthropic"
"""
import json
import time
import hashlib
import threading
import os
import urllib.request
import urllib.error
from pathlib import Path

_lock          = threading.Lock()
# _mutation_lock 序列化 profile CRUD 的 read→mutate→write→reload 整段流程，
# 与 _lock 区分（_lock 保护 _config / _cache 短临界区，不会被磁盘 IO 拖慢）。
# 历史 bug H-2（2026-05-11 审查）：之前 write_file 与 reload_config 之间
# 无外层锁，并发改 profile 时磁盘内容与 _config 可能不对应。
_mutation_lock = threading.Lock()
_config        = None    # dict | None — 当前激活 profile（已注入默认 + env 覆盖）
_profiles_raw  = []      # list[dict] — 文件原始 profile 列表（不含默认值）
_active_name   = ''      # str       — 当前激活 profile 名
_cache         = {}      # md5[:16] -> (result_str, expire_ts)
_base_dir      = None    # Path
_last_load_error = ''    # str    — 上次 _load_file 失败原因（供 /llm/get_config 暴露）

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

# Profile 允许的字段白名单——其他字段在 update_profile / add_profile 时被丢弃，
# 防止 schema 污染（M-6, 2026-05-11 审查）。'name' 由调用方单独处理。
_PROFILE_ALLOWED_FIELDS = frozenset(_DEFAULTS) | {
    'name', 'endpoint', 'api_key', 'model',
}


def _clean_profile_fields(fields: dict) -> dict:
    """筛掉白名单外的键。保留所有合法字段（含 name）。"""
    return {k: v for k, v in fields.items() if k in _PROFILE_ALLOWED_FIELDS}


def init(base_dir: Path) -> None:
    """加载配置文件，应用环境变量覆盖。在 BASE_DIR 确定后调用一次。"""
    global _base_dir
    _base_dir = Path(base_dir)
    reload_config()


def reload_config() -> bool:
    """重载 llm_config.json + 环境变量覆盖。返回是否已配置（激活 profile 有效）。"""
    global _config, _profiles_raw, _active_name
    raw = _load_file()
    profiles, active = _migrate_or_validate(raw)

    # 写回迁移结果（只在文件原本是旧格式时触发；新格式 round-trip 写入也是幂等的）
    if profiles and 'profiles' not in raw:
        _write_file({'active_profile': active, 'profiles': profiles})

    # 选出当前激活 profile（可能为空）
    active_profile = next((dict(p) for p in profiles if p.get('name') == active), None)
    if active_profile is None and profiles:
        # 激活名指向不存在的 profile，回退到首个
        active_profile = dict(profiles[0])
        active = active_profile.get('name', '')

    if active_profile:
        _apply_env(active_profile)
        configured = bool(active_profile.get('endpoint') and active_profile.get('model'))
    else:
        configured = False

    with _lock:
        _profiles_raw = [dict(p) for p in profiles]
        _active_name  = active or ''
        if configured:
            for k, v in _DEFAULTS.items():
                active_profile.setdefault(k, v)
            _config = active_profile
        else:
            _config = None
    return configured


def _load_file() -> dict:
    """读取 llm_config.json。解析失败时把损坏文件改名备份后再返回 {}。

    历史 bug H-3（2026-05-11 审查）：JSON 误删一个逗号即触发解析失败
    返回 {}，用户在 UI 重新填表保存时，原文件（含 API key 等）会被静默
    覆盖丢失。现在解析失败先 rename 到 ``llm_config.json.broken.{ts}``
    再返回 {}，错误原因留在 _last_load_error 供 /llm/get_config 暴露。
    """
    global _last_load_error
    if _base_dir is None:
        return {}
    p = _base_dir / 'llm_config.json'
    if not p.exists():
        _last_load_error = ''
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        _last_load_error = ''
        return data
    except Exception as e:
        try:
            broken = p.with_name(f'llm_config.json.broken.{int(time.time())}')
            p.rename(broken)
            _last_load_error = (
                f'llm_config.json 解析失败，原文件已备份到 {broken.name}: {e}'
            )
        except OSError as rename_err:
            _last_load_error = (
                f'llm_config.json 解析失败 且备份失败（请手工备份后修复）: '
                f'{e}; rename error: {rename_err}'
            )
        return {}


def get_last_load_error() -> str:
    """返回上次 _load_file 的失败原因字符串（无错误时为 ''）。"""
    return _last_load_error


def _write_file(data: dict) -> None:
    """原子写：先写到 .tmp 再 rename，避免半截文件。"""
    if _base_dir is None:
        return
    p   = _base_dir / 'llm_config.json'
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(p)


def _migrate_or_validate(raw: dict) -> tuple:
    """
    把任意 raw 规整为 (profiles, active_name)。
    - 旧扁平格式（含 endpoint/model 顶层字段）→ 包成单 profile
    - 新格式 → 直接返回，但兜底校验（active 名指向不存在时回退首个）
    - 空文件 / 不可解析 → ([], '')
    """
    if not raw:
        return [], ''

    if 'profiles' in raw and isinstance(raw.get('profiles'), list):
        profiles = [dict(p) for p in raw['profiles'] if isinstance(p, dict)]
        # 确保每个 profile 有 name
        for p in profiles:
            if not p.get('name'):
                p['name'] = p.get('model') or 'unnamed'
        # 去重 name（重名后缀 _2、_3 ...）
        seen = {}
        for p in profiles:
            base = p['name']
            i = 1
            while p['name'] in seen:
                i += 1
                p['name'] = f'{base}_{i}'
            seen[p['name']] = True
        active = raw.get('active_profile', '')
        return profiles, active

    # 老扁平格式：endpoint/model 在顶层
    if raw.get('endpoint') or raw.get('model'):
        name = raw.get('model') or 'default'
        single = dict(raw)
        single['name'] = name
        # 顶层别留 active_profile/profiles 残余键
        for k in ('active_profile', 'profiles'):
            single.pop(k, None)
        return [single], name

    return [], ''


def _apply_env(cfg: dict) -> None:
    """环境变量覆盖（优先级高于 profile 字段）。"""
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
    """返回当前激活 profile 的字段快照（含默认值 + env 覆盖）。"""
    with _lock:
        return dict(_config) if _config else None


def get_all_profiles() -> list:
    """返回所有 profile 字段的深拷贝（不脱敏 api_key——内部使用）。"""
    with _lock:
        return [dict(p) for p in _profiles_raw]


def get_active_profile_name() -> str:
    """当前激活 profile 名。未配置时返回空串。"""
    with _lock:
        return _active_name


def save_config(cfg: dict) -> None:
    """
    用 cfg 字段更新当前激活 profile，然后写文件 + 热重载。
    向后兼容旧调用：传单个 profile 字段（无 'profiles' 键）。
    若文件无任何 profile，则把 cfg 当作首个 profile 创建。

    H-2（2026-05-11）：整段 read→mutate→write→reload 用 _mutation_lock
    串行化，避免并发改 profile 时磁盘内容与 _config 不对应。
    """
    if _base_dir is None:
        raise RuntimeError('llm_client 未初始化')

    cfg = _clean_profile_fields(cfg)
    with _mutation_lock:
        raw = _load_file()
        profiles, active = _migrate_or_validate(raw)

        # 提取 cfg 里可能的 name 字段
        new_name = cfg.get('name', '').strip() or active or cfg.get('model') or 'default'
        new_profile = {k: v for k, v in cfg.items() if k != 'name'}
        new_profile['name'] = new_name

        if not profiles:
            # 空配置 — 把 cfg 当作首个 profile
            profiles = [new_profile]
            active   = new_name
        else:
            # 找到激活 profile 替换；若新 name 与激活不同则视为 rename
            idx = next((i for i, p in enumerate(profiles)
                        if p.get('name') == active), None)
            if idx is None:
                # 激活指针失效——退回首个
                idx = 0
            # 若 rename 到一个已存在的别的 profile 的 name，拒绝
            if new_name != profiles[idx].get('name') and \
                    any(p.get('name') == new_name for i, p in enumerate(profiles) if i != idx):
                raise ValueError(f'profile 名 {new_name!r} 已存在')
            profiles[idx] = new_profile
            active = new_name

        _write_file({'active_profile': active, 'profiles': profiles})
        reload_config()


def add_profile(profile: dict) -> str:
    """
    添加新 profile。失败返回错误说明（''=成功）。
    profile 必须含 name；name 必须唯一。
    M-6/H-2（2026-05-11）：白名单过滤未知字段 + _mutation_lock 串行化。
    """
    if _base_dir is None:
        return 'llm_client 未初始化'
    profile = _clean_profile_fields(profile)
    name = (profile.get('name') or '').strip()
    if not name:
        return 'profile 名不能为空'

    with _mutation_lock:
        raw = _load_file()
        profiles, active = _migrate_or_validate(raw)
        if any(p.get('name') == name for p in profiles):
            return f'profile 名 {name!r} 已存在'

        profiles.append({**profile, 'name': name})
        if not active:
            active = name   # 第一个 profile 自动激活
        _write_file({'active_profile': active, 'profiles': profiles})
        reload_config()
    return ''


def update_profile(name: str, fields: dict) -> str:
    """
    更新指定 profile。fields 含 'name' 时表示 rename。失败返回错误说明。
    M-6/H-2（2026-05-11）：白名单过滤未知字段 + _mutation_lock 串行化。
    """
    if _base_dir is None:
        return 'llm_client 未初始化'

    fields = _clean_profile_fields(fields)
    with _mutation_lock:
        raw = _load_file()
        profiles, active = _migrate_or_validate(raw)

        idx = next((i for i, p in enumerate(profiles)
                    if p.get('name') == name), None)
        if idx is None:
            return f'未找到 profile {name!r}'

        new_name = (fields.get('name') or name).strip() or name
        if new_name != name and \
                any(p.get('name') == new_name for i, p in enumerate(profiles) if i != idx):
            return f'profile 名 {new_name!r} 已存在'

        merged = {**profiles[idx], **fields, 'name': new_name}
        profiles[idx] = merged
        if active == name:
            active = new_name

        _write_file({'active_profile': active, 'profiles': profiles})
        reload_config()
    return ''


def delete_profile(name: str) -> str:
    """删除 profile。拒绝删最后一个。失败返回错误说明。
    H-2（2026-05-11）：_mutation_lock 串行化。"""
    if _base_dir is None:
        return 'llm_client 未初始化'

    with _mutation_lock:
        raw = _load_file()
        profiles, active = _migrate_or_validate(raw)

        if not profiles:
            return '当前无任何 profile'
        if len(profiles) <= 1:
            return '至少保留一个 profile，不能删除最后一个'

        idx = next((i for i, p in enumerate(profiles)
                    if p.get('name') == name), None)
        if idx is None:
            return f'未找到 profile {name!r}'

        profiles.pop(idx)
        if active == name:
            active = profiles[0].get('name', '')   # 删的是激活的——切换到首个

        _write_file({'active_profile': active, 'profiles': profiles})
        reload_config()
    return ''


def activate_profile(name: str) -> str:
    """切换激活 profile。失败返回错误说明。
    H-2（2026-05-11）：_mutation_lock 串行化。"""
    if _base_dir is None:
        return 'llm_client 未初始化'

    with _mutation_lock:
        raw = _load_file()
        profiles, _active = _migrate_or_validate(raw)

        if not any(p.get('name') == name for p in profiles):
            return f'未找到 profile {name!r}'

        _write_file({'active_profile': name, 'profiles': profiles})
        reload_config()
    return ''


# ── HTTP 层（纯 stdlib，绕过系统代理）──────────────────────────

def _is_anthropic_endpoint(endpoint: str) -> bool:
    """根据 endpoint URL 判断是否为 Anthropic API 格式。"""
    return 'anthropic' in endpoint.lower()


def _normalize_endpoint(endpoint: str, use_anthropic: bool) -> str:
    """
    自动补全 endpoint 路径：
    - Anthropic 格式：末尾补全为 /v1/messages
    - OpenAI 兼容格式：若末尾为 /v1 或 /v1/，自动追加 /chat/completions
    """
    clean = endpoint.rstrip('/')
    if use_anthropic:
        if not clean.endswith('/messages'):
            clean = clean + '/v1/messages'
    else:
        if clean.endswith('/v1'):
            clean = clean + '/chat/completions'
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
    M-1（2026-05-11）：优先识别 {"error": {...}} / type=='error'，把
    LLM 自报的业务错误（rate limit / invalid model / context too large）
    透传出来，而不是丢"未知响应格式 字段:['error']"这种无用信息。
    """
    err = data.get('error')
    if isinstance(err, dict):
        msg = err.get('message') or err.get('type') or json.dumps(err, ensure_ascii=False)
        raise RuntimeError(f'LLM 接口错误: {msg}')
    if data.get('type') == 'error':       # Anthropic 顶层错误形态
        err = data.get('error', {})
        msg = err.get('message') if isinstance(err, dict) else str(err or data)
        raise RuntimeError(f'LLM 接口错误: {msg}')

    if 'choices' in data:
        msg = data['choices'][0]['message']
        return msg.get('content') or msg.get('reasoning') or ''
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
