"""告警手机推送: 企业微信群机器人/飞书群机器人/钉钉群机器人/Server酱。

配置(config.yaml notify段, 全部留空=不推送, 只写本地alerts.jsonl):
  qywechat_webhook: 企业微信群机器人完整webhook URL(免费无每日条数限制,
                    推荐); 群设置->群机器人->添加->自定义机器人, 安全设置
                    关键词填"告警"
  feishu_webhook:   飞书群自定义机器人完整webhook URL(同上, 关键词"告警")
  dingtalk_webhook: 钉钉群自定义机器人完整webhook URL(关键词"告警")
  serverchan_key:  Server酱SENDKEY(微信服务号, 免费版每天限5条, 备选)

群机器人安全设置若用"自定义关键词", 所有消息必须含该关键词; 本系统告警
标题统一含"告警"二字, 关键词填"告警"即可。多通道全部尝试, 任一成功即可;
网络异常只记日志, 绝不阻塞交易主流程。

告警分级(2026-09-12): CRITICAL=紧急(设备掉线/下单失败/心跳卡死),
WARNING=警告(Cookie失效/挂单未成交), INFO=提醒(盘前自检通过/日报)。
同标题5分钟内去重, 防止心跳卡死/设备反复掉线导致告警风暴。
"""
import logging
import threading
import time

import requests

log = logging.getLogger("notify")

# 告警级别 → 标题前缀
_LEVEL_PREFIX = {
    "CRITICAL": "【紧急】",
    "WARNING":  "【警告】",
    "INFO":     "【提醒】",
}
_COOLDOWN = 300.0   # 同标题去重窗口(秒)


class Notifier:
    def __init__(self, cfg=None):
        n = getattr(cfg, "notify", None)
        self.qywechat_webhook = getattr(n, "qywechat_webhook", "") or ""
        self.feishu_webhook = getattr(n, "feishu_webhook", "") or ""
        self.dingtalk_webhook = getattr(n, "dingtalk_webhook", "") or ""
        self.serverchan_key = getattr(n, "serverchan_key", "") or ""
        self.timeout = getattr(n, "timeout", 8.0) or 8.0
        self._lock = threading.Lock()
        self._sent = {}  # title → last_sent_ts

    def enabled(self) -> bool:
        return bool(self.qywechat_webhook or self.feishu_webhook
                    or self.dingtalk_webhook or self.serverchan_key)

    def send(self, title: str, content: str = "",
             level: str = "WARNING") -> bool:
        """推送一条消息; 无配置返回False, 任一通道成功返回True。

        level: CRITICAL/WARNING/INFO, 自动加前缀并去重(CRITICAL冷却60s,
        其余300s)。同标题冷却期内重复推送被抑制(只记debug日志)。
        """
        if not self.enabled():
            return False
        prefix = _LEVEL_PREFIX.get(level, "【警告】")
        full_title = f"告警{prefix}{title}"
        cooldown = 60.0 if level == "CRITICAL" else _COOLDOWN
        now = time.time()
        with self._lock:
            last = self._sent.get(title)
            if last and now - last < cooldown:
                log.debug("告警去重抑制(%s %.0fs内): %s",
                          level, now - last, title)
                return False
            self._sent[title] = now
            # 清理过期条目(防dict无限增长)
            if len(self._sent) > 100:
                self._sent = {k: v for k, v in self._sent.items()
                              if now - v < _COOLDOWN * 2}
        ok = False
        if self.qywechat_webhook:
            ok = self._qywechat(full_title, content) or ok
        if self.feishu_webhook:
            ok = self._feishu(full_title, content) or ok
        if self.dingtalk_webhook:
            ok = self._dingtalk(full_title, content) or ok
        if self.serverchan_key:
            ok = self._serverchan(full_title, content) or ok
        if ok:
            log.info("告警已推送(%s): %s", level, full_title)
        else:
            log.warning("告警推送全部失败(本地alerts.jsonl仍有记录): %s",
                        full_title)
        return ok

    def _qywechat(self, title: str, content: str) -> bool:
        """企业微信群机器人: 免费无每日条数限制(每分钟20条), 推荐主通道。"""
        try:
            text = f"{title}\n{content}" if content else title
            r = requests.post(self.qywechat_webhook,
                              json={"msgtype": "text",
                                    "text": {"content": text[:2000]}},
                              timeout=self.timeout)
            d = r.json()
            if d.get("errcode") == 0:
                return True
            log.warning("企业微信返回异常: %s", d)
        except Exception as e:
            log.warning("企业微信推送失败: %s", e)
        return False

    def _feishu(self, title: str, content: str) -> bool:
        try:
            text = f"{title}\n{content}" if content else title
            r = requests.post(self.feishu_webhook,
                              json={"msg_type": "text",
                                    "content": {"text": text[:5000]}},
                              timeout=self.timeout)
            d = r.json()
            # 飞书成功: code=0 或 StatusCode=0(不同版本返回字段不同)
            if d.get("code", d.get("StatusCode", -1)) == 0:
                return True
            log.warning("飞书返回异常: %s", d)
        except Exception as e:
            log.warning("飞书推送失败: %s", e)
        return False

    def _dingtalk(self, title: str, content: str) -> bool:
        try:
            text = f"{title}\n{content}" if content else title
            r = requests.post(self.dingtalk_webhook,
                              json={"msgtype": "text",
                                    "text": {"content": text[:5000]}},
                              timeout=self.timeout)
            d = r.json()
            if d.get("errcode") == 0:
                return True
            log.warning("钉钉返回异常: %s", d)
        except Exception as e:
            log.warning("钉钉推送失败: %s", e)
        return False

    def _serverchan(self, title: str, content: str) -> bool:
        try:
            url = f"https://sctapi.ftqq.com/{self.serverchan_key}.send"
            r = requests.post(url, data={"title": title[:32],
                                        "desp": content or title},
                              timeout=self.timeout)
            d = r.json()
            if d.get("code") == 0:
                return True
            log.warning("Server酱返回异常: %s", d)
        except Exception as e:
            log.warning("Server酱推送失败: %s", e)
        return False
