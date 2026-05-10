# -*- coding: utf-8 -*-
"""
多 profile 管理 + 迁移测试。

涉及 core.llm_client 的：
  _migrate_or_validate, get_all_profiles, get_active_profile_name,
  add_profile, update_profile, delete_profile, activate_profile, save_config
"""
import json
import importlib
import sys
from pathlib import Path


def _setup(tmp_path):
    """每个测试新建临时 BASE_DIR + reload llm_client。"""
    if 'core.llm_client' in sys.modules:
        del sys.modules['core.llm_client']
    if 'core' in sys.modules:
        del sys.modules['core']
    from core import llm_client as lc
    lc.init(tmp_path)
    return lc


# ══════════════════════════════════════════════════════════════
# 配置文件迁移：旧扁平格式 → 新 profiles 列表
# ══════════════════════════════════════════════════════════════

class TestMigration:
    def test_old_flat_config_migrates(self, tmp_path):
        cfg_path = tmp_path / 'llm_config.json'
        cfg_path.write_text(json.dumps({
            'endpoint': 'https://example.com/v1',
            'api_key':  'sk-old',
            'model':    'GLM-4.7',
            'context_window': 100000,
            'p3_max_lines': 2500,
        }), encoding='utf-8')
        lc = _setup(tmp_path)

        # 文件应已被迁移成新格式
        new_raw = json.loads(cfg_path.read_text(encoding='utf-8'))
        assert 'profiles' in new_raw
        assert 'active_profile' in new_raw
        assert new_raw['active_profile'] == 'GLM-4.7'
        assert len(new_raw['profiles']) == 1
        assert new_raw['profiles'][0]['name'] == 'GLM-4.7'
        assert new_raw['profiles'][0]['endpoint'] == 'https://example.com/v1'

        # 公开 API 应反映迁移结果
        assert lc.is_configured()
        cfg = lc.get_config()
        assert cfg['model'] == 'GLM-4.7'
        assert cfg['endpoint'] == 'https://example.com/v1'
        assert lc.get_active_profile_name() == 'GLM-4.7'
        assert len(lc.get_all_profiles()) == 1

    def test_new_format_unchanged(self, tmp_path):
        cfg_path = tmp_path / 'llm_config.json'
        cfg_path.write_text(json.dumps({
            'active_profile': 'B',
            'profiles': [
                {'name': 'A', 'endpoint': 'http://a/v1', 'model': 'a'},
                {'name': 'B', 'endpoint': 'http://b/v1', 'model': 'b'},
            ],
        }), encoding='utf-8')
        lc = _setup(tmp_path)
        assert lc.get_active_profile_name() == 'B'
        cfg = lc.get_config()
        assert cfg['model'] == 'b'

    def test_active_pointer_invalid_falls_back_first(self, tmp_path):
        cfg_path = tmp_path / 'llm_config.json'
        cfg_path.write_text(json.dumps({
            'active_profile': 'NOT_EXIST',
            'profiles': [
                {'name': 'A', 'endpoint': 'http://a/v1', 'model': 'a'},
                {'name': 'B', 'endpoint': 'http://b/v1', 'model': 'b'},
            ],
        }), encoding='utf-8')
        lc = _setup(tmp_path)
        # 应回退到首个 profile
        assert lc.get_active_profile_name() == 'A'

    def test_empty_file_no_config(self, tmp_path):
        lc = _setup(tmp_path)
        assert not lc.is_configured()
        assert lc.get_active_profile_name() == ''
        assert lc.get_all_profiles() == []


# ══════════════════════════════════════════════════════════════
# Profile CRUD
# ══════════════════════════════════════════════════════════════

