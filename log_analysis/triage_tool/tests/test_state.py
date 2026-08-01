# -*- coding: utf-8 -*-
"""Tests for state.py cross-log first-error deduplication."""
import state


def _result(file_name, description, location='/share/proj/rpe_pkt_base.sv(1130)',
            error_id='rpe_pkt_transaction', level='UVM_ERROR'):
    return {
        'file': file_name,
        'all_errors': [{
            'level': level,
            'error_id': error_id,
            'description': description,
            'location': location,
        }],
    }


class TestUniqueErrorsByLevel:
    def test_same_location_and_template_ignore_dynamic_numbers(self):
        results = [
            _result(
                'loop_1436707230.log',
                "unpack: calc pkt_len('d70)>packed_data.size('d70),"
                "header_len('d74),payload_len('d4294967288),pad_len('d0),ip_len('d56):",
            ),
            _result(
                'loop_1315708347.log',
                "unpack: calc pkt_len('d78)>packed_data.size('d78),"
                "header_len('d82),payload_len('d4294967288),pad_len('d0),ip_len('d64):",
            ),
            _result(
                'loop_206082955.log',
                "unpack: calc pkt_len('d122)>packed_data.size('d122),"
                "header_len('d126),payload_len('d4294967288),pad_len('d0),ip_len('d108):",
            ),
        ]

        grouped = state._unique_errors_by_level(results)

        assert len(grouped['UVM_ERROR']) == 1
        entry = grouped['UVM_ERROR'][0]
        assert entry['error_id'] == 'rpe_pkt_transaction'
        assert entry['location'] == '/share/proj/rpe_pkt_base.sv(1130)'
        assert set(entry['files']) == {
            'loop_1436707230.log',
            'loop_1315708347.log',
            'loop_206082955.log',
        }

    def test_same_location_and_template_ignore_bare_hex_values(self):
        results = [
            _result('function_decimal.log', 'ceq of function=187 is full !!!',
                    location='/share/proj/rpe_rm.sv(5645)', error_id='rpe_rm'),
            _result('function_hex.log', 'ceq of function=7fc is full !!!',
                    location='/share/proj/rpe_rm.sv(5645)', error_id='rpe_rm'),
        ]

        grouped = state._unique_errors_by_level(results)

        assert len(grouped['UVM_ERROR']) == 1
        assert set(grouped['UVM_ERROR'][0]['files']) == {
            'function_decimal.log',
            'function_hex.log',
        }

    def test_same_error_id_but_different_location_splits_groups(self):
        results = [
            _result('a.log', "unpack: calc pkt_len('d70)>packed_data.size('d70):",
                    location='/tb/rpe_pkt_base.sv(1130)'),
            _result('b.log', "unpack: calc pkt_len('d78)>packed_data.size('d78):",
                    location='/tb/rpe_pkt_base.sv(1131)'),
        ]

        grouped = state._unique_errors_by_level(results)

        assert len(grouped['UVM_ERROR']) == 2
        assert {e['location'] for e in grouped['UVM_ERROR']} == {
            '/tb/rpe_pkt_base.sv(1130)',
            '/tb/rpe_pkt_base.sv(1131)',
        }

    def test_same_error_id_and_location_ignore_description_differences(self):
        results = [
            _result('a.log', "unpack: calc pkt_len('d70)>packed_data.size('d70):"),
            _result('b.log', "unpack: drop pkt_len('d78)>packed_data.size('d78):"),
        ]

        grouped = state._unique_errors_by_level(results)

        assert len(grouped['UVM_ERROR']) == 1
        assert set(grouped['UVM_ERROR'][0]['files']) == {'a.log', 'b.log'}

    def test_same_error_id_and_location_merge_qp_runtime_dump_values(self):
        results = [
            _result(
                'qp_1.log',
                "|END_OF_QP_CHECK: HCA_LCL_QP<pipe0><HOST0><FUNC'h1e><CHNL'h2><IDX'h53>"
                ":{ROCE_UD,CONNECT_LPK_1QP} qpc.sq_tx_pi('ha)!=qpc.sq_tx_cur_ci('h1), "
                "sq may not complete over!",
                location='/share/proj/rpe_qp.sv(3495)', error_id='rpe_qp',
            ),
            _result(
                'qp_2.log',
                "|END_OF_QP_CHECK: HCA_LCL_QP<pipe0><HOST0><FUNC'h1e><CHNL'h2><IDX'h53>"
                ":{ROCE_UD,CONNECT_LPK_2QP} qpc.sq_tx_pi('h9)!=qpc.sq_tx_cur_ci('h0), "
                "sq may not complete over!",
                location='/share/proj/rpe_qp.sv(3495)', error_id='rpe_qp',
            ),
            _result(
                'qp_3.log',
                "|END_OF_QP_CHECK: HCA_LCL_QP<pipe1><HOST0><FUNC'h789><CHNL'h4e><IDX'h171a3>"
                ":{ROCE_UD,CONNECT_LPK_2QP} qpc.sq_tx_pi('ha)!=qpc.sq_tx_cur_ci('h1), "
                "sq may not complete over!",
                location='/share/proj/rpe_qp.sv(3495)', error_id='rpe_qp',
            ),
        ]

        grouped = state._unique_errors_by_level(results)

        assert len(grouped['UVM_ERROR']) == 1
        assert set(grouped['UVM_ERROR'][0]['files']) == {'qp_1.log', 'qp_2.log', 'qp_3.log'}

    def test_no_location_old_format_uses_normalized_description(self):
        results = [
            _result('a.log', 'timeout on cycle 91 at 14248.69ns',
                    location='', error_id='T_BUS_ERR', level='ERROR'),
            _result('b.log', 'timeout on cycle 128 at 19249.20ns',
                    location='', error_id='T_BUS_ERR', level='ERROR'),
        ]

        grouped = state._unique_errors_by_level(results)

        assert len(grouped['ERROR']) == 1
        assert set(grouped['ERROR'][0]['files']) == {'a.log', 'b.log'}

    def test_signature_normalizes_sv_hex_and_decimal_literals(self):
        left = "value 32'hff got 'd70, payload 4294967288 at 100ns"
        right = "value 16'hab got 'd122, payload 7 at 200ns"

        assert state._dedup_description_signature(left) == state._dedup_description_signature(right)

    def test_signature_normalizes_bare_hex_function_values(self):
        left = 'ceq of function=187 is full !!!'
        right = 'ceq of function=7fc is full !!!'

        assert state._dedup_description_signature(left) == state._dedup_description_signature(right)

    def test_decimal_signature_does_not_partially_replace_words(self):
        signature = state._dedup_description_signature('axi_write 10th cqe and id=7fc')

        assert '10th' in signature
        assert '7fc' not in signature
        assert 'id=<num>' in signature
