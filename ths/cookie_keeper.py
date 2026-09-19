"""Cookie 自动保活: 定期访问同花顺需登录页面, 让 sessionid 自动续期。

原理: 同花顺 sessionid 在有活动时会续期 (类似大多数 Web 会话)。
程序每 30 分钟用当前 Cookie 访问一次需登录的用户页面,
- 成功: sessionid 续期, Cookie 持续有效
- 失败: 响铃告警, 提示重新登录复制 Cookie

不需要 Root、不需要 mitmproxy、不需要碰手机, 纯 PC 侧。
"""
import logging
import threading
import time

import requests

from ths.watchlist import V2_LIST, UA, _is_auth_msg

log = logging.getLogger("cookie_keeper")


class CookieKeeper:
    """后台线程: 定期探测 Cookie 有效性, 失效告警。"""

    def __init__(self, cookie_loader, alert_fn, interval: float = 1800.0,
                 timeout: float = 10.0):
        """
        cookie_loader: 无参可调用, 返回当前 Cookie 字符串 (每次读最新文件)
        alert_fn:     单参可 callable, 失效时调用 alert_fn(reason)
        interval:     探测间隔 (秒), 默认 30 分钟
        """
        self._loader = cookie_loader
        self._alert = alert_fn
        self.interval = interval
        self.timeout = timeout
        self._stop = threading.Event()
        self._thread = None
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": UA})

    def _check_once(self) -> bool:
        """用当前 Cookie 探测自选列表接口, 成功返回 True。"""
        cookie = (self._loader() or "").strip()
        if not cookie:
            self._alert("Cookie文件为空, 请登录 10jqka.com.cn 后复制Cookie到 ths_cookie.txt")
            return False
        from ths.watchlist import parse_cookie
        self._s.cookies.update(parse_cookie(cookie))
        try:
            r = self._s.get(V2_LIST, timeout=self.timeout)
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Cookie保活探测异常: %s", e)
            return False
        if d.get("errorCode") != 0:
            msg = str(d.get("errorMsg", ""))
            if _is_auth_msg(msg):
                self._alert(f"同花顺Cookie已失效, 请重新登录 10jqka.com.cn 复制Cookie: {msg}")
                return False
            log.warning("Cookie保活探测异常: %s", msg)
            return False
        n = len(d.get("result") or [])
        log.info("Cookie保活成功: 自选%d只", n)
        return True

    def start(self):
        """启动后台保活线程 (非阻塞)。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cookie_keeper",
                                        daemon=True)
        self._thread.start()
        log.info("Cookie保活已启动, 间隔%.0fs", self.interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._check_once()
            except Exception as e:
                log.error("Cookie保活异常: %s", e)
            self._stop.wait(self.interval)
