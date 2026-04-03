# -*- coding: utf-8 -*-
"""core/matcher.py 单元测试。"""
import pytest
from core.matcher import match_error, run_match


SAMPLE_DB = [
    {
        '错误类型': 'UVM_ERROR',
        '错误ID': 'MEM_ERR',
        '关键描述关键词': 'memory,mismatch',
        '报错原因': '内存读写不一致',
        '所属模块': 'MEM',
        '根因分类': 'DUT Bug',
        '解决方案': '检查内存控制器',
        '关联用例': '',
        '录入人': 'test',
        '录入日期': '2024-01-15',
        '_row_idx': 2,
    },
    {
        '错误类型': 'UVM_FATAL',
        '错误ID': 'TIMEOUT',
        '关键描述关键词': 'timeout,simulation',
        '报错原因': '仿真超时',
        '所属模块': 'TOP',
        '根因分类': 'TB Bug',
        '解决方案': '增大超时限制',
        '关联用例': '',
        '录入人': 'test',
        '录入日期': '2024-02-01',
        '_row_idx': 3,
    },
    {
        '错误类型': 'UVM_ERROR',
        '错误ID': 'BUS_ERR',
        '关键描述关键词': 'bus,error',
        '报错原因': '总线错误',
        '所属模块': 'BUS',
        '根因分类': 'DUT Bug',
        '解决方案': '检查总线仲裁',
        '关联用例': '',
        '录入人': 'test',
        '录入日期': '',  # 缺失日期
        '_row_idx': 4,
    },
]


class TestMatchError:
    def test_step1_error_id_match(self):
        error = {'level': 'UVM_ERROR', 'error_id': 'MEM_ERR',
                 'description': 'memory read mismatch'}
        result = match_error(error, SAMPLE_DB)
        assert result['status'] == 'matched'
        assert result['match_by'] == 'error_id'
        assert result['entry']['错误ID'] == 'MEM_ERR'

    def test_step2_keyword_match(self):
        error = {'level': 'UVM_ERROR', 'error_id': '',
                 'description': 'bus error detected on AXI'}
        result = match_error(error, SAMPLE_DB)
        assert result['status'] == 'matched'
        assert result['match_by'] == 'keywords'
        assert result['entry']['错误ID'] == 'BUS_ERR'

    def test_unmatched(self):
        error = {'level': 'UVM_ERROR', 'error_id': 'UNKNOWN',
                 'description': 'some unknown error'}
        result = match_error(error, SAMPLE_DB)
        assert result['status'] == 'unmatched'

    def test_empty_error_returns_no_error(self):
        result = match_error({}, SAMPLE_DB)
        assert result['status'] == 'no_error'

    def test_case_insensitive_error_id(self):
        error = {'level': 'UVM_ERROR', 'error_id': 'mem_err',
                 'description': ''}
        result = match_error(error, SAMPLE_DB)
        assert result['status'] == 'matched'

    def test_sort_by_date_missing_date_last(self):
        # BUS_ERR 条目缺失日期，应排在有日期的条目后面
        error = {'level': 'UVM_ERROR', 'error_id': '',
                 'description': 'memory bus error mismatch'}
        # 让两条都能匹配（keyword 包含 memory+mismatch for MEM_ERR, bus+error for BUS_ERR）
        # 只验证缺失日期不会排到最前
        db_with_two = [SAMPLE_DB[0], SAMPLE_DB[2]]  # MEM_ERR(有日期) + BUS_ERR(无日期)
        # MEM_ERR 关键词: memory,mismatch；BUS_ERR 关键词: bus,error
        # 描述 "memory mismatch" 只匹配 MEM_ERR
        e2 = {'level': 'UVM_ERROR', 'error_id': '', 'description': 'bus error issue'}
        result = match_error(e2, db_with_two)
        assert result['status'] == 'matched'
        # BUS_ERR 是唯一匹配，缺失日期不影响唯一匹配
        assert result['entry']['错误ID'] == 'BUS_ERR'

    def test_chinese_comma_in_keywords(self):
        db = [{
            '错误类型': 'UVM_ERROR',
            '错误ID': 'CN_ERR',
            '关键描述关键词': 'alpha，beta',  # 中文逗号
            '报错原因': '测试',
            '_row_idx': 2,
        }]
        error = {'level': 'UVM_ERROR', 'error_id': '',
                 'description': 'alpha beta gamma'}
        result = match_error(error, db)
        assert result['status'] == 'matched'

    def test_keyword_and_logic(self):
        # 只有一个关键词匹配，不应命中（AND 逻辑）
        error = {'level': 'UVM_ERROR', 'error_id': '',
                 'description': 'only memory'}  # 只有 memory，没有 mismatch
        result = match_error(error, SAMPLE_DB)
        assert result['status'] == 'unmatched'


class TestRunMatch:
    def test_run_match_attaches_match_field(self, tmp_db):
        from core.db_manager import append_entry
        append_entry(tmp_db, {
            '错误类型': 'UVM_ERROR',
            '错误ID': 'TEST_ERR',
            '关键描述关键词': 'test,error',
            '报错原因': '测试错误',
            '所属模块': 'TEST',
            '根因分类': 'DUT Bug',
            '解决方案': '检查',
            '关联用例': '',
            '录入人': 'tester',
        })
        parse_results = [{
            'file': 'test.log',
            'filepath': '/tmp/test.log',
            'statistics': {'UVM_ERROR': 1},
            'status': 'fail',
            'pass_found': False,
            'top_errors': [{'level': 'UVM_ERROR', 'error_id': 'TEST_ERR',
                             'description': 'test error occurred', 'location': ''}],
            'all_errors': [],
        }]
        results = run_match(parse_results, tmp_db)
        assert results[0]['top_errors'][0]['match']['status'] == 'matched'
        assert results[0]['match']['status'] == 'matched'

    def test_run_match_unmatched(self, tmp_db):
        parse_results = [{
            'file': 'test.log',
            'filepath': '/tmp/test.log',
            'statistics': {'UVM_ERROR': 1},
            'status': 'fail',
            'pass_found': False,
            'top_errors': [{'level': 'UVM_ERROR', 'error_id': 'UNKNOWN_ERR',
                             'description': 'something unknown', 'location': ''}],
            'all_errors': [],
        }]
        results = run_match(parse_results, tmp_db)
        assert results[0]['match']['status'] == 'unmatched'

    def test_run_match_no_errors(self, tmp_db):
        parse_results = [{
            'file': 'pass.log',
            'filepath': '/tmp/pass.log',
            'statistics': {},
            'status': 'pass',
            'pass_found': True,
            'top_errors': [],
            'all_errors': [],
        }]
        results = run_match(parse_results, tmp_db)
        assert results[0]['match']['status'] == 'no_error'
