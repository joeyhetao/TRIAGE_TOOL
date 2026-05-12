# -*- coding: utf-8 -*-
"""
2026-05-11 第三轮代码审查的回归测试

覆盖：
  C-1: _running_jobs_summary 读 status（不是 phase）—— P6 互锁
  H-1: _parse_json_safe 带括号配对，能处理 "解释 + JSON" 混合输出
  H-3: 损坏 llm_config.json 被备份后再返回 {}，旧文件不丢
"""
import json
import sys
import time
import pytest


def _reload_llm_client(tmp_path):
    """清旧 module 缓存，新建临时 BASE_DIR + reload llm_client。"""
    for mod in ('core.llm_client', 'core'):
        if mod in sys.modules:
            del sys.modules[mod]
    from core import llm_client as lc
    lc.init(tmp_path)
    return lc


# ══════════════════════════════════════════════════════════════
# C-1：P6 review job 互锁字段命名
# ══════════════════════════════════════════════════════════════

class TestC1ReviewJobLockField:
    """_running_jobs_summary 必须读 status 字段（_run_review_job 写的就是 status）。
    若回到 'phase' 读法，所有 P6 任务会被漏报，profile 切换互锁失效。"""

    def test_running_review_job_is_visible_to_summary(self, tmp_path):
        from blueprints import llm_bp
        # 手工塞一个"正在跑"的 P6 任务，模拟实际运行场景的字段命名
        with llm_bp._review_lock:
            llm_bp._review_jobs.clear()
            llm_bp._review_jobs['job-running'] = {
                'status': 'running', 'group': 'UVM_ERROR',
                'done': 1, 'total': 3, 'ts': time.time(),
                'suspect_pairs': [], 'skipped': 0,
            }
        try:
            summary = llm_bp._running_jobs_summary()
            assert len(summary) == 1, '正在运行的 P6 任务必须被 _running_jobs_summary 看到'
            assert summary[0]['kind']   == 'P6_kb_review'
            assert summary[0]['job_id'] == 'job-running'
            assert summary[0]['phase']  == 'running'  # 字段名 phase 是前端兼容字段
        finally:
            with llm_bp._review_lock:
                llm_bp._review_jobs.clear()

    def test_terminal_states_excluded(self, tmp_path):
        from blueprints import llm_bp
        with llm_bp._review_lock:
            llm_bp._review_jobs.clear()
            for s in ('done', 'error', 'stopped'):
                llm_bp._review_jobs[f'job-{s}'] = {
                    'status': s, 'done': 1, 'total': 1, 'ts': time.time(),
                    'suspect_pairs': [], 'skipped': 0,
                }
        try:
            summary = llm_bp._running_jobs_summary()
            assert summary == [], '终态 job 不应出现在 running summary'
        finally:
            with llm_bp._review_lock:
                llm_bp._review_jobs.clear()

    def test_pending_state_excluded(self):
        """pending 是初始化态，还没开始跑——不算 running。"""
        from blueprints import llm_bp
        with llm_bp._review_lock:
            llm_bp._review_jobs.clear()
            llm_bp._review_jobs['j'] = {
                'status': 'pending', 'done': 0, 'total': 1, 'ts': time.time(),
            }
        try:
            # pending 不在 ('done','error','stopped') 里，会被算 running
            summary = llm_bp._running_jobs_summary()
            assert len(summary) == 1
        finally:
            with llm_bp._review_lock:
                llm_bp._review_jobs.clear()


# ══════════════════════════════════════════════════════════════
# H-1：_parse_json_safe 括号配对解析
# ══════════════════════════════════════════════════════════════

class TestH1JsonParser:
    def setup_method(self):
        from blueprints.llm_bp import _parse_json_safe
        self.parse = _parse_json_safe

    def test_clean_json(self):
        assert self.parse('{"a": 1}') == {'a': 1}

    def test_explanation_before_json(self):
        text = '好的，我的判断是：{"ranked": [0, 1]}'
        assert self.parse(text) == {'ranked': [0, 1]}

    def test_explanation_after_json(self):
        text = '{"valid": 1} 解释一下：上面这条最相关'
        assert self.parse(text) == {'valid': 1}

    def test_explanation_with_braces_in_text(self):
        """LLM 在 JSON 之后又用 { } 举例——旧贪婪正则会把这种吃进来导致失败。"""
        text = '{"ok": true}\n例如 { a 不等于 b } 时报错'
        assert self.parse(text) == {'ok': True}

    def test_markdown_fence_stripped(self):
        text = '这是结果：\n```json\n{"x": 42}\n```\n完毕'
        assert self.parse(text) == {'x': 42}

    def test_markdown_fence_no_lang_tag(self):
        text = '```\n{"y": 99}\n```'
        assert self.parse(text) == {'y': 99}

    def test_nested_object(self):
        text = '{"outer": {"inner": 1, "list": [1, 2, 3]}}'
        assert self.parse(text) == {'outer': {'inner': 1, 'list': [1, 2, 3]}}

    def test_array_mode(self):
        text = '排序结果：[2, 0, 1]'
        assert self.parse(text, array=True) == [2, 0, 1]

    def test_brace_inside_string(self):
        """字符串内的 } 不应被视作闭合括号。"""
        text = '{"msg": "this has } inside"}'
        assert self.parse(text) == {'msg': 'this has } inside'}

    def test_escaped_quote_in_string(self):
        text = '{"msg": "say \\"hi\\""}'
        assert self.parse(text) == {'msg': 'say "hi"'}

    def test_invalid_json_returns_none(self):
        assert self.parse('{not valid json}') is None

    def test_no_braces_returns_none(self):
        assert self.parse('just plain text') is None

    def test_empty_input(self):
        assert self.parse('') is None
        assert self.parse(None) is None  # type: ignore

    def test_multiple_objects_returns_first(self):
        """LLM 偶尔返回多个 JSON，应取第一个完整对象。"""
        text = '{"a": 1} 然后 {"b": 2}'
        assert self.parse(text) == {'a': 1}


