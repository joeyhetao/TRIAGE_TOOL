# -*- coding: utf-8 -*-
"""core/log_parser.py 单元测试。"""
import pytest
from core.log_parser import parse_log, parse_logs, _error_result


class TestParseLog:
    def test_uvm_error_extracted(self, sample_log):
        result = parse_log(sample_log)
        assert result['statistics']['UVM_ERROR'] == 2
        assert result['statistics']['UVM_FATAL'] == 1
        assert result['statistics']['UVM_WARNING'] == 1
        assert result['status'] == 'fail'

    def test_top_errors_exclude_warnings(self, sample_log):
        result = parse_log(sample_log)
        levels = [e['level'] for e in result['top_errors']]
        assert 'UVM_WARNING' not in levels

    def test_top_n_limit(self, tmp_path):
        # 生成超过 TOP_N 条的错误
        lines = [
            f"UVM_ERROR /tb/dut.sv({i}) @ {i}ns: uvm_test_top [ERR{i}] error {i}\n"
            for i in range(10)
        ]
        log_file = tmp_path / 'many_errors.log'
        log_file.write_text(''.join(lines), encoding='utf-8')
        result = parse_log(str(log_file))
        assert len(result['top_errors']) <= 5
        assert result['statistics']['UVM_ERROR'] == 10  # 全文统计仍为10

    def test_pass_with_pass_pattern(self, passing_log):
        result = parse_log(passing_log, pass_patterns=['JVP TEST PASSED'])
        assert result['status'] == 'pass'
        assert result['pass_found'] is True

    def test_fail_without_pass_pattern_in_file(self, tmp_path):
        log_file = tmp_path / 'no_pass.log'
        log_file.write_text('Simulation done\n', encoding='utf-8')
        result = parse_log(str(log_file), pass_patterns=['JVP TEST PASSED'])
        assert result['status'] == 'fail'
        assert result['pass_found'] is False

    def test_pass_without_pass_patterns_config(self, passing_log):
        # 未配置 pass_patterns 时退化为旧逻辑：无错误即 pass
        result = parse_log(passing_log, pass_patterns=[])
        assert result['status'] == 'pass'

    def test_continuation_lines_merged(self, tmp_path):
        content = (
            "UVM_ERROR /tb/dut.sv(42) @ 100ns: uvm_test_top [MEM] main description\n"
            "  continuation line 1\n"
            "  continuation line 2\n"
        )
        log_file = tmp_path / 'cont.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        desc = result['top_errors'][0]['description']
        assert 'continuation line 1' in desc
        assert 'continuation line 2' in desc

    def test_all_errors_deduplication(self, tmp_path):
        content = (
            "UVM_ERROR /tb/dut.sv(1) @ 1ns: uvm_test_top [DUP_ERR] first occurrence\n"
            "UVM_ERROR /tb/dut.sv(2) @ 2ns: uvm_test_top [DUP_ERR] second occurrence\n"
        )
        log_file = tmp_path / 'dup.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        dup_errors = [e for e in result['all_errors'] if e['error_id'] == 'DUP_ERR']
        assert len(dup_errors) == 1  # 去重后只有1条
        assert result['statistics']['UVM_ERROR'] == 2  # 统计仍为2

    def test_extra_keywords(self, tmp_path):
        content = "ERROR: custom error message\nFATAL: fatal message\n"
        log_file = tmp_path / 'extra.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['ERROR', 'FATAL'])
        assert result['statistics'].get('ERROR', 0) == 1
        assert result['statistics'].get('FATAL', 0) == 1
        assert result['status'] == 'fail'

    def test_nonexistent_file_returns_error_result(self):
        # parse_log 不存在的文件应抛出异常（由 parse_logs 的 try/except 捕获）
        with pytest.raises(Exception):
            parse_log('/nonexistent/path/file.log')

    def test_error_result_structure(self):
        r = _error_result('/tmp/foo.log', 'Permission denied')
        assert r['status'] == 'fail'
        assert r['file'] == 'foo.log'
        assert 'Permission denied' in r['error']
        assert r['top_errors'] == []
        assert r['all_errors'] == []


class TestParseLogs:
    def test_single_file(self, sample_log):
        results = parse_logs([sample_log])
        assert len(results) == 1
        assert results[0]['statistics']['UVM_ERROR'] == 2

    def test_multiple_files(self, sample_log, passing_log):
        results = parse_logs([sample_log, passing_log],
                             pass_patterns=['JVP TEST PASSED'])
        assert len(results) == 2
        # 顺序与输入一致
        assert results[0]['status'] == 'fail'
        assert results[1]['status'] == 'pass'

    def test_one_bad_file_does_not_kill_batch(self, sample_log, tmp_path):
        bad_path = str(tmp_path / 'nonexistent.log')
        results = parse_logs([sample_log, bad_path])
        assert len(results) == 2
        # 好文件正常解析
        assert results[0]['statistics']['UVM_ERROR'] == 2
        # 坏文件返回 error 占位结果
        assert results[1]['status'] == 'fail'
        assert 'error' in results[1]

    def test_progress_callback_called(self, sample_log):
        calls = []
        def cb(filename, result, done, total):
            calls.append((filename, done, total))
        parse_logs([sample_log], progress_cb=cb)
        assert len(calls) == 1
        assert calls[0][1] == 1  # done=1
        assert calls[0][2] == 1  # total=1
