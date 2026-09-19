"""信号识别编排: UI Hierarchy 优先, 视觉模板兜底, 双通道决策。

决策矩阵 (UNKNOWN 永远不会自动降级为 NONE):
- UI 命中多/空         -> 直接采纳
- UI 区域内多空并存    -> UNKNOWN (歧义)
- UI 未命中/不可用     -> 看视觉
- 视觉命中多/空        -> 采纳
- 视觉歧义             -> UNKNOWN
- 双通道均"未发现"     -> NONE
- 任一通道不可用且另一通道也无结论 -> UNKNOWN
"""
import logging
import os
import time

import cv2

from adb.screenshot import crop_region
from config import AppConfig
from models.result import DetectionInfo, Signal

from .ui_detector import UIHierarchyDetector
from .vision_detector import VisionDetector

log = logging.getLogger("detector")


def _imwrite(path: str, img):
    ok, buf = cv2.imencode(os.path.splitext(path)[1], img)
    if ok:
        buf.tofile(path)


class SignalDetector:
    """预留接口: 未来自动定位最新K时, 只需替换本类的区域计算部分。"""

    def __init__(self, cfg: AppConfig,
                 ui_detector: UIHierarchyDetector, vision_detector: VisionDetector):
        self.cfg = cfg
        self.ui_detector = ui_detector
        self.vision_detector = vision_detector

    def detect(self, stock_code: str, img, xml: str = None):
        """对已截取的页面图像做信号识别。返回 (Signal, DetectionInfo)。

        img: 页面截图 (由调用方统一截图并计时)。
        xml: UI Hierarchy 文本; None/"" 表示跳过UI通道 (本App多/空为canvas自绘,
        UI树实测不可见, 仅在数字读码失败需要名称反查代码时才dump)。
        """
        region = self.cfg.regions.latest_k_region

        ui_sig, ui_info = self.ui_detector.detect(xml or "", region)
        vis_sig, vis_info = self.vision_detector.detect(img, region)
        final, info = self._decide(ui_sig, ui_info, vis_sig, vis_info)

        if self.cfg.detection.save_debug_screenshot:
            self._save_debug(stock_code, img, final, info, region)
        return final, info

    def _decide(self, ui_sig, ui_info: DetectionInfo, vis_sig, vis_info: DetectionInfo):
        if ui_info.state == "found" and ui_sig == Signal.UNKNOWN:
            return Signal.UNKNOWN, ui_info
        if ui_sig in (Signal.LONG, Signal.SHORT):
            return ui_sig, ui_info

        # UI 未给出结论
        if vis_sig in (Signal.LONG, Signal.SHORT):
            return vis_sig, vis_info
        if vis_sig == Signal.UNKNOWN:
            return Signal.UNKNOWN, vis_info

        if vis_info.state == "not_found":
            if ui_info.state == "not_found":
                vis_info.notes += " | 双通道一致未发现信号"
            else:
                vis_info.notes += " | UI不可用, 仅视觉确认无信号"
            return Signal.NONE, vis_info

        # 双通道均不可用
        fallback = ui_info if ui_info.state == "unavailable" else vis_info
        return Signal.UNKNOWN, fallback

    def _save_debug(self, stock_code: str, img, final: Signal,
                    info: DetectionInfo, region):
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            base = os.path.join(
                self.cfg.resolve(self.cfg.paths.screenshots_dir),
                f"{stock_code or 'unknown'}_{ts}",
            )
            _imwrite(base + "_raw.png", img)
            _imwrite(base + "_region.png", crop_region(img, region))

            ann = img.copy()
            cv2.rectangle(ann, (region[0], region[1]), (region[2], region[3]),
                          (0, 255, 255), 2)
            color = (0, 0, 255) if final == Signal.UNKNOWN else (0, 255, 0)
            if info.bbox and len(info.bbox) == 4:
                x, y, w, h = info.bbox
                cv2.rectangle(ann, (x, y), (x + w, y + h), color, 2)
            label = f"{final.value}|{info.source}|conf={info.confidence:.2f}"
            cv2.putText(ann, label, (max(10, region[0]), max(30, region[1] - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
            _imwrite(base + "_annotated.png", ann)
        except Exception as e:  # 调试截图失败不能影响扫描
            log.warning("保存调试截图失败: %s", e)