# ══════════════════════════════════════════════════════════════
# H-3：损坏 llm_config.json 备份机制
# ══════════════════════════════════════════════════════════════

class TestH3BrokenConfigBackup:
    def test_corrupt_json_is_backed_up(self, tmp_path):
        cfg_path = tmp_path / 'llm_config.json'
        cfg_path.write_text(
            '{"active_profile": "GLM-4.7", "profiles": [{name "broken"]}',
            encoding='utf-8',
        )
        lc = _reload_llm_client(tmp_path)
        # 原 llm_config.json 应被改名
        assert not cfg_path.exists(), '损坏的 config 应该被改名'
        # 应该存在以 .broken. 开头的备份
        backups = list(tmp_path.glob('llm_config.json.broken.*'))
        assert len(backups) == 1
        # 备份内容应该完整（与原始坏 JSON 一致）
        assert 'name "broken"' in backups[0].read_text(encoding='utf-8')
        # 错误原因应可通过 API 拿到
        assert 'llm_config.json 解析失败' in lc.get_last_load_error()
        # 未配置状态
        assert not lc.is_configured()
        assert lc.get_all_profiles() == []

    def test_valid_json_does_not_set_error(self, tmp_path):
        cfg_path = tmp_path / 'llm_config.json'
        cfg_path.write_text(
            json.dumps({
                'active_profile': 'X',
                'profiles': [{'name': 'X', 'endpoint': 'https://x.com', 'model': 'm'}],
            }), encoding='utf-8',
        )
        lc = _reload_llm_client(tmp_path)
        assert lc.get_last_load_error() == ''
        assert lc.is_configured()

    def test_missing_file_does_not_set_error(self, tmp_path):
        lc = _reload_llm_client(tmp_path)
        assert lc.get_last_load_error() == ''
        assert not lc.is_configured()

    def test_save_after_broken_does_not_clobber_backup(self, tmp_path):
        """损坏文件被备份后，用户重填表单保存——备份必须仍在原处。"""
        cfg_path = tmp_path / 'llm_config.json'
        cfg_path.write_text('{ bad json', encoding='utf-8')
        lc = _reload_llm_client(tmp_path)
        backups_before = list(tmp_path.glob('llm_config.json.broken.*'))
        assert len(backups_before) == 1
        backup_content = backups_before[0].read_text(encoding='utf-8')

        # 用户在 UI 里重新填表保存
        err = lc.add_profile({
            'name': 'NewProfile', 'endpoint': 'https://new.com',
            'model': 'm', 'api_key': 'k',
        })
        assert err == ''
        # 备份应该完好无损
        backups_after = list(tmp_path.glob('llm_config.json.broken.*'))
        assert len(backups_after) == 1
        assert backups_after[0].read_text(encoding='utf-8') == backup_content
        # 新 config 也应已落盘
        new_data = json.loads(cfg_path.read_text(encoding='utf-8'))
        assert new_data['active_profile'] == 'NewProfile'


# ══════════════════════════════════════════════════════════════
# M-6：profile 字段白名单（顺手测）
# ══════════════════════════════════════════════════════════════

class TestM6ProfileFieldWhitelist:
    def test_unknown_field_stripped(self, tmp_path):
        lc = _reload_llm_client(tmp_path)
        err = lc.add_profile({
            'name': 'p1', 'endpoint': 'https://x.com', 'model': 'm',
            'api_key': 'k',
            'malicious_field': 'should be dropped',     # 非白名单字段
            '__proto__':       'evil',                  # JS-style 攻击姿势
        })
        assert err == ''
        prof = lc.get_all_profiles()[0]
        assert 'malicious_field' not in prof
        assert '__proto__' not in prof
        assert prof['name'] == 'p1'
        assert prof['endpoint'] == 'https://x.com'

    def test_known_fields_preserved(self, tmp_path):
        lc = _reload_llm_client(tmp_path)
        err = lc.add_profile({
            'name': 'p', 'endpoint': 'https://x.com', 'model': 'm',
            'api_key': 'k', 'timeout': 60, 'context_window': 200000,
            'p3_max_lines': 5000,
        })
        assert err == ''
        prof = lc.get_all_profiles()[0]
        assert prof['timeout'] == 60
        assert prof['context_window'] == 200000
        assert prof['p3_max_lines'] == 5000
