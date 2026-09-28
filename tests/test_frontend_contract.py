"""部门知识问答中心前端静态契约测试。

只断言“静态可验证”的契约：模块入口、关键 DOM id、过期论文 / 演示文案不存在、
业务模式按钮的语义属性、表单与发送的反馈钩子，以及移动端防横向溢出的结构性 CSS 规则。

本环境未安装浏览器 / DOM 测试工具（无 playwright / puppeteer / node_modules），
因此窄屏“不产生横向溢出”以结构性规则断言 + 纯函数测试覆盖，而不是真实渲染。
"""

from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "static"
JS_DIR = STATIC / "js"
CSS = STATIC / "css" / "app.css"
INDEX = STATIC / "index.html"
STUDY = STATIC / "study"

MODULE_FILES = (
    "api.js", "app.js", "chart.js", "chat.js", "config.js",
    "knowledge-admin.js", "roles.js", "state.js", "ui.js", "utils.js",
)

# 改造后必须存在、且 JS 依赖的关键 DOM id
CRITICAL_IDS = (
    "sidebar", "sidebar-backdrop", "main",
    "view-home", "view-knowledge", "view-ingest",
    "mode-grid", "mode-workspace", "mode-ws-name", "prompt-chips",
    "chart-mode", "chart-granularity", "chart-type-seg", "chart-svg",
    "chart-data-badge", "chart-status", "chart-range", "chart-source",
    "stat-grid", "home-metrics", "session-list", "identity-select",
    "biz-selector", "biz-selector-btns", "biz-selector-notice",
    "task-input", "submit-btn", "spinner", "submit-text", "cancel-btn",
    "knowledge-admin-panel", "ingest-title", "ingest-content",
    "ingest-classification", "ingest-department", "btn-ingest",
    "btn-refresh-docs", "knowledge-admin-status", "knowledge-admin-list",
    "toast",
)

# 必须被删除 / 改写的过期文案（“演示身份”是后端 demo identity 的正式术语，不在此列）
STALE_PHRASES = (
    "50 篇", "50篇",
    "论文演示", "论文演示语料", "论文演示库", "论文问答评估",
    "RAG / GraphRAG 论文",
    "尚未接入业务数据",
    "演示数据",
    "真实数据 · 知识库",
)


