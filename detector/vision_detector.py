"""视觉识别通道: OpenCV 模板匹配, 只在 latest_k_region 附近检测。

独立封装, 未来可整体替换为其他模型, 上层只依赖 detect() 接口。
模板文件: detector/templates/long_*.png ("多") / short_*.png ("空"),
建议从不同股票、红涨绿跌两种底色的截图上裁剪多张。

归属规则: 图表右对齐, 最新K线中心固定在 latest_k_center_x 附近。
角标中心必须落在 [center-tolerance, center+tolerance] 内才算最新K信号;
K线间距约22px而角标直径45px, 倒数第二根K的角标(实测x~979)会被带外排除。
"""
import os
from typing import List, Optional, Tuple

import cv2
import numpy as np

from adb.screenshot import crop_region
from config import AppConfig
from models.result import DetectionInfo, Signal


class VisionDetector:
    name = "vision"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.templates: List[Tuple[Signal, str, np.ndarray]] = []
        self._loaded = False

    def _templates_dir(self) -> str:
        return self.cfg.resolve(self.cfg.vision.templates_dir)

    def reload(self):
        self._loaded = False
        self.templates = []
        self._ensure_loaded()

    def _ensure_loaded(self):
        if self._loaded:
            return
        d = self._templates_dir()
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                path = os.path.join(d, fn)
                lower = fn.lower()
                kind = None
                if lower.startswith(self.cfg.vision.long_prefix) and lower.endswith(".png"):
                    kind = Signal.LONG
                elif lower.startswith(self.cfg.vision.short_prefix) and lower.endswith(".png"):
                    kind = Signal.SHORT
                if kind is None:
                    continue
                # 彩色匹配: 多/空角标结构相同仅颜色不同, 灰度匹配会导致互相误命中
                tpl = cv2.imread(path, cv2.IMREAD_COLOR)
                if tpl is not None:
                    self.templates.append((kind, fn, tpl))
        self._loaded = True

    def detect(self, screen_bgr: np.ndarray, region,
               threshold: Optional[float] = None
               ) -> Tuple[Optional[Signal], DetectionInfo]:
        """在 region(向外扩12px)内做模板匹配, 按"最新K中心带"归属。

        返回 (Signal 或 None, DetectionInfo):
        - 带内命中唯一类型 -> LONG/SHORT
        - 带内无命中 -> None (not_found; 带外命中视为历史角标记入 notes)
        - 多/空同时带内命中(同一根K不可能既是多又是空) -> UNKNOWN
        """
        info = DetectionInfo(source=self.name)
        threshold = self.cfg.detection.template_threshold if threshold is None else threshold
        center_x = self.cfg.detection.latest_k_center_x
        tol = self.cfg.detection.latest_k_tolerance

        self._ensure_loaded()
        if not self.templates:
            info.error = "模板缺失, 请向 detector/templates 放入 long_*.png / short_*.png"
            return None, info

        pad = 12
        crop = crop_region(screen_bgr, region, pad=pad)
        if crop is None:
            info.error = "检测区域无效(超出屏幕或尺寸为0)"
            return None, info
        h, w = crop.shape[:2]

        origin_x = max(0, region[0] - pad)
        origin_y = max(0, region[1] - pad)
        best = {}  # kind -> (cx, score, x, y, tw, th, fname)
        rejected = []  # 带外命中(历史角标)
        for kind, fname, tpl in self.templates:
            th, tw = tpl.shape[:2]
            if th >= h or tw >= w:
                continue
            res = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
            for x, y, sc in self._peaks(res, threshold, tw, th):
                cx = origin_x + x + tw / 2
                if abs(cx - center_x) <= tol:
                    cur = best.get(kind)
                    if cur is None or sc > cur[1]:
                        best[kind] = (cx, sc, x, y, tw, th, fname)
                else:
                    rejected.append(f"{kind.value}@x{cx:.0f}({sc:.2f})")

        l = best.get(Signal.LONG)
        s = best.get(Signal.SHORT)
        info.notes = (f"long_best={'%.3f' % l[1] if l else '-'}  "
                      f"short_best={'%.3f' % s[1] if s else '-'}  "
                      f"band=[{center_x - tol},{center_x + tol}]  threshold={threshold}")
        if rejected:
            info.notes += "  排除历史角标: " + " ".join(sorted(set(rejected)))

        if not best:
            # 模板匹配未命中, 尝试颜色掩码兜底
            if self.cfg.detection.color_fallback:
                fb = self._color_fallback(crop, origin_x, origin_y,
                                          center_x, tol)
                if fb is not None:
                    fb_kind, fb_area, fb_cx, fb_cy = fb
                    info.state = "found"
                    info.confidence = min(0.75, fb_area / 2000.0)
                    info.bbox = [int(fb_cx - 25), int(fb_cy - 25), 50, 50]
                    info.notes += (f" color_fallback={fb_kind.value}"
                                   f" area={fb_area} cx={fb_cx:.0f}")
                    return fb_kind, info
            info.state = "not_found"
            return None, info

        if len(best) == 2:
            info.state = "found"
            info.error = "最新K中心带上同时命中多与空, 几何上不可能, 判 UNKNOWN"
            return Signal.UNKNOWN, info

        kind, v = next(iter(best.items()))
        cx, score, x, y, tw, th, fname = v
        info.state = "found"
        info.confidence = score
        info.bbox = [origin_x + x, origin_y + y, tw, th]
        info.notes += f" template={fname} center_x={cx:.0f}"
        return kind, info

    def _color_fallback(self, crop: np.ndarray, origin_x: int,
                        origin_y: int, center_x: float,
                        tol: int) -> Optional[Tuple]:
        """颜色掩码兜底: 在中心带内找橙红(多)或绿(空)色块。

        返回 (Signal, area, cx, cy) 或 None。
        多/空同时存在时返回面积大者; 面积接近时判 UNKNOWN (返回 None, 由上层记 not_found)。
        """
        min_area = self.cfg.detection.color_min_area
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        h, w = crop.shape[:2]
        # 中心带在crop中的x范围 (带内才认)
        band_x1 = max(0, int(center_x - tol - origin_x - 25))
        band_x2 = min(w, int(center_x + tol - origin_x + 25))

        # 橙红: H in [0,30] U [170,180]
        mask_long = cv2.inRange(hsv, np.array([0, 80, 150]),
                                np.array([30, 255, 255]))
        mask_long |= cv2.inRange(hsv, np.array([170, 80, 150]),
                                 np.array([180, 255, 255]))
        # 绿: H in [35, 85]
        mask_short = cv2.inRange(hsv, np.array([35, 80, 100]),
                                 np.array([85, 255, 255]))

        # 只看中心带内的列
        band_long = mask_long[:, band_x1:band_x2]
        band_short = mask_short[:, band_x1:band_x2]

        def _largest_blob(mask):
            """返回最大连通域的 (area, cx, cy) 或 None。"""
            n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
            best = None
            for i in range(1, n):
                a = stats[i, cv2.CC_STAT_AREA]
                if a < min_area:
                    continue
                if best is None or a > best[0]:
                    best = (a, centroids[i][0] + band_x1 + origin_x,
                            centroids[i][1] + origin_y)
            return best

        long_blob = _largest_blob(band_long)
        short_blob = _largest_blob(band_short)

        if long_blob and short_blob:
            # 两者都有, 面积接近(差<2倍) -> 不可靠
            if max(long_blob[0], short_blob[0]) < 2 * min(long_blob[0], short_blob[0]):
                return None
            return (Signal.LONG,) + long_blob if long_blob[0] > short_blob[0] \
                else (Signal.SHORT,) + short_blob
        if long_blob:
            return (Signal.LONG,) + long_blob
        if short_blob:
            return (Signal.SHORT,) + short_blob
        return None

    @staticmethod
    def _peaks(res: np.ndarray, threshold: float, tw: int, th: int,
               max_peaks: int = 6) -> List[Tuple[int, int, float]]:
        """提取匹配结果中所有阈值以上的峰值位置(简易NMS去重)。"""
        ys, xs = np.where(res >= threshold)
        if len(xs) == 0:
            return []
        scores = res[ys, xs]
        order = np.argsort(-scores)
        kept = []
        radius = max(6, min(tw, th) // 2)
        for idx in order:
            x, y = int(xs[idx]), int(ys[idx])
            if all(abs(x - kx) > radius or abs(y - ky) > radius for kx, ky, _ in kept):
                kept.append((x, y, float(scores[idx])))
                if len(kept) >= max_peaks:
                    break
        return kept
