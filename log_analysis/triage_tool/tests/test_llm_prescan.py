# -*- coding: utf-8 -*-
"""
P2 — AI 日志问答 锚点定位准确性测试
覆盖 Phase 1 (A/D/F) 改动的纯函数行为。

约定：
  - 用 tmp_path fixture 写合成 log，每个 fixture 50–500 行
  - 不依赖真 LLM 服务（只测纯函数）
"""
from blueprints.llm_bp import (
    _extract_query_keywords,
    _query_prefers_start,
    _query_prefers_end,
    _p3_prescan,
    _p3_prescan_blocks,
    _cluster_anchors,
    _allocate_blocks,
    _adaptive_max_lines,
    _validate_file_index,
    _MIN_LINES_FLOOR,
    _CN_STOPWORDS,
    _CN_SYNONYMS,
)


# ══════════════════════════════════════════════════════════════
# Phase 1.A — 关键词提取（中文 + 同义词）
# ══════════════════════════════════════════════════════════════

class TestExtractKeywordsA:
    def test_quoted_phrase_kept(self):
        kws = _extract_query_keywords('查找 "AXI write timeout" 的报错')
        assert 'AXI write timeout' in kws

    def test_allcaps_id_extracted(self):
        kws = _extract_query_keywords('UVM_FATAL ERR_001 怎么处理')
        assert 'UVM_FATAL' in kws
        assert 'ERR_001' in kws

    def test_underscore_token_extracted(self):
        kws = _extract_query_keywords('axi_driver 模块超时')
        assert 'axi_driver' in kws

    def test_chinese_chunk_extracted(self):
        kws = _extract_query_keywords('分析报错原因')
        # "报错" 应该作为子片段被提取（"分析"是停用词被滤掉）
        assert '报错' in kws

    def test_stopwords_filtered(self):
        kws = _extract_query_keywords('列出所有 UVM_FATAL')
        # "列出"、"所有" 是停用词，不应出现
        assert '列出' not in kws
        assert '所有' not in kws
        assert 'UVM_FATAL' in kws

    def test_synonym_expansion_baoCuo(self):
        kws = _extract_query_keywords('哪些报错最严重')
        # 同义词扩展：'报错' 应映射到 error/err/fatal/warning
        assert 'error' in kws
        assert 'fatal' in kws

    def test_synonym_expansion_chao_shi(self):
        kws = _extract_query_keywords('为什么超时')
        assert 'timeout' in kws

    def test_synonym_expansion_chu_shi_hua(self):
        kws = _extract_query_keywords('初始化阶段做了什么')
        assert 'init' in kws
        assert 'phase' in kws

    def test_pure_chinese_query_not_empty(self):
        # 关键回归：纯中文 query 必须有至少一个英文/中文 token 用于锚定
        kws = _extract_query_keywords('分析时钟复位失败')
        assert len(kws) > 0
        # 同义词链：时钟→clock, 复位→reset, 失败→fail
        assert 'clock' in kws or 'clk' in kws
        assert 'fail' in kws or 'failure' in kws

    def test_particle_prefix_filtered(self):
        # "的报"、"分析最" 等以单字虚词起头/起尾或停用词扩展的子片段应被过滤
        kws = _extract_query_keywords('的报错')
        # "的报错" 整体也以"的"起头，应过滤；只剩"报错"和同义词
        assert '的报' not in kws
        assert '的报错' not in kws


# ══════════════════════════════════════════════════════════════
# Phase 1.D — 意图词表 (PREFER_START / PREFER_END)
# ══════════════════════════════════════════════════════════════

class TestPreferStart:
    def test_qian_ge(self):
        assert _query_prefers_start('前 5 个错误')

    def test_zui_zao(self):
        assert _query_prefers_start('最早出现的报错')

    def test_chu_shi_hua(self):
        assert _query_prefers_start('初始化阶段做了什么')

    def test_kai_tou(self):
        assert _query_prefers_start('开头的几行')

    def test_first_few_en(self):
        assert _query_prefers_start('show first few lines')

    def test_at_the_beginning(self):
        assert _query_prefers_start('what happened at the beginning')

    def test_negative_no_start(self):
        assert not _query_prefers_start('中段的内容')