class TestProfileCRUD:
    def _seed_two(self, tmp_path):
        cfg_path = tmp_path / 'llm_config.json'
        cfg_path.write_text(json.dumps({
            'active_profile': 'A',
            'profiles': [
                {'name': 'A', 'endpoint': 'http://a/v1', 'model': 'a',
                 'api_key': 'ka', 'p3_max_lines': 2500, 'context_window': 100000},
                {'name': 'B', 'endpoint': 'http://b/v1', 'model': 'b',
                 'api_key': 'kb', 'p3_max_lines': 1500, 'context_window': 32000},
            ],
        }), encoding='utf-8')
        return _setup(tmp_path)

    def test_add_profile_appends(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.add_profile({'name': 'C', 'endpoint': 'http://c/v1', 'model': 'c',
                              'api_key': 'kc', 'context_window': 64000, 'p3_max_lines': 2000})
        assert err == ''
        assert len(lc.get_all_profiles()) == 3
        names = [p['name'] for p in lc.get_all_profiles()]
        assert names == ['A', 'B', 'C']
        # add 不切换激活
        assert lc.get_active_profile_name() == 'A'

    def test_add_duplicate_name_rejected(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.add_profile({'name': 'A', 'endpoint': 'http://x/v1', 'model': 'x'})
        assert 'A' in err and ('已存在' in err or 'exist' in err.lower())
        # 列表保持不变
        assert len(lc.get_all_profiles()) == 2

    def test_add_empty_name_rejected(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.add_profile({'name': '', 'endpoint': 'http://x/v1', 'model': 'x'})
        assert err  # 非空错误说明
        assert len(lc.get_all_profiles()) == 2

    def test_update_active_changes_fields(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.update_profile('A', {'endpoint': 'http://a-new/v1', 'p3_max_lines': 3000})
        assert err == ''
        cfg = lc.get_config()
        assert cfg['endpoint'] == 'http://a-new/v1'
        assert cfg['p3_max_lines'] == 3000
        assert lc.get_active_profile_name() == 'A'

    def test_update_with_rename_changes_active_pointer(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.update_profile('A', {'name': 'A_renamed'})
        assert err == ''
        # 激活指针应跟着改
        assert lc.get_active_profile_name() == 'A_renamed'
        names = [p['name'] for p in lc.get_all_profiles()]
        assert 'A_renamed' in names and 'A' not in names

    def test_update_rename_to_existing_rejected(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.update_profile('A', {'name': 'B'})
        assert err  # 应有"已存在"类错误
        # 原结构未变
        assert lc.get_active_profile_name() == 'A'

    def test_update_nonexistent_name(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.update_profile('NOPE', {'endpoint': 'http://x'})
        assert err
        assert 'NOPE' in err or '未找到' in err

    def test_delete_non_active(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.delete_profile('B')
        assert err == ''
        assert len(lc.get_all_profiles()) == 1
        assert lc.get_active_profile_name() == 'A'

    def test_delete_active_switches_to_first(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.delete_profile('A')
        assert err == ''
        # 列表里只剩 B，激活自动跟过去
        names = [p['name'] for p in lc.get_all_profiles()]
        assert names == ['B']
        assert lc.get_active_profile_name() == 'B'

    def test_delete_last_refused(self, tmp_path):
        lc = self._seed_two(tmp_path)
        # 先删一个
        lc.delete_profile('B')
        # 再删最后一个 — 应被拒
        err = lc.delete_profile('A')
        assert err  # 应有"至少保留一个"类错误
        assert len(lc.get_all_profiles()) == 1

    def test_activate_switches_active(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.activate_profile('B')
        assert err == ''
        assert lc.get_active_profile_name() == 'B'
        cfg = lc.get_config()
        assert cfg['model'] == 'b'
        assert cfg['endpoint'] == 'http://b/v1'

    def test_activate_nonexistent(self, tmp_path):
        lc = self._seed_two(tmp_path)
        err = lc.activate_profile('NOPE')
        assert err  # 应有"未找到"类错误
        # 激活未变
        assert lc.get_active_profile_name() == 'A'

    def test_save_config_compat_updates_active(self, tmp_path):
        """旧调用方传单个 profile 字段 → 应更新当前激活 profile。"""
        lc = self._seed_two(tmp_path)
        lc.save_config({
            'endpoint': 'http://updated/v1',
            'model':    'a',
            'api_key':  'new-key',
            'context_window': 200000,
            'p3_max_lines':   5000,
        })
        cfg = lc.get_config()
        assert cfg['endpoint'] == 'http://updated/v1'
        assert cfg['context_window'] == 200000
        # 激活仍是 A，列表仍是 A+B
        assert lc.get_active_profile_name() == 'A'
        assert len(lc.get_all_profiles()) == 2

    def test_p3_max_lines_independent_per_profile(self, tmp_path):
        """每 profile 的 p3_max_lines 必须独立。"""
        lc = self._seed_two(tmp_path)
        # A 设 3000；B 设 5000
        lc.update_profile('A', {'p3_max_lines': 3000})
        lc.update_profile('B', {'p3_max_lines': 5000})
        # 切到 A
        lc.activate_profile('A')
        assert lc.get_config()['p3_max_lines'] == 3000
        # 切到 B
        lc.activate_profile('B')
        assert lc.get_config()['p3_max_lines'] == 5000
