"""手动交易测试工具: 通过easytrader UI管线对模拟账户下指定订单。

用途: 盘中验证下单路径(步骤4类测试)。委托确认弹窗30s超时自动取消,
提交与确认在同一脚本内完成。

用法:
    python trade_manual_test.py --action BUY  --code 601288 --qty 100
    python trade_manual_test.py --action SELL --code 300418 --qty 100 --price 46.60
    price省略=市价委托(需盘中; 模拟账户若不支持市价会返回明确错误)
"""
import argparse
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

SHOT = r"d:\Awakening\logs"


def visible_ctrl(win, ctrl_id, cls):
    cands = [w for w in win.descendants(class_name=cls)
             if w.control_id() == ctrl_id]
    vis = [w for w in cands if w.is_visible()]
    return vis[0] if vis else None


def click_btn(app, keyword):
    top = app.top_window()
    for w in top.descendants(class_name="Button"):
        try:
            t = w.window_text() or ""
            if w.is_visible() and keyword in t:
                w.click()
                return True
        except Exception:
            pass
    return False


def read_texts(app):
    texts = []
    for w in app.top_window().descendants(class_name="Static"):
        try:
            if w.is_visible() and w.window_text().strip():
                texts.append(w.window_text())
        except Exception:
            pass
    return texts


def wait_popup(app, keyword, timeout_s=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        texts = read_texts(app)
        if any(keyword in t for t in texts):
            return texts
        time.sleep(0.4)
    return None


def trade(action: str, code: str, qty: int, price: float):
    import easytrader
    import easytrader.grid_strategies as gs
    user = easytrader.use("universal_client")
    user.grid_strategy = gs.Copy          # Xls策略临时文件链路在本机不可靠
    user.enable_type_keys_for_editor()    # 皮肤控件必须select+type_keys
    user.connect(r"D:\THS\同花顺\xiadan.exe")
    app = user._app
    win = app.top_window()

    page_key, page_f = ("买入", "{F1}") if action == "BUY" else ("卖出", "{F2}")
    win.set_focus()
    time.sleep(0.3)
    win.type_keys(page_f)
    time.sleep(1.5)

    code_e = visible_ctrl(win, 1032, "Edit")
    price_e = visible_ctrl(win, 1033, "Edit")
    amount_e = visible_ctrl(win, 1034, "Edit")
    btn = visible_ctrl(win, 1006, "Button")
    if not code_e:
        print(f"!! {page_key}页控件不可见")
        return

    print(f"[1] 填代码 {code}")
    code_e.click_input(); time.sleep(0.3)
    code_e.select(); code_e.type_keys(code)
    time.sleep(2.0)  # 等名称/盘口/可买卖数量刷新

    if price > 0:
        # 数量先填, 价格最后填(价格框失焦会被客户端回填买一价), 填完立即提交
        print(f"[2] 数量 {qty}")
        amount_e.click_input(); time.sleep(0.3)
        amount_e.select(); amount_e.type_keys(str(qty))
        time.sleep(0.3)
        print(f"[3] 价格 {price} (最后填)")
        price_e.click_input(); time.sleep(0.3)
        price_e.select(); price_e.type_keys(str(price))
        time.sleep(0.3)
    else:
        print(f"[2] 市价: 数量 {qty}")
        amount_e.click_input(); time.sleep(0.3)
        amount_e.select(); amount_e.type_keys(str(qty))
        time.sleep(0.3)

    win.capture_as_image().save(f"{SHOT}\\manual_form.png")
    print("[4] 提交")
    btn.click()

    # 弹窗链: 小数位提示(点是) -> 委托确认(点是) -> 回执(点确定)
    for _ in range(6):
        texts = wait_popup(app, "提示信息", 2.0) or []
        joined = " ".join(texts)
        if "小数部分" in joined or "涨跌停" in joined:
            print("  警告提示 -> 是")
            click_btn(app, "是")
            continue
        conf = wait_popup(app, "委托确认", 2.0)
        if conf:
            print(f"  委托确认: {[t[:50] for t in conf[:4]]}")
            click_btn(app, "是")
            receipt = wait_popup(app, "提示", 8.0)
            if receipt:
                joined = " ".join(receipt)
                print(f"  回执: {joined[:100]}")
                app.top_window().capture_as_image().save(
                    f"{SHOT}\\manual_receipt.png")
                click_btn(app, "确定")
            return
        # 无弹窗则再等
        time.sleep(0.6)
    print("!! 未捕获到委托确认弹窗")
    app.top_window().capture_as_image().save(f"{SHOT}\\manual_stuck.png")


def main():
    ap = argparse.ArgumentParser(description="手动交易测试(模拟账户)")
    ap.add_argument("--action", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--code", required=True, help="6位证券代码")
    ap.add_argument("--qty", type=int, required=True)
    ap.add_argument("--price", type=float, default=0.0,
                    help="0=市价委托(需盘中)")
    a = ap.parse_args()
    trade(a.action, a.code, a.qty, a.price)


if __name__ == "__main__":
    main()