class TestPreferEnd:
    def test_zui_hou(self):
        assert _query_prefers_end('最后 5 个错误')

    def test_jie_shu_qian(self):
        assert _query_prefers_end('结束前发生了什么')

    def test_beng_kui_qian(self):
        assert _query_prefers_end('崩溃前最后一行')

    def test_mo_wei(self):
        assert _query_prefers_end('末尾的报错')

    def test_last_en(self):
        assert _query_prefers_end('show last 10 lines')

    def test_final_en(self):
        assert _query_prefers_end('what is the final error')

    def test_negative_no_end(self):
        assert not _query_prefers_end('开头的几行')

    def test_start_and_end_independent(self):
        # 同时含"最早"和"最后"——两个判断器各自独立返回 True
        q = '列出最早和最后的报错'
        assert _query_prefers_start(q)
        assert _query_prefers_end(q)


# ══════════════════════════════════════════════════════════════
# Phase 1.D — _p3_prescan END 分支
# ══════════════════════════════════════════════════════════════

def _make_log(tmp_path, anchors):
    """生成合成 log：在 anchors 行号写 UVM_ERROR，其他行填占位。
    anchors 是 1-based 行号列表，total_lines = max(anchors) + 100。
    """
    total = max(anchors) + 100
    lines = []
    aset = set(anchors)
    for ln in range(1, total + 1):
        if ln in aset:
            lines.append(f'UVM_ERROR /sim/tb.sv({ln}) @ {ln}ns: env [E_{ln}] something\n')
        else:
            lines.append(f'INFO line {ln}\n')
    f = tmp_path / 'sim.log'
    f.write_text(''.join(lines), encoding='utf-8')
    return str(f)


class TestPrescanEndBranch:
    def test_end_query_picks_last_anchor_window(self, tmp_path):
        # 锚点在 100, 5000, 9500；max_lines=500
        # END 分支应取 win_end ≈ 9500+50，win_start ≈ win_end-500+1
        path = _make_log(tmp_path, [100, 5000, 9500])
        ws, we, span, fa, la, total = _p3_prescan(
            path, '最后的报错', extra_patterns=[], max_lines=500)
        assert la == 9500
        assert ws <= 9500 <= we
        # END 分支：窗口紧贴末锚点
        assert we >= 9500
        assert we - ws + 1 == 500 or we == total

    def test_start_query_picks_first_anchor_window(self, tmp_path):
        path = _make_log(tmp_path, [100, 5000, 9500])
        ws, we, span, fa, la, total = _p3_prescan(
            path, '最早的报错', extra_patterns=[], max_lines=500)
        assert fa == 100
        assert ws <= 100 <= we
        # START 分支：win_start = max(1, first_anchor-50) = 50
        assert ws == 50

    def test_no_intent_uses_density_window(self, tmp_path):
        # 50 个锚点聚集在 5000-5049，1 个孤立在 9500
        anchors = list(range(5000, 5050)) + [9500]
        path = _make_log(tmp_path, anchors)
        ws, we, span, fa, la, total = _p3_prescan(
            path, '什么报错', extra_patterns=[], max_lines=500)
        # 滑窗应选 5000-5049 这个 50-anchor 簇，孤立的 9500 被忽略
        assert ws <= 5000
        assert we >= 5049
        assert we < 9500


# ══════════════════════════════════════════════════════════════
# Phase 1.F — coverage_warning 行为（间接通过 _p3_prescan 的返回检查）
# ══════════════════════════════════════════════════════════════

class TestCoverageF:
    def test_low_outside_estimate_no_warning(self, tmp_path):
        # 锚点跨度 (last - first) 略大于 max_lines，但窗外内容估算 < max_lines/2
        # _p3_prescan 本身不返 warning，但在 custom_extract 路由层判断；
        # 这里只验证 prescan 能返回正常 total_span 让上层判断
        path = _make_log(tmp_path, [100, 200, 300, 400, 500])
        ws, we, span, fa, la, total = _p3_prescan(
            path, '错误', extra_patterns=[], max_lines=2500)
        # 锚点跨度 401 < max_lines 2500，应直接走"全锚点已在范围内"分支
        assert span == 401
        assert ws <= 100 <= we
        assert ws <= 500 <= we

    def test_high_outside_estimate_triggers_warning(self, tmp_path):
        # 锚点跨度远超 max_lines —— 上层应触发 warning
        path = _make_log(tmp_path, [100, 5000, 10000])
        ws, we, span, fa, la, total = _p3_prescan(
            path, '错误', extra_patterns=[], max_lines=500)
        assert span == 9901
        assert (we - ws + 1) <= 500
        # 上层 custom_extract 会用 outside_estimate = span - window_span = 9901-500 = 9401
        # > max_lines/2 = 250 → 触发 warning
        outside_estimate = span - (we - ws + 1)
        assert outside_estimate > 500 // 2


