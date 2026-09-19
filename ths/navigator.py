"""同花顺页面导航: 股票信息读取、下一只按钮定位与点击。

实测 (同花顺 Android):
- 下一只按钮 resource-id: .../al_rightbutton, content-desc: 切到下一支股票
- 标题名称节点 resource-id: .../navi_title_text
- 股票代码通常为自绘渲染, 不在 UI 树中, 因此采用 标题名称 -> 代码 反查
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Dict, Optional, Tuple

from adb.control import UIController, center_of
from adb.device import AdbError
from config import AppConfig

log = logging.getLogger("navigator")


class Navigator:
    def __init__(self, control: UIController, cfg: AppConfig,
                 name_to_code: Optional[Dict[str, str]] = None):
        self.control = control
        self.cfg = cfg
        self.name_to_code = name_to_code or {}
        # 按钮坐标直接使用配置中的实测值 (位置固定, UI dump定位需3.5s, 不值得)
        self._next_pos: Tuple[int, int] = tuple(cfg.buttons.next_stock)

    def get_stock_info(self) -> Tuple[str, str]:
        """返回 (股票代码, 股票名称)。失败返回空串, 不抛异常。"""
        try:
            xml = self.control.dump_ui()
        except AdbError:
            return "", ""
        return self.extract_info(xml)

    def extract_info(self, xml: str) -> Tuple[str, str]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return "", ""
        pat = re.compile(self.cfg.ui.stock_code_pattern)
        title_name = ""
        for node in root.iter("node"):
            text = (node.get("text") or "").strip()
            desc = (node.get("content-desc") or "").strip()
            if not title_name:
                rid = node.get("resource-id") or ""
                if "navi_title_text" in rid and text:
                    title_name = text
            for candidate in (text, desc):
                if candidate:
                    m = pat.search(candidate)
                    if m:
                        return m.group(0), title_name
        if title_name and title_name in self.name_to_code:
            return self.name_to_code[title_name], title_name
        return "", title_name

    def find_next_button(self, xml: str) -> Optional[Tuple[int, int]]:
        """在 UI 树中查找下一只按钮, 优先可点击节点。"""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None
        keywords = [k.lower() for k in self.cfg.ui.next_button_keywords]
        id_keys = [i.lower() for i in self.cfg.ui.next_button_ids]
        clickable_hits, other_hits = [], []
        for node in root.iter("node"):
            rid = (node.get("resource-id") or "").lower()
            text = ((node.get("text") or "") + " "
                    + (node.get("content-desc") or "")).strip().lower()
            id_hit = any(k in rid for k in id_keys)
            kw_hit = any(k in text for k in keywords if k)
            if not (id_hit or kw_hit):
                continue
            pos = center_of(node.get("bounds", ""))
            if not pos:
                continue
            if node.get("clickable") == "true":
                clickable_hits.append(pos)
            else:
                other_hits.append(pos)
        hits = clickable_hits or other_hits
        return hits[0] if hits else None

    def tap_next(self) -> Tuple[int, int]:
        """点击下一只按钮, 使用配置中的实测坐标。"""
        self.control.tap(*self._next_pos)
        return self._next_pos

    def goto(self, code: str):
        """搜索跳转到指定股票的日K页 (随机访问, 不依赖'>'循环)。

        成功后停留在目标股票日K页; 落页代码由调用方截图验证。
        失败抛 AdbError。

        日K tab一律走UI dump动态定位+选中态校验, 不用静态坐标:
        盘后/竞价时段"盘后固定价格交易"等横条会把周期tab行整体下压
        约108px(实测664→772), 静态坐标点空后落页停在分时页, 而分时页
        没有多/空角标, 持仓巡检会把卖出信号静默读成NONE。落页偶发的
        速递/异动解读模态弹窗会吞掉tab点击, back()先关弹窗再重试。
        """
        g = self.cfg.goto_nav
        last_err = ""
        for attempt in range(1, max(1, g.max_retries) + 1):
            try:
                self.control.tap(*g.search_btn)          # 顶栏搜索
                time.sleep(g.search_load_delay)
                self.control.tap(*g.input_box)           # 聚焦输入框
                self.control.input_text(code)            # 输入6位代码
                time.sleep(g.suggest_delay)              # 等联想列表
                self.control.tap(*g.first_suggestion)    # 首条联想
                time.sleep(g.page_load_delay)            # 等落页(分时)
                if self._ensure_kline_after_goto():
                    return
                last_err = "日K tab点击后未确认选中(可能被弹窗遮挡)"
                log.warning("goto(%s) 第%d次日K确认失败: %s",
                            code, attempt, last_err)
            except AdbError as e:
                last_err = str(e)
            if attempt < max(1, g.max_retries):
                time.sleep(1.0)
        raise AdbError(f"goto({code})失败: {last_err}")

    def _ensure_kline_after_goto(self) -> bool:
        """goto落页后确保停在日K页: 动态点tab→校验选中态;
        未选中则back()关闭可能遮挡的模态弹窗后再试一次。"""
        if self.ensure_kline_page(verify=True):
            return True
        try:
            self.control.back()  # 有模态弹窗时back关弹窗; 无弹窗时退回上页
        except AdbError:
            pass
        time.sleep(0.8)
        return self.ensure_kline_page(verify=True)

    # ---------- 日K tab 动态定位 ----------

    def _kline_tab_node(self) -> Optional[Tuple[Tuple[int, int], bool]]:
        """在UI树中找日K tab, 返回 (中心坐标, 是否选中态); 找不到返回None。

        盘后"盘后固定价格交易"横条会挤压周期tab行, 硬编码坐标(227,664)失效,
        必须动态解析bounds。
        """
        try:
            xml = self.control.dump_ui()
            root = ET.fromstring(xml)
        except (AdbError, ET.ParseError):
            return None
        for node in root.iter("node"):
            if (node.get("text") or "").strip() == "日K":
                pos = center_of(node.get("bounds", ""))
                if pos:
                    return pos, (node.get("selected") or "").lower() == "true"
        return None

    def ensure_kline_page(self, verify: bool = False) -> bool:
        """确保当前处于日K页: 动态定位tab点击, 选中态校验, 未选中补点一次。

        返回True=已确认选中; False=无法确认(UI dump失败或补点后仍未选中,
        仅记日志不抛异常, 由调用方决定是否继续)。

        verify=True(goto持仓巡检用): 点击后重新dump确认选中态, 未选中再
        补点一次并最终确认。多2次UI dump(约4s/次), 但持仓巡检必须确认
        落在日K页——分时页没有多/空角标, 误判NONE会漏掉卖出信号。
        """
        tab = self._kline_tab_node()
        if tab is not None and tab[1]:
            return True                      # 已在日K页, 无需点击
        pos = tab[0] if tab else tuple(self.cfg.goto_nav.kline_tab)
        if tab is None:
            log.warning("日K tab动态定位失败, 兜底配置坐标%s", pos)
        self.control.tap(*pos)
        time.sleep(self.cfg.goto_nav.kline_load_delay)
        if not verify:
            # 主循环路径: 不二次dump校验(本机dump约4s/次); 坐标来自同帧
            # 动态定位点中率高, 万一点偏PageDetector/enter_watchlist兜底
            if tab is None:
                return False
            log.info("日K tab未选中态, 已按动态坐标补点 %s", pos)
            return True
        # goto路径: 重新dump确认; 仍未选中(弹窗吞键等)补点一次再确认
        tab2 = self._kline_tab_node()
        if tab2 is not None and tab2[1]:
            return True
        if tab2 is not None:
            self.control.tap(*tab2[0])
            time.sleep(self.cfg.goto_nav.kline_load_delay)
            tab3 = self._kline_tab_node()
            return tab3 is not None and tab3[1]
        return False

    def is_in_watchlist_cycle(self) -> bool:
        """当前是否处于自选股'>'循环上下文(日K选中 且 下一只按钮存在)。

        续扫探测用: goto搜索落页虽有日K但al_rightbutton节点不存在,
        无法沿'>'续扫, 必须重经自选列表恢复上下文。单次dump同时判断两项。
        """
        try:
            xml = self.control.dump_ui()
            root = ET.fromstring(xml)
        except (AdbError, ET.ParseError):
            return False
        kline_selected = False
        has_next = False
        for node in root.iter("node"):
            if (node.get("text") or "").strip() == "日K":
                kline_selected = (node.get("selected") or "").lower() == "true"
            rid = node.get("resource-id") or ""
            if rid.endswith("al_rightbutton"):
                has_next = True
        return kline_selected and has_next

    # 自选列表行 desc 格式: "平潭发展#000592"
    _WATCH_ROW_RE = re.compile(r"^(.+)#(\d{6})$")

    def enter_watchlist(self, target_code: str = "") -> bool:
        """从任意页面回到自选股行情页 (日K, 带<>循环箭头)。

        goto搜索落页后<>箭头消失且处于错误分组, 必须经"自选"列表重新进入
        才能恢复自选股循环上下文。
        - target_code 非空: 尝试打开列表中对应行; 不在列表则打开第一只
        - 不传: 打开列表第一只
        成功返回 True; 超时未找到列表返回 False (不抛异常, 调用方决策)。
        """
        h = self.cfg.home_nav
        _t0 = time.time()
        for _ in range(max(4, h.max_back + 3)):
            _ti = time.time()
            try:
                xml = self.control.dump_ui()
            except AdbError:
                self.control.back()
                time.sleep(h.back_delay)
                log.info("enter_watchlist: dump失败back (%.1fs)",
                         time.time() - _ti)
                continue
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                self.control.back()
                time.sleep(h.back_delay)
                log.info("enter_watchlist: xml解析失败back (%.1fs)",
                         time.time() - _ti)
                continue

            rows = []           # (code, center)
            zixuan_tab = None   # 底部"自选"tab中心
            for node in root.iter("node"):
                rid = node.get("resource-id") or ""
                t = (node.get("text") or "").strip()
                if rid.endswith("fixed_column"):
                    m = self._WATCH_ROW_RE.match(
                        (node.get("content-desc") or "").strip())
                    if m:
                        pos = center_of(node.get("bounds", ""))
                        if pos:
                            rows.append((m.group(2), pos))
                elif t == "自选" and rid.endswith("title"):
                    zixuan_tab = center_of(node.get("bounds", ""))

            if rows:
                # 下拉刷新触发云端数据更新(列表结构/行位不变, 复用首帧rows坐标,
                # 不再二次dump——本机uiautomator dump单次约4s, 省一次)
                _tr = time.time()
                try:
                    self.control.swipe(540, 920, 540, 1500, 450)
                    time.sleep(0.8)        # 等刷新手势+数据返回
                except AdbError:
                    pass
                log.info("enter_watchlist: 列表就绪%d只 下拉刷新%.1fs",
                         len(rows), time.time() - _tr)
                tx, ty = rows[0][1]
                for code, pos in rows:
                    if target_code and code == target_code:
                        tx, ty = pos
                        break
                self.control.tap(tx, ty)             # 打开行情页
                time.sleep(0.8)                      # 行情页加载(dump前过渡)
                _tk = time.time()
                self.ensure_kline_page()             # 动态切日K(盘后坐标会偏移)
                log.info("enter_watchlist: 完成 总耗时%.1fs (切日K %.1fs)",
                         time.time() - _t0, time.time() - _tk)
                return True

            if zixuan_tab:
                # 主页: 进自选tab
                self.control.tap(*zixuan_tab)
                time.sleep(h.tab_load_delay)
                log.info("enter_watchlist: 点自选tab (累计%.1fs)",
                         time.time() - _t0)
            else:
                # 行情页/搜索页/其他: 返回(下次dump约4s天然充当过渡等待)
                self.control.back()
                log.info("enter_watchlist: back (累计%.1fs)",
                         time.time() - _t0)
        log.warning("enter_watchlist: %d次尝试后失败 总耗时%.1fs",
                    max(4, h.max_back + 3), time.time() - _t0)
        return False
