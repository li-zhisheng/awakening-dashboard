# -*- coding: utf-8 -*-
"""E17b: 全屏顶部截图 - 看清面板实际状态。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from PIL import ImageGrab

img = ImageGrab.grab()
print("全屏尺寸:", img.size)
img.crop((0, 0, img.size[0], 120)).save(r"d:\Awakening\logs\panel_top.png")
img.save(r"d:\Awakening\logs\panel_full.png")
print("已保存 logs/panel_top.png (顶部120px) 和 panel_full.png")