# ══════════════════════════════════════════════════════════════
# Phase 2.B+E — 锚点权重 + 文件路径加权
# ══════════════════════════════════════════════════════════════

def _make_mixed_log(tmp_path, fatal_lines=None, error_lines=None,
                    warning_lines=None, custom_lines=None, total_lines=None):
    """生成包含 FATAL/ERROR/WARNING 混合的合成 log。
    custom_lines = {lineno: 'raw content'}  用于注入特定字符串（如文件路径）。
    """
    fatal_lines    = set(fatal_lines or [])
    error_lines    = set(error_lines or [])
    warning_lines  = set(warning_lines or [])
    custom_lines   = custom_lines or {}
    if total_lines is None:
        all_marked = fatal_lines | error_lines | warning_lines | set(custom_lines.keys())
        total_lines = (max(all_marked) if all_marked else 100) + 100
    out = []
    for ln in range(1, total_lines + 1):
        if ln in custom_lines:
            out.append(custom_lines[ln] + '\n')
        elif ln in fatal_lines:
            out.append(f'UVM_FATAL /sim/dut.sv({ln}) @ {ln}ns: env [F_{ln}] critical_text\n')
        elif ln in error_lines:
            out.append(f'UVM_ERROR /sim/tb.sv({ln}) @ {ln}ns: env [E_{ln}] error_text\n')
        elif ln in warning_lines:
            out.append(f'UVM_WARNING /sim/tb.sv({ln}) @ {ln}ns: env [W_{ln}] warn_text\n')
        else:
            out.append(f'INFO line {ln}\n')
    f = tmp_path / 'mix.log'
    f.write_text(''.join(out), encoding='utf-8')
    return str(f)


class TestWeightsB:
    def test_fatal_outweighs_warning_when_keyword_matches(self, tmp_path):
        # 1 个 FATAL @ L100，50 个 WARNING @ L500-549
        # query "致命" → fatal 同义词命中 FATAL 行（含 "fatal" lower）
        # 预期：选 FATAL 窗口而非 WARNING 簇
        path = _make_mixed_log(
            tmp_path,
            fatal_lines=[100],
            warning_lines=list(range(500, 550)),
            total_lines=10000,
        )
        ws, we, span, fa, la, total = _p3_prescan(
            path, '致命错误', extra_patterns=[], max_lines=500)
        # 应包含 FATAL@100，不包含整个 WARNING 簇 (L500-549) 之外的部分
        assert ws <= 100 <= we
        # 窗口应"贴着"FATAL，不被 50 个 WARNING 拉走
        assert ws < 500 or we < 549

    def test_warning_query_picks_warning_cluster(self, tmp_path):
        # 同样布局，query 是 "warning"——应选 WARNING 簇
        path = _make_mixed_log(
            tmp_path,
            fatal_lines=[100],
            warning_lines=list(range(500, 550)),
            total_lines=10000,
        )
        ws, we, span, fa, la, total = _p3_prescan(
            path, '所有 warning', extra_patterns=[], max_lines=500)
        # WARNING 簇 L500-549 应在窗口内
        assert ws <= 500
        assert we >= 549

    def test_no_keyword_fallback_to_uvm_density(self, tmp_path):
        # 中性查询，没有 kw 命中
        # 5 FATAL @ L1000-L1004 vs 50 WARNING @ L5000-L5049
        # FATAL 簇权重 = 5×5 = 25；WARNING 簇 = 1×50 = 50
        # 密度兜底应选 WARNING 簇（虽然 FATAL 更严重，但中性查询无法判断）
        path = _make_mixed_log(
            tmp_path,
            fatal_lines=list(range(1000, 1005)),
            warning_lines=list(range(5000, 5050)),
            total_lines=10000,
        )
        ws, we, span, fa, la, total = _p3_prescan(
            path, '什么事', extra_patterns=[], max_lines=500)
        # Density picks the heavier cluster (WARNING by raw count)
        assert ws <= 5000
        assert we >= 5049


