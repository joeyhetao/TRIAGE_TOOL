# -*- coding: utf-8 -*-
"""core/db_manager.py 单元测试。"""
import pytest
from core.db_manager import (
    ensure_db, load_db, append_entry, update_entry,
    delete_entry, find_duplicates, HEADERS,
)


def _make_entry(**kwargs):
    base = {
        '错误类型': 'UVM_ERROR',
        '错误ID': 'TEST_001',
        '关键描述关键词': 'test,keyword',
        '报错原因': '测试原因',
        '所属模块': 'TEST',
        '根因分类': 'DUT Bug',
        '解决方案': '测试解决方案',
        '关联用例': '',
        '录入人': 'tester',
    }
    base.update(kwargs)
    return base


class TestEnsureDb:
    def test_creates_file_with_headers(self, tmp_path):
        db_path = str(tmp_path / 'new.xlsx')
        ensure_db(db_path)
        import openpyxl
        wb = openpyxl.load_workbook(db_path)
        ws = wb.active
        headers = [cell.value for cell in ws[1]]
        assert headers == HEADERS

    def test_idempotent(self, tmp_db):
        # 已存在时不抛异常、不覆盖
        append_entry(tmp_db, _make_entry())
        ensure_db(tmp_db)
        entries = load_db(tmp_db)
        assert len(entries) == 1


class TestLoadDb:
    def test_empty_db(self, tmp_db):
        assert load_db(tmp_db) == []

    def test_loaded_entry_has_row_idx(self, tmp_db):
        append_entry(tmp_db, _make_entry())
        entries = load_db(tmp_db)
        assert entries[0]['_row_idx'] == 2

    def test_multiple_entries(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='E001'))
        append_entry(tmp_db, _make_entry(错误ID='E002'))
        entries = load_db(tmp_db)
        assert len(entries) == 2
        assert entries[0]['错误ID'] == 'E001'
        assert entries[1]['错误ID'] == 'E002'


class TestAppendEntry:
    def test_appended_entry_readable(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='APPEND_01'))
        entries = load_db(tmp_db)
        assert entries[0]['错误ID'] == 'APPEND_01'

    def test_auto_fills_date(self, tmp_db):
        append_entry(tmp_db, _make_entry())
        entries = load_db(tmp_db)
        assert entries[0]['录入日期'] != ''

    def test_append_multiple(self, tmp_db):
        for i in range(3):
            append_entry(tmp_db, _make_entry(错误ID=f'E{i:03d}'))
        assert len(load_db(tmp_db)) == 3


class TestUpdateEntry:
    def test_update_field(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='UPD_01'))
        entries = load_db(tmp_db)
        row_idx = entries[0]['_row_idx']
        update_entry(tmp_db, row_idx, {'报错原因': '更新后的原因'})
        updated = load_db(tmp_db)
        assert updated[0]['报错原因'] == '更新后的原因'

    def test_update_nonexistent_field_ignored(self, tmp_db):
        append_entry(tmp_db, _make_entry())
        entries = load_db(tmp_db)
        row_idx = entries[0]['_row_idx']
        # 更新不存在的字段不应抛异常
        update_entry(tmp_db, row_idx, {'_nonexistent': 'value'})

    def test_update_corrupt_file_raises_value_error(self, tmp_path):
        db_path = str(tmp_path / 'corrupt.xlsx')
        # 写入非 Excel 内容
        with open(db_path, 'w') as f:
            f.write('this is not a valid xlsx file')
        with pytest.raises(ValueError, match='无法读取知识库文件'):
            update_entry(db_path, 2, {'报错原因': '测试'})


class TestDeleteEntry:
    def test_delete_reduces_count(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='DEL_01'))
        append_entry(tmp_db, _make_entry(错误ID='DEL_02'))
        entries = load_db(tmp_db)
        assert len(entries) == 2
        delete_entry(tmp_db, entries[0]['_row_idx'])
        remaining = load_db(tmp_db)
        assert len(remaining) == 1
        assert remaining[0]['错误ID'] == 'DEL_02'

    def test_delete_corrupt_file_raises_value_error(self, tmp_path):
        db_path = str(tmp_path / 'corrupt.xlsx')
        with open(db_path, 'w') as f:
            f.write('not xlsx')
        with pytest.raises(ValueError, match='无法读取知识库文件'):
            delete_entry(db_path, 2)


class TestFindDuplicates:
    def test_exact_id_duplicate(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='DUP_001'))
        entry = _make_entry(错误ID='DUP_001')
        conflicts = find_duplicates(tmp_db, entry)
        assert len(conflicts) == 1

    def test_no_duplicate(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='UNIQ_001'))
        entry = _make_entry(错误ID='UNIQ_002', 关键描述关键词='other,kw',
                             报错原因='不同原因', 解决方案='不同方案')
        conflicts = find_duplicates(tmp_db, entry)
        assert len(conflicts) == 0

    def test_exclude_row_idx(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='SELF_001'))
        entries = load_db(tmp_db)
        row_idx = entries[0]['_row_idx']
        # 排除自身行：编辑时不应与自己冲突
        conflicts = find_duplicates(tmp_db, _make_entry(错误ID='SELF_001'),
                                    exclude_row_idx=row_idx)
        assert len(conflicts) == 0

    def test_keyword_duplicate(self, tmp_db):
        append_entry(tmp_db, _make_entry(错误ID='', 关键描述关键词='kw_a,kw_b'))
        entry = _make_entry(错误ID='', 关键描述关键词='kw_a,kw_b',
                             报错原因='不同原因', 解决方案='不同方案')
        conflicts = find_duplicates(tmp_db, entry)
        assert len(conflicts) == 1
