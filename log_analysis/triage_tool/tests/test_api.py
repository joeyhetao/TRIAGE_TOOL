# -*- coding: utf-8 -*-
"""Flask API 端点集成测试（使用 Flask test client）。"""
import json
import pytest
import state


class TestIndexRoute:
    def test_get_index(self, client):
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'triage' in resp.data.lower() or b'log' in resp.data.lower()


class TestAnalyzeRoute:
    def test_analyze_no_file_returns_400(self, client):
        resp = client.post('/analyze', data={})
        assert resp.status_code == 400

    def test_analyze_upload_single_file(self, client, sample_log):
        with open(sample_log, 'rb') as f:
            resp = client.post('/analyze', data={
                'logs': (f, 'test.log'),
            }, content_type='multipart/form-data')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'job_id' in data

    def test_analyze_path_mode_empty_paths(self, client):
        resp = client.post('/analyze', data={
            'path_mode': '1',
            'log_paths': '',
        })
        assert resp.status_code == 400


class TestProgressStatusRoute:
    def test_nonexistent_job(self, client):
        resp = client.get('/progress_status/nonexistent-job-id')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['phase'] == 'error'


class TestResultRoute:
    def test_result_no_session(self, client):
        resp = client.get('/result')
        assert resp.status_code == 200  # 无结果时仍返回200，显示空列表

    def _seed_results(self, client, results, sid='dedup-route-test'):
        with client.session_transaction() as sess:
            sess['sid'] = sid
        state._set_results(sid, results, state.DB_DEFAULT)

    def test_unique_error_count_and_list_split_by_source_location(self, client):
        results = [
            {
                'file': 'case_a.log',
                'all_errors': [{
                    'level': 'UVM_ERROR',
                    'error_id': 'FIRST_ERR',
                    'description': 'first failure',
                    'location': '/tb/a.sv(1)',
                }],
            },
            {
                'file': 'case_b.log',
                'all_errors': [{
                    'level': 'UVM_ERROR',
                    'error_id': 'FIRST_ERR',
                    'description': 'first failure',
                    'location': '/tb/b.sv(2)',
                }],
            },
            {
                'file': 'case_c.log',
                'all_errors': [{
                    'level': 'UVM_ERROR',
                    'error_id': 'SECOND_ERR',
                    'description': 'second unique failure',
                    'location': '/tb/c.sv(3)',
                }],
            },
        ]
        self._seed_results(client, results)

        assert state._unique_error_counts(results)['UVM_ERROR'] == 3
        resp = client.get('/errors?level=UVM_ERROR')

        assert resp.status_code == 200
        assert '\u5171 3 \u6761\u552f\u4e00\u9519\u8bef'.encode('utf-8') in resp.data
        assert b'FIRST_ERR' in resp.data
        assert b'SECOND_ERR' in resp.data
        assert b'case_a.log' in resp.data
        assert b'case_b.log' in resp.data
        assert '\u51fa\u73b0\u5728 <b>1</b> \u4e2a\u6587\u4ef6'.encode('utf-8') in resp.data

    def test_same_error_id_with_different_first_error_descriptions_split(self, client):
        results = [
            {
                'file': 'compare_fail.log',
                'all_errors': [{
                    'level': 'UVM_ERROR',
                    'error_id': 'rpe_checker',
                    'description': 'COMPARE_FAIL sth aeoq, AEQ polling mode mismatch',
                    'location': '/tb/rpe_checker.sv(6083)',
                }],
            },
            {
                'file': 'axi_write.log',
                'all_errors': [{
                    'level': 'UVM_ERROR',
                    'error_id': 'rpe_checker',
                    'description': 'axi_write 10th cqe_of_cq, but rm not gen this cqe',
                    'location': '/tb/rpe_checker.sv(5478)',
                }],
            },
        ]
        self._seed_results(client, results, sid='dedup-same-id-different-desc')

        grouped = state._unique_errors_by_level(results)['UVM_ERROR']
        assert state._unique_error_counts(results)['UVM_ERROR'] == 2
        assert len(grouped) == 2
        assert grouped[0]['files'] == ['compare_fail.log']
        assert grouped[1]['files'] == ['axi_write.log']

        resp = client.get('/errors?level=UVM_ERROR')

        assert resp.status_code == 200
        assert b'COMPARE_FAIL' in resp.data
        assert b'axi_write' in resp.data
        assert b'compare_fail.log' in resp.data
        assert b'axi_write.log' in resp.data

    def test_unique_error_level_is_normalized(self, client):
        results = [{
            'file': 'case_norm.log',
            'all_errors': [{
                'level': ' uvm_error ',
                'error_id': 'NORM_ERR',
                'description': 'normalized level failure',
                'location': '/tb/norm.sv(4)',
            }],
        }]
        self._seed_results(client, results, sid='dedup-normalized-test')

        assert state._unique_error_counts(results)['UVM_ERROR'] == 1
        resp = client.get('/errors?level=%20uvm_error%20')

        assert resp.status_code == 200
        assert '共 1 条唯一错误'.encode('utf-8') in resp.data
        assert b'NORM_ERR' in resp.data

    def test_errors_empty_session_reports_expired_result(self, client):
        resp = client.get('/errors?level=UVM_ERROR')

        assert resp.status_code == 200
        assert '当前分析结果缓存已过期或程序已重启，请重新分析'.encode('utf-8') in resp.data