class TestModulePathE:
    def test_module_name_in_path_anchors(self, tmp_path):
        # 三个 ERROR：两个在 axi_driver.sv，一个在其他 sv
        # 查询 "axi_driver 错误"——kw 'axi_driver' 命中 axi 路径行
        path = _make_mixed_log(
            tmp_path,
            custom_lines={
                100: 'UVM_ERROR /proj/tb/axi/axi_driver.sv(50) @ 100ns: env [E1] m1',
                200: 'UVM_ERROR /proj/tb/other/other_mod.sv(20) @ 200ns: env [E2] m2',
                300: 'UVM_ERROR /proj/tb/axi/axi_driver.sv(80) @ 300ns: env [E3] m3',
            },
            total_lines=1000,
        )
        ws, we, span, fa, la, total = _p3_prescan(
            path, 'axi_driver 错误', extra_patterns=[], max_lines=500)
        # 窗口应覆盖 L100 和 L300（两条 axi_driver.sv 行）
        assert ws <= 100
        assert we >= 300


class TestSynonymsCoverage:
    def test_chinese_chu_shi_hua_synonym_anchors_init(self, tmp_path):
        # 一行包含 "init" 关键词，一行普通 ERROR
        path = _make_mixed_log(
            tmp_path,
            custom_lines={
                500: 'UVM_INFO env [INIT] init phase started @ 500ns',
                900: 'UVM_ERROR /proj/tb/other.sv(20) @ 900ns: env [E] something',
            },
            total_lines=2000,
        )
        ws, we, span, fa, la, total = _p3_prescan(
            path, '初始化阶段', extra_patterns=[], max_lines=500)
        # "初始化" 触发 START；同时 init 同义词命中 L500
        # PREFER_START 优先生效，但锚点至少应包含 L500（含 init 关键词）
        assert ws <= 500 <= we


# ══════════════════════════════════════════════════════════════
# Phase 3.C — 多块聚簇 + 预算分配
# ══════════════════════════════════════════════════════════════

class TestClusterAnchors:
    def test_single_cluster_when_close_anchors(self):
        weights = {100: 5, 110: 3, 120: 1}
        clusters = _cluster_anchors(weights, gap_threshold=50)
        assert len(clusters) == 1
        assert clusters[0] == (100, 120, 9)

    def test_split_on_large_gap(self):
        weights = {100: 5, 110: 3, 5000: 5, 5010: 3}
        clusters = _cluster_anchors(weights, gap_threshold=200)
        assert len(clusters) == 2
        assert clusters[0] == (100, 110, 8)
        assert clusters[1] == (5000, 5010, 8)

    def test_gap_exactly_threshold_keeps_in_cluster(self):
        # gap == threshold 不应分簇（gap > threshold 才分）
        weights = {100: 5, 200: 5}
        clusters = _cluster_anchors(weights, gap_threshold=100)
        assert len(clusters) == 1

    def test_empty(self):
        assert _cluster_anchors({}, gap_threshold=50) == []


