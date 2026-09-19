"""第一优先识别通道: Android UI Hierarchy。

在 UI 树中查找 text/content-desc 恰为 "多"/"空" 的元素,
并按 latest_k_region 过滤归属, 避免把历史信号误判为最新信号。
"""
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

from adb.control import parse_bounds
from adb.screenshot import region_contains
from models.result import DetectionInfo, Signal

SIGNAL_CHARS = {"多": Signal.LONG, "空": Signal.SHORT}


class UIHierarchyDetector:
    name = "ui_hierarchy"

    def detect(self, xml: str, region) -> Tuple[Optional[Signal], DetectionInfo]:
        """返回 (Signal 或 None, DetectionInfo)。

        state=found      -> 区域内发现信号元素
        state=not_found  -> dump 成功但区域内没有信号元素
        state=unavailable-> dump 为空或解析失败
        """
        info = DetectionInfo(source=self.name)
        if not xml:
            info.error = "UI XML 为空"
            return None, info
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            info.error = f"UI XML 解析失败: {e}"
            return None, info

        hits = []
        for node in root.iter("node"):
            for attr in ("text", "content-desc"):
                text = (node.get(attr) or "").strip()
                if text in SIGNAL_CHARS:
                    bounds = parse_bounds(node.get("bounds", ""))
                    if bounds:
                        cx = (bounds[0] + bounds[2]) // 2
                        cy = (bounds[1] + bounds[3]) // 2
                        if region_contains(region, cx, cy):
                            hits.append((SIGNAL_CHARS[text], cx, cy, bounds))
                    break

        if not hits:
            info.state = "not_found"
            info.error = "UI树中未在检测区域发现多/空元素"
            return None, info

        kinds = {h[0] for h in hits}
        # 最新K线是屏幕最右侧K线, 同类多命中时取最靠右的
        rightmost = max(hits, key=lambda h: h[1])
        b = rightmost[3]
        info.state = "found"
        info.confidence = 0.99
        info.bbox = [b[0], b[1], b[2] - b[0], b[3] - b[1]]
        info.notes = f"区域内命中{len(hits)}处"

        if len(kinds) == 1:
            return kinds.pop(), info
        info.error = "检测区域内同时存在多与空元素, 无法判定归属"
        return Signal.UNKNOWN, info