class TestKBRoutes:
    def _db_path(self, tmp_db):
        return tmp_db

    def test_kb_add_missing_reason(self, client, tmp_db):
        resp = client.post('/kb/add', json={
            'db_path': tmp_db,
            '错误类型': 'UVM_ERROR',
            '错误ID': 'TEST_001',
            '报错原因': '',
        })
        assert resp.status_code == 400

    def test_kb_add_success(self, client, tmp_db):
        resp = client.post('/kb/add', json={
            'db_path': tmp_db,
            '错误类型': 'UVM_ERROR',
            '错误ID': 'ADD_001',
            '报错原因': '测试原因',
            '关键描述关键词': 'add,test',
            '解决方案': '测试方案',
            '所属模块': 'TEST',
            '根因分类': 'DUT Bug',
            '关联用例': '',
            '录入人': 'tester',
        })
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get('success') is True

    def test_kb_query(self, client, tmp_db):
        # 先添加一条
        client.post('/kb/add', json={
            'db_path': tmp_db,
            '错误类型': 'UVM_ERROR',
            '错误ID': 'QRY_001',
            '报错原因': '查询测试原因',
            '关键描述关键词': '',
            '解决方案': '',
            '所属模块': '',
            '根因分类': '',
            '关联用例': '',
            '录入人': '',
        })
        resp = client.post('/query', json={
            'db_path': tmp_db,
            'error_id': 'QRY_001',
        })
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data.get('entries', [])) >= 1

    def test_kb_delete(self, client, tmp_db):
        from core.db_manager import append_entry, load_db
        append_entry(tmp_db, {
            '错误类型': 'UVM_ERROR', '错误ID': 'DEL_001',
            '关键描述关键词': '', '报错原因': '删除测试',
            '所属模块': '', '根因分类': '', '解决方案': '',
            '关联用例': '', '录入人': 'tester',
        })
        entries = load_db(tmp_db)
        row_idx = entries[0]['_row_idx']
        resp = client.post('/kb/delete', json={
            'db_path': tmp_db,
            'row_idx': row_idx,
        })
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get('success') is True


class TestConfigRoutes:
    def test_get_extra_patterns(self, client):
        resp = client.get('/extra_patterns')
        assert resp.status_code == 200

    def test_get_pass_patterns(self, client):
        resp = client.get('/pass_patterns')
        assert resp.status_code == 200


class TestExportRoutes:
    def test_export_excel_no_results(self, client):
        resp = client.get('/export/excel')
        # 无结果时应返回错误或空文件，不应崩溃
        assert resp.status_code in (200, 400, 404)

    def test_export_html_no_results(self, client):
        resp = client.get('/export/html')
        assert resp.status_code in (200, 400, 404)