class TestAllocateBlocks:
    def test_single_cluster_padded(self):
        clusters = [(500, 510, 10)]
        blocks = _allocate_blocks(clusters, max_lines=1000, total_lines=10000)
        assert len(blocks) == 1
        s, e, w = blocks[0]
        # cluster 11 行 + 2*50 padding = 111 行
        assert s == 450
        assert e == 560
        assert w == 10

    def test_multiple_clusters_within_budget(self):
        clusters = [(100, 100, 5), (5000, 5000, 5)]
        blocks = _allocate_blocks(clusters, max_lines=500, total_lines=10000)
        # 两块都装下：每块 1+100=101 行，总 202 < 500
        assert len(blocks) == 2
        assert blocks[0][0] <= 100 <= blocks[0][1]
        assert blocks[1][0] <= 5000 <= blocks[1][1]

    def test_overlapping_blocks_merge(self):
        # 两个相邻簇 padding 后会重叠：[50, 200] 和 [150, 350]
        clusters = [(100, 150, 5), (200, 300, 3)]
        blocks = _allocate_blocks(clusters, max_lines=2000, total_lines=10000, padding=50)
        # [50, 200] + [150, 350] 应合并为 [50, 350]
        assert len(blocks) == 1
        assert blocks[0][0] == 50
        assert blocks[0][1] >= 350
        assert blocks[0][2] == 8   # 权重相加

    def test_low_weight_cluster_dropped_when_budget_exhausted(self):
        # 第一个高权重簇就会吃掉大部分预算
        clusters = [(100, 600, 100), (5000, 5000, 1)]
        blocks = _allocate_blocks(clusters, max_lines=600, total_lines=10000)
        # 第一个簇 501+100 = 601 > 600 → 收缩到 600
        # 用完预算后，第二个簇应被丢弃
        assert all(b[0] < 5000 for b in blocks) or len(blocks) == 1


class TestPrescanBlocks:
    def test_close_anchors_one_block(self, tmp_path):
        path = _make_log(tmp_path, [100, 110, 120])
        blocks, fa, la, total = _p3_prescan_blocks(
            path, '错误', extra_patterns=[], max_lines=500)
        assert len(blocks) == 1
        bs, be, bw = blocks[0]
        assert bs <= 100 <= 120 <= be

    def test_far_apart_anchors_two_blocks(self, tmp_path):
        # FATAL@100 + FATAL@9000，gap=8900 远大于 max_lines/4=125
        path = _make_log(tmp_path, [100, 9000])
        # 必须用 query 关键词命中两个 FATAL（"fatal" 匹配 UVM_FATAL）
        # 但 _make_log 用的是 UVM_ERROR，所以 query "error" 命中两个 ERROR
        blocks, fa, la, total = _p3_prescan_blocks(
            path, 'error', extra_patterns=[], max_lines=500)
        assert len(blocks) == 2
        # 两块各覆盖一个锚点
        block_starts = sorted(b[0] for b in blocks)
        block_ends   = sorted(b[1] for b in blocks)
        assert block_starts[0] <= 100 <= block_ends[0]
        assert block_starts[1] <= 9000 <= block_ends[1]

    def test_start_intent_keeps_first_cluster_only(self, tmp_path):
        path = _make_log(tmp_path, [100, 9000])
        blocks, fa, la, total = _p3_prescan_blocks(
            path, '最早的 error', extra_patterns=[], max_lines=500)
        # PREFER_START → 仅保留第一簇
        assert len(blocks) == 1
        assert blocks[0][0] <= 100 <= blocks[0][1]
        assert blocks[0][1] < 9000

    def test_end_intent_keeps_last_cluster_only(self, tmp_path):
        path = _make_log(tmp_path, [100, 9000])
        blocks, fa, la, total = _p3_prescan_blocks(
            path, '最后的 error', extra_patterns=[], max_lines=500)
        # PREFER_END → 仅保留最后簇
        assert len(blocks) == 1
        assert blocks[0][0] <= 9000 <= blocks[0][1]
        assert blocks[0][0] > 100

    def test_start_and_end_keeps_both(self, tmp_path):
        # 三个锚点：100, 5000, 9000
        path = _make_log(tmp_path, [100, 5000, 9000])
        blocks, fa, la, total = _p3_prescan_blocks(
            path, '最早和最后的 error', extra_patterns=[], max_lines=500)
        # 同时含 START + END → 保留首末两簇（中间被丢）
        assert len(blocks) == 2
        starts = sorted(b[0] for b in blocks)
        ends   = sorted(b[1] for b in blocks)
        assert starts[0] <= 100 <= ends[0]
        assert starts[1] <= 9000 <= ends[1]

    def test_no_anchors_returns_default_block(self, tmp_path):
        # 文件无 UVM 也无关键词命中
        f = tmp_path / 'plain.log'
        f.write_text('\n'.join(f'INFO line {i}' for i in range(1, 1001)),
                     encoding='utf-8')
        blocks, fa, la, total = _p3_prescan_blocks(
            str(f), 'no match query xyzqqq', extra_patterns=[], max_lines=500)
        # 默认返回 [1, 500]
        assert len(blocks) == 1
        assert blocks[0][0] == 1
        assert blocks[0][1] == 500


