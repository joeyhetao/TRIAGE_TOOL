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

    def test_sva_error_after_pass_marker_still_fails(self, tmp_path):
        content = (
            "JVP TEST PASSED. Warning number is 143\n"
            "UVM_WARNING : 143\n"
            "$finish called from file /tools/uvm_root.svh, line 527.\n"
            "/proj/dv/foo.sv, 122: top_tb.u_DUT.u_checker: started at 10fs failed at 10fs\n"
            "SVA_ERROR: instance top_tb.u_DUT.u_checker final state is unexpected!!!\n"
        )
        log_file = tmp_path / 'late_sva_error.log'
        log_file.write_text(content, encoding='utf-8')

        result = parse_log(str(log_file), pass_patterns=['JVP TEST PASSED'])

        assert result['pass_found'] is True
        assert result['status'] == 'fail'
        assert result['statistics']['SVA_ERROR'] == 1
        assert result['top_errors'][0]['level'] == 'SVA_ERROR'
        assert 'final state is unexpected' in result['top_errors'][0]['description']

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

    def test_all_errors_keeps_only_first_non_warning_error(self, tmp_path):
        content = (
            "UVM_WARNING /tb/dut.sv(0) @ 0ns: uvm_test_top [WARN_ONLY] warning ignored for dedup\n"
            "UVM_ERROR /tb/dut.sv(1) @ 1ns: uvm_test_top [FIRST_ERR] first error\n"
            "UVM_ERROR /tb/dut.sv(2) @ 2ns: uvm_test_top [SECOND_ERR] second error\n"
        )
        log_file = tmp_path / 'first_error.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))

        assert result['statistics']['UVM_WARNING'] == 1
        assert result['statistics']['UVM_ERROR'] == 2
        assert [e['error_id'] for e in result['top_errors']] == ['FIRST_ERR', 'SECOND_ERR']
        assert [e['error_id'] for e in result['all_errors']] == ['FIRST_ERR']

    def test_all_errors_empty_for_warning_only_log(self, tmp_path):
        content = "UVM_WARNING /tb/dut.sv(0) @ 0ns: uvm_test_top [WARN_ONLY] warning only\n"
        log_file = tmp_path / 'warning_only.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))

        assert result['statistics']['UVM_WARNING'] == 1
        assert result['top_errors'] == []
        assert result['all_errors'] == []

    def test_extra_keywords(self, tmp_path):
        content = "ERROR: custom error message\nFATAL: fatal message\n"
        log_file = tmp_path / 'extra.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['ERROR', 'FATAL'])
        assert result['statistics'].get('ERROR', 0) == 1
        assert result['statistics'].get('FATAL', 0) == 1
        assert result['status'] == 'fail'

    # ─────────────────────────────────────────────────────────────────
    # _gen_pattern 完备性回归（BUG-030）
    # 覆盖 VCS 标准报错 / IP 内部报错 / RTL SVA 等真实 IC 验证场景，
    # 同时验证 word-boundary 防误报与 [ID] 抽取
    # ─────────────────────────────────────────────────────────────────

    def test_gen_vcs_format(self, tmp_path):
        # VCS 标准报错：Error-[ID] msg，连字符分隔，ID 含 -
        line = "Error-[CNST-CIF] Constraints inconsistency failure\n"
        log_file = tmp_path / 'vcs.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['ERROR'])
        assert result['statistics']['ERROR'] == 1
        entry = result['top_errors'][0]
        assert entry['level'] == 'ERROR'
        assert entry['error_id'] == 'CNST-CIF'  # 关键：ID 被抽取到 error_id
        assert entry['description'] == 'Constraints inconsistency failure'

    def test_gen_ip_format(self, tmp_path):
        # IP 内部报错：关键词后直接接 [ID]，无分隔符
        line = "IP_FATAL[T_BUS_ERR] timeout on cycle 91\n"
        log_file = tmp_path / 'ip.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['IP_FATAL'])
        assert result['statistics']['IP_FATAL'] == 1
        entry = result['top_errors'][0]
        assert entry['error_id'] == 'T_BUS_ERR'
        assert entry['description'] == 'timeout on cycle 91'

    def test_gen_sva_no_separator(self, tmp_path):
        # SVA 自定义：关键词后是空格，无 [ID]，纯描述
        line = "MY_SVA_ERR signal X stayed low for 100ns\n"
        log_file = tmp_path / 'sva.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['MY_SVA_ERR'])
        assert result['statistics']['MY_SVA_ERR'] == 1
        entry = result['top_errors'][0]
        assert entry['error_id'] == ''  # 无 ID，走 KB Step2 关键词匹配
        assert entry['description'] == 'signal X stayed low for 100ns'

    def test_gen_word_boundary_blocks_partial_match(self, tmp_path):
        # word-boundary 防误报：Erroring / MY_ERR_VAR=x 不能命中
        content = (
            "Erroring something happened\n"
            "MY_ERR_VAR = some value\n"
            "REAL_ERROR: this one should hit\n"
        )
        log_file = tmp_path / 'boundary.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file),
                           extra_keywords=['ERROR', 'MY_ERR', 'REAL_ERROR'])
        # 仅 REAL_ERROR 行应被识别
        assert result['statistics'].get('REAL_ERROR', 0) == 1
        # Erroring 不命中 ERROR；MY_ERR_VAR 不命中 MY_ERR（VAR 是 word char，\b 阻断）
        assert result['statistics'].get('ERROR', 0) == 0
        assert result['statistics'].get('MY_ERR', 0) == 0

    def test_gen_classic_colon_still_works(self, tmp_path):
        # 兼容回归：旧 ERROR: msg 格式（无 [ID]）仍正确解析
        line = "ERROR: just a simple message\n"
        log_file = tmp_path / 'classic.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['ERROR'])
        assert result['statistics']['ERROR'] == 1
        entry = result['top_errors'][0]
        assert entry['error_id'] == ''
        assert entry['description'] == 'just a simple message'

    def test_gen_mixed_formats_in_one_log(self, tmp_path):
        # 同一个日志混合 VCS / IP / SVA / 旧格式，全部应被识别
        content = (
            "Error-[CNST-CIF] vcs format\n"
            "IP_FATAL[T_BUS] ip format\n"
            "MY_SVA assertion failed at 100ns\n"
            "ERROR: classic format\n"
        )
        log_file = tmp_path / 'mixed.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(
            str(log_file),
            extra_keywords=['ERROR', 'IP_FATAL', 'MY_SVA']
        )
        assert result['statistics']['ERROR'] == 2     # VCS + classic 都计入 ERROR
        assert result['statistics']['IP_FATAL'] == 1
        assert result['statistics']['MY_SVA'] == 1
        ids = {e['error_id'] for e in result['top_errors']}
        assert 'CNST-CIF' in ids
        assert 'T_BUS' in ids

    # ─────────────────────────────────────────────────────────────────
    # VCS 专用正则回归（BUG-031）—— 开箱即用，无需配置 EXTRA_PATTERNS
    # 真实样本来自 GitHub: chipyard#914, SpinalHDL#669, cocotb VCS issues
    # ─────────────────────────────────────────────────────────────────

    def test_vcs_zero_config_recognition(self, tmp_path):
        # 核心价值：用户不配 EXTRA_PATTERNS 也能识别 VCS 报错
        line = "Error-[CNST-CIF] Constraints inconsistency failure\n"
        log_file = tmp_path / 'vcs_zero_config.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file))  # 注意：没传 extra_keywords
        assert result['statistics']['ERROR'] == 1
        entry = result['top_errors'][0]
        assert entry['level'] == 'ERROR'
        assert entry['error_id'] == 'CNST-CIF'
        assert entry['description'] == 'Constraints inconsistency failure'

    def test_vcs_all_severities(self, tmp_path):
        # 五种 severity：Error/Warning/Fatal 计入；Note/Info 不计
        content = (
            "Error-[SFCOR] Source file cannot be opened\n"
            "Warning-[VPI-CT-NS] VPI function is not supported\n"
            "Fatal-[INTERR] Internal compiler error\n"
            "Note-[GENERIC] this is just a note\n"
            "Info-[VERSION] VCS version info\n"
        )
        log_file = tmp_path / 'vcs_severities.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        assert result['statistics']['ERROR'] == 1
        assert result['statistics']['WARNING'] == 1
        assert result['statistics']['FATAL'] == 1
        # Note/Info 不应进 statistics（IC 验证语境下非错误）
        assert 'NOTE' not in result['statistics']
        assert 'INFO' not in result['statistics']

    def test_vcs_id_charset_with_hyphens_and_underscores(self, tmp_path):
        # 真实 ID 样本：VPI-CT-NS（连字符）、DBGACC_REG（下划线）、DPI-UED、SE-LMHW
        content = (
            "Error-[VPI-CT-NS] hyphen separator in id\n"
            "Warning-[DBGACC_REG] underscore in id\n"
            "Error-[DPI-UED] mixed hyphen\n"
            "Error-[SE-LMHW] two-part hyphen\n"
        )
        log_file = tmp_path / 'vcs_ids.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        ids = {e['error_id'] for e in result['top_errors']}
        assert 'VPI-CT-NS' in ids
        assert 'DPI-UED' in ids
        assert 'SE-LMHW' in ids
        # WARNING 不进 top_errors，也不进入跨日志去重 all_errors
        assert result['statistics']['WARNING'] == 1
        assert all(e['level'] != 'WARNING' for e in result['all_errors'])

    def test_vcs_continuation_indented(self, tmp_path):
        # VCS 续行使用 2-space 缩进（chipyard#914 实证）
        content = (
            "Error-[DPI-UED] C++ Exception detected\n"
            "  Import DPI routine invoked at file\n"
            "  '/home/user/project/dut.sv'\n"
        )
        log_file = tmp_path / 'vcs_cont.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        desc = result['top_errors'][0]['description']
        assert desc.startswith('C++ Exception detected')
        assert 'Import DPI routine' in desc
        assert "'/home/user/project/dut.sv'" in desc

    def test_vcs_warning_not_in_top_errors(self, tmp_path):
        # Warning-[ID] 计入 statistics 但不进 top_errors（与 UVM_WARNING 一致）
        content = (
            "Warning-[VPI-CT-NS] some warning\n"
            "Error-[SFCOR] real error\n"
        )
        log_file = tmp_path / 'vcs_warn.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        assert result['statistics']['WARNING'] == 1
        assert result['statistics']['ERROR'] == 1
        levels = [e['level'] for e in result['top_errors']]
        assert 'WARNING' not in levels
        assert levels == ['ERROR']

    def test_vcs_id_routes_to_kb_step1(self, tmp_path):
        # 端到端集成：VCS 抽出的 error_id 进 KB Step1 精确命中
        from core.matcher import match_error
        line = "Error-[CNST-CIF] Constraints inconsistency failure\n"
        log_file = tmp_path / 'vcs_kb.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file))
        parsed_error = result['top_errors'][0]
        db_entries = [{
            '错误类型': 'ERROR',
            '错误ID': 'CNST-CIF',
            '关键描述关键词': '',
            '报错原因': 'over-constrained random',
            '解决方案': 'relax constraints',
            '录入日期': '2026-06-03',
            '稳定ID': 'vcs001',
        }]
        m = match_error(parsed_error, db_entries)
        assert m['status'] == 'matched'
        assert m['match_by'] == 'error_id'
        assert m['entry']['错误ID'] == 'CNST-CIF'

    def test_cross_isolation_vcs_does_not_eat_uvm(self, tmp_path):
        # 跨格式隔离 #1：UVM 行不能被 VCS 正则误命中
        # （UVM 优先级更高，UVM 行应走 UVM 路径，留 file/line/time 字段）
        content = (
            "UVM_ERROR /tb/dut.sv(42) @ 100ns: uvm_test_top.env [MEM_ERR] memory mismatch\n"
            "Error-[CNST-CIF] vcs error\n"
        )
        log_file = tmp_path / 'mixed.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        # UVM 行进 UVM_ERROR 统计、有 timestamp/location
        assert result['statistics']['UVM_ERROR'] == 1
        uvm_entry = next(e for e in result['top_errors']
                         if e['level'] == 'UVM_ERROR')
        assert uvm_entry['error_id'] == 'MEM_ERR'
        assert uvm_entry['timestamp'] == '100ns'
        assert 'dut.sv(42)' in uvm_entry['location']
        # VCS 行单独进 ERROR 统计
        assert result['statistics']['ERROR'] == 1
        vcs_entry = next(e for e in result['top_errors']
                         if e['level'] == 'ERROR')
        assert vcs_entry['error_id'] == 'CNST-CIF'

    def test_cross_isolation_extra_keywords_does_not_double_count(self, tmp_path):
        # 跨格式隔离 #2：即使用户配了 EXTRA_PATTERNS=['ERROR']，
        # VCS 行也只命中一次（VCS pattern 先于通用关键词 pattern，命中后 continue）
        line = "Error-[CNST-CIF] vcs error\n"
        log_file = tmp_path / 'no_double.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['ERROR'])
        # 只应有 1 个 ERROR 计数（VCS pattern 命中），不能是 2（VCS + 通用关键词重复）
        assert result['statistics']['ERROR'] == 1
        assert len(result['top_errors']) == 1
        assert result['top_errors'][0]['error_id'] == 'CNST-CIF'

    # ─────────────────────────────────────────────────────────────────
    # Xcelium 专用正则回归（BUG-032）—— 开箱即用，无需配置 EXTRA_PATTERNS
    # 真实样本来自 GitHub: cocotb#1363, openhwgroup/core-v-verif#11,
    # openhwgroup/cva6#2136, google/riscv-dv#305
    # ─────────────────────────────────────────────────────────────────

    def test_xcelium_zero_config_recognition(self, tmp_path):
        # 核心价值：用户不配 EXTRA_PATTERNS 也能识别 Xcelium 报错
        line = ("xmsim: *W,DSEM2009: This SystemVerilog design is simulated "
                "as per IEEE 1800-2009 SystemVerilog simulation semantics.\n")
        log_file = tmp_path / 'xcelium_zero.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file))  # 不传 extra_keywords
        assert result['statistics']['WARNING'] == 1
        # WARNING 不进 top_errors，也不进入跨日志去重 all_errors
        assert result['top_errors'] == []
        assert result['all_errors'] == []

    def test_xcelium_severity_SE_double_char(self, tmp_path):
        # 关键回归：*SE (Severe Error) 必须能被识别（不止单字符 severity）
        line = "xrun: *SE,JGUSOS: severe error from JG checker\n"
        log_file = tmp_path / 'xcelium_se.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file))
        # *SE 归并到 ERROR 统计
        assert result['statistics']['ERROR'] == 1
        entry = result['top_errors'][0]
        assert entry['level'] == 'ERROR'
        assert entry['error_id'] == 'JGUSOS'

    def test_xcelium_with_source_location(self, tmp_path):
        # source location: (file,line) 或 (file,line|col)，column 可选
        content = (
            "xmelab: *E,MBXNYI (/wrk/proj/tb_top.sv,87): missing connection\n"
            "xmelab: *E,DLCSMD (/wrk/proj/cfg.sv,12|45): checksum mismatch\n"
        )
        log_file = tmp_path / 'xcelium_loc.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        assert result['statistics']['ERROR'] == 2
        locations = {e['location'] for e in result['top_errors']}
        assert any('tb_top.sv(87)' in loc for loc in locations)
        assert any('cfg.sv(12)' in loc for loc in locations)

    def test_xcelium_all_tool_prefixes(self, tmp_path):
        # 全部支持的工具前缀都应能识别（注意：真实 Xcelium 输出 ID 后必有 ':'）
        content = (
            "xrun: *E,VLGERR: error from xrun\n"
            "xmsim: *E,RUNERR: error from xmsim\n"
            "xmelab: *E,ELABERR: error from xmelab\n"
            "xmvlog: *E,VLGFLT: error from xmvlog\n"
            "ncsim: *E,LEGACY: error from old NC-Verilog ncsim\n"
            "ncelab: *E,NCELAB: error from ncelab\n"
            "ncvlog: *E,NCVLG: error from ncvlog\n"
            "irun: *E,IRUNERR: error from Incisive irun\n"
        )
        log_file = tmp_path / 'xcelium_tools.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        assert result['statistics']['ERROR'] == 8

    def test_xcelium_pid_version_suffix(self, tmp_path):
        # 工具前缀可带 (PID) 或 (版本号) 后缀
        line = "xrun(64): *E,VLGERR: error with PID suffix\n"
        log_file = tmp_path / 'xcelium_pid.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file))
        assert result['statistics']['ERROR'] == 1
        assert result['top_errors'][0]['error_id'] == 'VLGERR'

    def test_xcelium_severity_filter_skips_note_info(self, tmp_path):
        # *N (Note) / *I (Info) / *D (Debug) 不计入错误统计
        content = (
            "xrun: *E,REALERR: a real error\n"
            "xmsim: *N,XYZNOTE: a note (should be ignored)\n"
            "xmsim: *I,VERINFO: version info (should be ignored)\n"
        )
        log_file = tmp_path / 'xcelium_filter.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        # 只有 *E 计入 ERROR
        assert result['statistics']['ERROR'] == 1
        # 不该出现 NOTE/INFO 字段
        assert 'NOTE' not in result['statistics']
        assert 'INFO' not in result['statistics']

    def test_xcelium_id_routes_to_kb_step1(self, tmp_path):
        # 端到端集成：Xcelium 抽出的 error_id 进 KB Step1 精确命中
        from core.matcher import match_error
        line = "xmelab: *E,MBXNYI (/wrk/proj/tb_top.sv,87): mailbox type error\n"
        log_file = tmp_path / 'xcelium_kb.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file))
        parsed_error = result['top_errors'][0]
        db_entries = [{
            '错误类型': 'ERROR',
            '错误ID': 'MBXNYI',
            '关键描述关键词': '',
            '报错原因': 'unpacked struct not supported as mailbox type',
            '解决方案': 'upgrade Xcelium to 19.09+',
            '录入日期': '2026-06-03',
            '稳定ID': 'xc001',
        }]
        m = match_error(parsed_error, db_entries)
        assert m['status'] == 'matched'
        assert m['match_by'] == 'error_id'
        assert m['entry']['错误ID'] == 'MBXNYI'

    def test_cross_isolation_three_simulators_in_one_log(self, tmp_path):
        # 跨格式隔离：UVM + VCS + Xcelium 同时存在，各走各的路径
        content = (
            "UVM_ERROR /tb/dut.sv(42) @ 100ns: uvm_test_top [UVM_ID] uvm msg\n"
            "Error-[VCS_ID] vcs error msg\n"
            "xmsim: *E,XCEL_ID: Xcelium error msg\n"
        )
        log_file = tmp_path / 'three_sims.log'
        log_file.write_text(content, encoding='utf-8')
        result = parse_log(str(log_file))
        # 各自独立计数
        assert result['statistics']['UVM_ERROR'] == 1
        assert result['statistics']['ERROR'] == 2  # VCS + Xcelium 合并到 ERROR
        # 三条 ID 都被正确抽取
        ids = {e['error_id'] for e in result['top_errors']}
        assert 'UVM_ID' in ids
        assert 'VCS_ID' in ids
        assert 'XCEL_ID' in ids

    def test_gen_extracted_id_routes_to_kb_step1(self, tmp_path):
        # 端到端集成：抽出的 error_id 应能在 matcher Step1 精确命中
        from core.matcher import match_error
        line = "Error-[CNST-CIF] Constraints inconsistency failure\n"
        log_file = tmp_path / 'kb_route.log'
        log_file.write_text(line, encoding='utf-8')
        result = parse_log(str(log_file), extra_keywords=['ERROR'])
        parsed_error = result['top_errors'][0]
        # 构造一条 KB 条目模拟用户已沉淀的修复
        db_entries = [{
            '错误类型': 'ERROR',
            '错误ID': 'CNST-CIF',
            '关键描述关键词': '',
            '报错原因': 'over-constrained random',
            '解决方案': 'relax constraints',
            '录入日期': '2026-06-03',
            '稳定ID': 'abc123',
        }]
        m = match_error(parsed_error, db_entries)
        assert m['status'] == 'matched'
        assert m['match_by'] == 'error_id'  # 关键：走 Step1 而非 Step2
        assert m['entry']['错误ID'] == 'CNST-CIF'

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
