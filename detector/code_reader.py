"""股票代码读取: 标题代码行数字模板匹配 (替代每股一次的 UI dump)。

原理: 代码行为固定字号(h=31px)白色数字, 0-9 模板各匹配后按中心x聚类,
取"连续6个且间距均匀"的最高分窗口拼码。非通用OCR — 仅10个固定字形模板。
失败由上层回退 UI dump 名称反查。
"""
import os
from typing import List, Tuple

import cv2
import numpy as np

from adb.screenshot import crop_region
from config import AppConfig
from detector.vision_detector import VisionDetector


class CodeReader:
    name = "code_reader"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.digits: List[Tuple[str, np.ndarray]] = []  # (字符, BGR模板)
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        d = self.cfg.resolve(self.cfg.vision.digits_dir)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if not fn.lower().endswith(".png"):
                    continue
                tpl = cv2.imread(os.path.join(d, fn), cv2.IMREAD_COLOR)
                if tpl is not None:
                    self.digits.append((os.path.splitext(fn)[0], tpl))
        self._loaded = True

    def read(self, screen_bgr: np.ndarray) -> Tuple[str, str]:
        """从截图标题代码行读6位代码。返回 (code, notes), 失败 code 为 ""。"""
        self._ensure_loaded()
        if not self.digits:
            return "", "数字模板缺失"
        crop = crop_region(screen_bgr, self.cfg.regions.title_code_region)
        if crop is None:
            return "", "代码行区域无效"
        h, w = crop.shape[:2]

        # 1) 每个数字模板独立匹配取峰值
        raw_hits = []  # (中心x, 字符, 分数)
        for ch, tpl in self.digits:
            th, tw = tpl.shape[:2]
            if th >= h or tw >= w:
                continue
            res = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
            for x, y, sc in VisionDetector._peaks(
                    res, self.cfg.detection.template_threshold, tw, th):
                raw_hits.append((x + tw / 2, ch, sc))

        # 2) 跨数字聚类: 中心x相近(<=14px)的命中保留分高者
        raw_hits.sort(key=lambda t: t[0])
        clusters = []  # [cx, ch, score]
        for cx, ch, sc in raw_hits:
            if clusters and cx - clusters[-1][0] <= 14:
                if sc > clusters[-1][2]:
                    clusters[-1] = [cx, ch, sc]
            else:
                clusters.append([cx, ch, sc])

        # 3) 滑窗: 连续6个、间距极差<=8px、总分最高的窗口
        best = None  # (总分, window)
        for i in range(len(clusters) - 5):
            win = clusters[i:i + 6]
            xs = [c[0] for c in win]
            gaps = [xs[j + 1] - xs[j] for j in range(5)]
            if max(gaps) - min(gaps) > 8:
                continue
            total = sum(c[2] for c in win)
            if best is None or total > best[0]:
                best = (total, win)
        if best is None:
            return "", f"数字命中{len(clusters)}个但无均匀6位序列"

        win = best[1]
        code = "".join(c[1] for c in win)
        scores = ",".join(f"{c[2]:.2f}" for c in win)
        return code, f"各位分数[{scores}]"