# ══════════════════════════════════════════════════════════════
# 自适应 max_lines（context_window 安全阀）
# ══════════════════════════════════════════════════════════════

class TestAdaptiveMaxLines:
    def test_user_cap_wins_when_context_is_large(self):
        # 用户配 p3_max_lines=2500，context 100K：可装 ~2778 行 > 2500，取小 = 2500
        cfg = {'context_window': 100000, 'p3_chars_per_token': 4, 'p3_max_lines': 2500}
        assert _adaptive_max_lines(cfg) == 2500

    def test_context_cap_wins_when_user_setting_is_aggressive(self):
        # 用户激进配 p3_max_lines=50000，但 context 仅 32K：必须钳到 context 允许范围
        cfg = {'context_window': 32000, 'p3_chars_per_token': 4, 'p3_max_lines': 50000}
        n = _adaptive_max_lines(cfg)
        # (32000 - 800) × 4 × 0.7 / 100 = ~873 行
        assert n < 1000
        assert n >= _MIN_LINES_FLOOR

    def test_context_uplift_when_user_allows(self):
        # 用户在 UI 把 p3_max_lines 提到 10000，context 200K（Claude）→ 应当放大到 ~5570 行
        cfg = {'context_window': 200000, 'p3_chars_per_token': 4, 'p3_max_lines': 10000}
        n = _adaptive_max_lines(cfg)
        # (200000 - 800) × 4 × 0.7 / 100 ≈ 5577；用户上限 10000
        # 取小 = 5577
        assert 5000 < n < 6000

    def test_floor_protects_tiny_context(self):
        # 极端：context 只有 2000 tokens（不现实但要保底）
        cfg = {'context_window': 2000, 'p3_chars_per_token': 4, 'p3_max_lines': 500}
        n = _adaptive_max_lines(cfg)
        assert n >= _MIN_LINES_FLOOR

    def test_chars_per_token_affects_estimate(self):
        # chars_per_token=2（中文实际值）→ 比默认 4 给出更小的 max_lines
        # 因为字符量估算变小
        cfg_4 = {'context_window': 50000, 'p3_chars_per_token': 4, 'p3_max_lines': 99999}
        cfg_2 = {'context_window': 50000, 'p3_chars_per_token': 2, 'p3_max_lines': 99999}
        assert _adaptive_max_lines(cfg_2) < _adaptive_max_lines(cfg_4)

    def test_default_when_keys_missing(self):
        # cfg 全空 → 使用各字段默认值（context_window=100000, cpt=4, p3_max_lines=2500）
        n = _adaptive_max_lines({})
        # 计算结果 ~2778，用户上限 2500 → 返 2500
        assert n == 2500


# ══════════════════════════════════════════════════════════════
# file_index 越界校验（多文件场景下的 P2 路由参数）
# ══════════════════════════════════════════════════════════════

class TestFileIndexValidation:
    """对应 /llm/custom_extract 的 file_index 越界检查——纯函数。"""

    def test_default_zero_with_files(self):
        assert _validate_file_index(0, 1)
        assert _validate_file_index(0, 5)

    def test_index_in_range(self):
        assert _validate_file_index(2, 5)
        assert _validate_file_index(4, 5)

    def test_index_at_upper_bound_rejected(self):
        # 5 个文件下标为 0..4，传 5 越界
        assert not _validate_file_index(5, 5)

    def test_negative_rejected(self):
        assert not _validate_file_index(-1, 5)

    def test_empty_file_paths(self):
        # 0 个文件——任何 idx 都越界（包括 0）
        assert not _validate_file_index(0, 0)

    def test_non_int_rejected(self):
        assert not _validate_file_index('0', 5)   # 字符串
        assert not _validate_file_index(1.5, 5)   # 浮点
        assert not _validate_file_index(None, 5)  # None
        # 注意：bool 是 int 的子类，True == 1，会被当作有效；
        # 路由层在 int(data.get(...)) 时已经把传入值规整为 int
