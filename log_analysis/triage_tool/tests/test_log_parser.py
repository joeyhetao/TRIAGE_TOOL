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

    # ─────────────────────────────────────────────────────────────────
    # UVM 行格式完备性回归（BUG-029）
    # 每条覆盖一个独立的合法 UVM 报错变体，确保正则不漏检任何形式
    # ─────────────────────────────────────────────────────────────────

    def _write_and_parse(self, tmp_path, content, name='variant.log'):
        log_file = tmp_path / name
        log_file.write_text(content, encoding='utf-8')
        return parse_log(str(log_file))

    def test_variant_missing_file_line(self, tmp_path):
        # 变体 2：sequence/vsequencer 报错，无 file(line) 前缀
        # 来源：用户内网真实样本
        line = ("UVM_ERROR @ 82.00ns: uvm_test_top.env.vsqr@@nic_pb_apb_seq "
                "[uvm_test_top.env.vsqr.nic_pb_apb_seq] Response queue overflow, "
                "response was dropped\n")
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_ERROR'] == 1
        entry = result['top_errors'][0]
        assert entry['location'] == ''  # 无 file(line) → 空字符串
        assert entry['error_id'] == 'uvm_test_top.env.vsqr.nic_pb_apb_seq'
        assert entry['description'].startswith('Response queue overflow')
        assert entry['timestamp'] == '82.00ns'

    def test_variant_time_with_space_before_unit(self, tmp_path):
        # 变体 3a：time 单位前有空格（OpenTitan / 双时间精度仿真器常见）
        line = ("UVM_FATAL /dv/reg.sv(161) @ 0 ps: uvm_test_top "
                "[ral] Check failed\n")
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_FATAL'] == 1
        assert result['top_errors'][0]['timestamp'] == '0ps'  # 空格去掉

    def test_variant_time_microsecond_float(self, tmp_path):
        # 变体 3b：浮点时间 + us 单位（OpenTitan 真实样本）
        line = ("UVM_ERROR @ 6933.414503 us: uvm_test_top.env.vseq "
                "[uvm_test_top.env.vseq] Check failed\n")
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_ERROR'] == 1
        assert result['top_errors'][0]['timestamp'] == '6933.414503us'

    def test_variant_opentitan_file_after_time(self, tmp_path):
        # 变体 4：OpenTitan 自定义 server，(file:line) 放在 @ time 之后
        # 此时 (file:line) 整体被 reporter 槽吞下；level/id/msg 仍正确提取（不漏检）
        line = ("UVM_FATAL @ 0 ps: (dv_base_reg_block.sv:161) "
                "[ral] Check failed ((base_addr & mask) == 0)\n")
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_FATAL'] == 1
        entry = result['top_errors'][0]
        assert entry['error_id'] == 'ral'
        assert entry['description'].startswith('Check failed')

    def test_variant_verbosity_prefix(self, tmp_path):
        # 变体 5：开启 show_verbosity 后 severity 紧接 (LEVEL)
        line = ("UVM_ERROR(MEDIUM) /tb/dut.sv(99) @ 1000 ns: "
                "uvm_test_top.env.agt.drv@@seq_id "
                "[uvm_driver #(REQ,RSP)] Constraint solver failed\n")
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_ERROR'] == 1
        entry = result['top_errors'][0]
        assert entry['error_id'] == 'uvm_driver #(REQ,RSP)'
        assert 'dut.sv(99)' in entry['location']

    def test_variant_id_with_spaces(self, tmp_path):
        # 变体 6：id 字段含空格（OpenTitan / 自定义 server 常用）
        line = ("UVM_ERROR @ 3023421 ps: (otbn_model_if.sv:47) "
                "[ASSERT FAILED] NoModelErrs\n")
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_ERROR'] == 1
        assert result['top_errors'][0]['error_id'] == 'ASSERT FAILED'

    def test_variant_empty_id(self, tmp_path):
        # 变体 11：id 为空 []（罕见但规范合法）
        line = "UVM_ERROR /tb/x.sv(10) @ 100ns: uvm_test_top [] missing id\n"
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_ERROR'] == 1
        assert result['top_errors'][0]['error_id'] == ''
        assert result['top_errors'][0]['description'] == 'missing id'

    def test_variant_filename_with_macro_placeholder(self, tmp_path):
        # 变体 10：filename 含未展开宏占位（riscv-dv 真实样本）
        line = ("UVM_FATAL $$STRING$$/src/riscv-dv/src/isa/riscv_instr.sv(418) "
                "@ 0: reporter [riscv_VSETVL_instr] Unsupported format VSET_FORMAT\n")
        result = self._write_and_parse(tmp_path, line)
        assert result['statistics']['UVM_FATAL'] == 1
        entry = result['top_errors'][0]
        assert entry['error_id'] == 'riscv_VSETVL_instr'
        assert 'riscv_instr.sv(418)' in entry['location']
        assert entry['timestamp'] == '0'  # 无单位

    def test_uvm_id_with_parametrized_type(self, tmp_path):
        # 参数化 UVM class 名（含空格、#、括号、逗号）+ 组件路径含数组索引 [0]
        line = (
            "UVM_ERROR /share/project/greenland/dev/yulin.chen/dv_grl_yulin/"
            "dv/jvp/utils/com_utils/r1p0/./src/com_driver.sv(283) "
            "@ 19249.00ns: uvm_test_top.env.m_com_pipe_pb_wr_agt[0].drv "
            "[uvm_driver #(REQ,RSP)] wait tr_cycle_num('d91) < "
            "tr_queue_num_max('d91) timeout.from:14248.69ns to:19249.20ns\n"
        )
        log_file = tmp_path / 'parametrized_id.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file))
        assert result['statistics']['UVM_ERROR'] == 1
        assert len(result['top_errors']) == 1
        entry = result['top_errors'][0]
        assert entry['error_id'] == 'uvm_driver #(REQ,RSP)'
        assert entry['description'].startswith('wait tr_cycle_num')
        assert 'com_driver.sv(283)' in entry['location']

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
