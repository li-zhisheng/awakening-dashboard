"""临时脚本: 解析UI dump找搜索入口 (用完即删)。"""
import re
import sys

xml = open(sys.argv[1] if len(sys.argv) > 1 else "d:/Awakening/logs/ui_now.xml",
           encoding="utf-8").read()
nodes = re.findall(r"<node[^>]*/?>", xml)
print("节点数:", len(nodes))
for n in nodes:
    t = re.search(r'text="([^"]*)"', n)
    d = re.search(r'content-desc="([^"]*)"', n)
    rid = re.search(r'resource-id="([^"]*)"', n)
    b = re.search(r'bounds="([^"]*)"', n)
    text = t.group(1) if t else ""
    desc = d.group(1) if d else ""
    res = rid.group(1).split("/")[-1] if rid else ""
    bounds = b.group(1) if b else ""
    if text or desc:
        print(f"text={text!r} desc={desc!r} id={res} {bounds}")
