# -*- coding: utf-8 -*-
from datetime import datetime
from flask import Blueprint, send_file

import state
from core.reporter import generate_excel, generate_html

export_bp = Blueprint('export', __name__)


@export_bp.route('/export/excel')
def export_excel():
    sid = state._sid()
    results, _ = state._get_results(sid)
    if not results:
        return '无分析结果', 400
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = 'triage_report_{}.xlsx'.format(ts)
    out_path = str(state.REPORT_DIR / fname)
    generate_excel(results, out_path)
    return send_file(out_path, as_attachment=True, download_name=fname)


@export_bp.route('/export/html')
def export_html():
    sid = state._sid()
    results, _ = state._get_results(sid)
    if not results:
        return '无分析结果', 400
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = 'triage_report_{}.html'.format(ts)
    out_path = str(state.REPORT_DIR / fname)
    generate_html(results, out_path)
    return send_file(out_path, as_attachment=True, download_name=fname)