def _strip_js_comments(src: str) -> str:
    """去掉 // 行注释与 /* */ 块注释，避免“禁止声称”的规则命中注释里的反例说明。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)//[^\n]*$", "", src)
    return src


def _read_all_frontend_sources() -> dict:
    sources = {INDEX.name: INDEX.read_text(encoding="utf-8")}
    sources[CSS.name] = CSS.read_text(encoding="utf-8")
    for path in sorted(JS_DIR.glob("*.js")):
        sources[path.name] = path.read_text(encoding="utf-8")
    return sources


class FrontendMainPageContractTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")
        self.css = CSS.read_text(encoding="utf-8")
        self.js = {p.name: p.read_text(encoding="utf-8") for p in JS_DIR.glob("*.js")}
        self.all_js = "\n".join(self.js.values())
        self.all_frontend = self.html + "\n" + self.css + "\n" + self.all_js

    # ── 模块入口 ──────────────────────────────────────────────
    def test_module_entrypoints_exist_and_are_wired(self):
        for name in MODULE_FILES:
            self.assertTrue((JS_DIR / name).is_file(), name)
        self.assertIn("/static/js/app.js", self.html)
        self.assertIn("/static/css/app.css", self.html)
        for dep in ("./chat.js", "./ui.js", "./chart.js", "./state.js",
                    "./config.js", "./api.js", "./knowledge-admin.js"):
            self.assertIn(dep, self.js["app.js"], dep)

    # ── 关键 DOM id ───────────────────────────────────────────
    def test_critical_dom_ids_present(self):
        for dom_id in CRITICAL_IDS:
            self.assertIn('id="%s"' % dom_id, self.html, dom_id)

    # ── 过期论文 / 演示文案不存在 ─────────────────────────────
    def test_stale_paper_and_demo_copy_removed(self):
        for phrase in STALE_PHRASES:
            self.assertNotIn(phrase, self.all_frontend, phrase)

    # ── 准确命名与检索上下文 ─────────────────────────────────
    def test_accurate_product_and_context_copy(self):
        self.assertIn("部门知识问答中心", self.html)
        self.assertIn("测试期种子知识", self.html + self.all_js)
        self.assertIn("非真实公司数据", self.html + self.all_js)

    def test_test_data_is_not_presented_as_real_company_data(self):
        # “真实公司数据”只允许出现在否定语（非真实公司数据）里
        positives = re.findall(r"(?<!非)真实公司数据", self.all_frontend)
        self.assertEqual(positives, [], "测试数据被错误地称为真实公司数据")
        self.assertIn("不代表任何真实经营结果", self.html)
        self.assertIn("非真实经营数据", self.js["chart.js"])

    # ── 业务模式按钮的语义属性 ───────────────────────────────
    def test_business_mode_buttons_expose_aria_semantics(self):
        self.assertIn('role="group"', self.html)
        self.assertIn("aria-pressed", self.js["ui.js"])
        self.assertIn("renderBizSelector", self.js["ui.js"])
        self.assertIn("data-mode", self.js["ui.js"])
        self.assertIn("data-connected", self.js["ui.js"])
        self.assertRegex(self.js["ui.js"], r"aria-pressed.*active")
        # 图表类型切换按钮同样可感知
        self.assertIn('aria-pressed="true"', self.html)
        self.assertIn('aria-pressed="false"', self.html)

    # ── 表单 / 发送反馈钩子 ──────────────────────────────────
    def test_send_and_form_feedback_hooks(self):
        self.assertIn('id="spinner"', self.html)
        self.assertIn('id="submit-text"', self.html)
        self.assertIn('id="cancel-btn"', self.html)
        self.assertIn("aria-busy", self.js["chat.js"])
        self.assertIn("updateRunState", self.js["chat.js"])
        # 资料提交：加载态 + 状态区可感知
        self.assertIn('id="ingest-spinner"', self.html)
        self.assertIn('id="ingest-text"', self.html)
        self.assertIn('role="status"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn("setIngestBusy", self.js["knowledge-admin.js"])
        self.assertIn("aria-busy", self.js["knowledge-admin.js"])
        self.assertIn("required", self.html)

    # ── 部门过滤：不得假称已生效 ─────────────────────────────
    def test_department_filter_is_not_claimed_active(self):
        self.assertIn("DEPT_FILTER_WIRED = false", self.js["config.js"])
        self.assertIn("待接线", self.html + self.all_js)
        # 只在“用户可见文案”里检查（JS 注释里的反例说明不算）
        visible = self.html + "\n" + "\n".join(
            _strip_js_comments(v) for v in self.js.values()
        )
        for false_claim in ("部门过滤已生效", "已按部门筛选", "部门过滤已启用"):
            self.assertNotIn(false_claim, visible, false_claim)
        # ask 请求体不得新增部门过滤参数（后端 extra=forbid）
        ask_line = self.js["chat.js"].split("/api/paper-qa/ask", 1)[1][:400]
        self.assertNotIn("applicable_departments", ask_line)
        self.assertNotIn("department", ask_line)

    # ── 移动端 / 响应式 / 可用性 CSS 结构性规则 ──────────────
    def test_mobile_and_responsive_css_rules(self):
        for rule in (
            "@media (max-width: 900px)",
            "mobile-open",
            "transform: translateX(-100%)",
            ".sidebar-backdrop",
            "@media (max-width: 768px)",
            "@media (max-width: 480px)",
            "@media (min-width: 1440px)",
            "overflow-x: hidden",
            "min-width: 0",
            "min-height: 44px",
            "prefers-reduced-motion: reduce",
            "@media (pointer: coarse)",
        ):
            self.assertIn(rule, self.css, rule)
        # 回归：旧的互相矛盾的移动端侧栏规则不得复活
        self.assertNotIn("#sidebar:not(.collapsed) { transform: translateX(0); }", self.css)

    def test_no_horizontal_overflow_guards(self):
        self.assertIn(".home-scroll, .chat-area { overflow-x: hidden; }", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertIn(".chart-table-wrap", self.css)

    # ── 图标：不用 emoji 作结构性图标 ────────────────────────
    def test_no_emoji_structural_icons(self):
        emoji = re.compile(
            "[\U0001F300-\U0001FAFF\U00002600-\U000027BF]"
        )
        for name, src in list(self.js.items()) + [("index.html", self.html)]:
            self.assertIsNone(emoji.search(src), "%s 仍含 emoji 结构性图标" % name)


class FrontendStudyPageContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (STUDY / "index.html").read_text(encoding="utf-8")
        self.js = (STUDY / "app.js").read_text(encoding="utf-8")
        self.css = (STUDY / "styles.css").read_text(encoding="utf-8")

    def test_study_views_and_ocr_entry_preserved(self):
        for view in ("capture", "library", "search", "review", "ocr"):
            self.assertIn('id="%s"' % view, self.html)
            self.assertIn('data-view="%s"' % view, self.html)
        self.assertIn("/api/study/ocr/jobs", self.js)
        self.assertIn("/api/study/ocr/health", self.js)
        self.assertIn("mock", self.html + self.js)
        self.assertIn("real", self.html + self.js)

    def test_study_search_and_library_contract_strings(self):
        combined = self.html + self.js
        for text in ("从问题提取的知识点", "可能遗漏的知识", "5 条证据",
                     "BM25", "向量", "图谱关联", "为什么推荐",
                     "按条件精确筛选与排序", "按意思找题，请使用“检索”"):
            self.assertIn(text, combined, text)
        self.assertIn("aria-expanded", combined)
        self.assertIn('id="sort-records"', self.html)

    def test_study_ocr_feedback_and_theme(self):
        self.assertIn('id="ocr-submit"', self.html)
        self.assertIn('id="ocr-spinner"', self.html)
        self.assertIn("setOcrBusy", self.js)
        self.assertIn("aria-busy", self.js)
        self.assertIn('data-state="checking"', self.html)
        self.assertIn("dataset.state", self.js)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("min-height: 44px", self.css)


@unittest.skipUnless(shutil.which("node"), "node 不可用：跳过前端纯函数测试")
class FrontendPureFunctionTests(unittest.TestCase):
    """用 node 直接加载纯函数模块，验证不依赖 DOM 的契约。"""

    def _run_node(self, expr: str):
        script = (
            "import('file://" + str(JS_DIR / "config.js") + "').then(async (cfg) => {"
            "  const roles = await import('file://" + str(JS_DIR / "roles.js") + "');"
            "  const utils = await import('file://" + str(JS_DIR / "utils.js") + "');"
            "  const out = (function(){ " + expr + " })();"
            "  process.stdout.write(JSON.stringify(out));"
            "});"
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_route_label_and_modes(self):
        out = self._run_node("return {label: cfg.routeLabel('INTERNAL_RETRIEVAL'),"
                             " modes: cfg.BUSINESS_MODES.length,"
                             " wired: cfg.DEPT_FILTER_WIRED,"
                             " questions: cfg.EXAMPLE_QUESTIONS.length};")
        self.assertEqual(out["label"], "知识库内部检索")
        self.assertEqual(out["modes"], 5)
        self.assertIs(out["wired"], False)
        self.assertGreaterEqual(out["questions"], 4)

    def test_role_meta_boundaries(self):
        out = self._run_node("return {emp: roles.roleMeta('employee').canSubmit,"
                             " acc: roles.roleMeta('accountant').canSubmit,"
                             " sup: roles.roleMeta('finance_supervisor').canReview,"
                             " unknown: roles.roleMeta('nope').id};")
        self.assertIs(out["emp"], False)
        self.assertIs(out["acc"], True)
        self.assertIs(out["sup"], True)
        self.assertEqual(out["unknown"], "employee")

    def test_pure_formatters(self):
        out = self._run_node("return {dur: utils.fmtDuration(1500),"
                             " score: utils.formatScore(0.5),"
                             " count: utils.fmtCount(null),"
                             " title: utils.normalizeSource({}, 0).title};")
        self.assertEqual(out["dur"], "1.5s")
        self.assertEqual(out["score"], "0.50")
        self.assertEqual(out["count"], "—")
        self.assertEqual(out["title"], "未命名文档")


if __name__ == "__main__":
    unittest.main()
