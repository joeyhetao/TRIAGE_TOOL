# -*- coding: utf-8 -*-
"""pytest 配置与共享 fixtures。"""
import os
import sys
import tempfile
import pytest

# 确保从 triage_tool 根目录导入（支持直接运行 pytest tests/）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def app():
    """返回配置好测试模式的 Flask 应用实例。"""
    import app as _app_module
    _app_module.app.config.update({
        'TESTING': True,
        'SECRET_KEY': 'test-secret-key',
    })
    return _app_module.app


@pytest.fixture
def client(app):
    """返回 Flask 测试客户端。"""
    with app.test_client() as c:
        yield c


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时 Excel 知识库文件，测试后自动清理。"""
    from core.db_manager import ensure_db
    db_path = str(tmp_path / 'test_db.xlsx')
    ensure_db(db_path)
    return db_path


@pytest.fixture
def sample_log(tmp_path):
    """写入一个包含 UVM 错误的简单日志文件，返回路径。"""
    content = (
        "UVM_ERROR /tb/dut.sv(42) @ 100ns: uvm_test_top.env [MEM_ERR] memory read mismatch\n"
        "  expected=0xDEAD got=0xBEEF\n"
        "UVM_FATAL /tb/dut.sv(99) @ 200ns: uvm_test_top [TIMEOUT] simulation timeout\n"
        "UVM_WARNING /tb/dut.sv(10) @ 50ns: uvm_test_top [WARN1] minor warning\n"
        "UVM_ERROR /tb/dut.sv(55) @ 300ns: uvm_test_top.env [BUS_ERR] bus error\n"
    )
    log_file = tmp_path / 'test.log'
    log_file.write_text(content, encoding='utf-8')
    return str(log_file)


@pytest.fixture
def passing_log(tmp_path):
    """写入一个通过的日志文件（无错误 + 含 PASS 标记）。"""
    content = (
        "Simulation started\n"
        "UVM_WARNING /tb/dut.sv(10) @ 50ns: uvm_test_top [WARN1] minor warning\n"
        "JVP TEST PASSED\n"
    )
    log_file = tmp_path / 'pass_test.log'
    log_file.write_text(content, encoding='utf-8')
    return str(log_file)
