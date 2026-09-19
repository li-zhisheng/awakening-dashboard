"""同花顺"我的自选"云同步 (PC侧, 不占手机)。

接口来源: 同花顺Web自选接口 (ths-favorite 开源项目逆向, 2026年仍可用):
- v2 列表: GET t.10jqka.com.cn/newcircle/group/getSelfStockWithMarket/
- v2 增删: GET t.10jqka.com.cn/newcircle/group/modifySelfStock/?op=add|del&stockcode=代码_市场码
- v1 批量: GET/POST ugc.10jqka.com.cn/optdata/selfstock/open/api/v1/query|modify
           (整表读-改-写, 一次请求完成增+删)

鉴权: 浏览器登录 10jqka.com.cn 后的完整 Cookie (需含 userid/sessionid)。
云同步: 修改"我的自选"后, 登录同一账号的手机App经官方三端云同步自动刷新自选列表。
注意: 只操作"我的自选"(默认自选股分组), 不触碰用户自定义分组。
"""
import logging
import time

import requests

log = logging.getLogger("watchlist")

V2_LIST = "https://t.10jqka.com.cn/newcircle/group/getSelfStockWithMarket/"
V2_MODIFY = "https://t.10jqka.com.cn/newcircle/group/modifySelfStock/"
V1_QUERY = "https://ugc.10jqka.com.cn/optdata/selfstock/open/api/v1/query"
V1_MODIFY = "https://ugc.10jqka.com.cn/optdata/selfstock/open/api/v1/modify"

UA = ("Hexin_Gphone/11.28.03 (Royal Flush) hxtheme/0 innerversion/G037.09.028.1.32 "
      "followPhoneSystemTheme/0 userid/000000000 getHXAPPAccessibilityMode/0 "
      "hxNewFont/1 isVip/0 getHXAPPFontSetting/normal getHXAPPAdaptOldSetting/0 "
      "okhttp/3.14.9")

_AUTH_KEYWORDS = ("登录", "登陆", "未授权", "权限", "失效", "过期", "请先", "auth", "login")


class WatchlistAuthError(RuntimeError):
    """Cookie失效/未登录。"""


class WatchlistError(RuntimeError):
    """其他接口错误。"""


class WatchlistDeleteGuardError(WatchlistError):
    """待删除占现有云自选比例超阈值, 整表替换被护栏中止(防Cookie/账号错误/本地空表清仓)。"""


def parse_cookie(raw: str) -> dict:
    """把浏览器 Cookie 头字符串解析为 dict。"""
    out = {}
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or part.startswith("#") or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def hexin_market(code: str) -> str:
    """6位A股代码 -> 同花顺市场ID (以云端实测为准)。

    云端实测(2026-09-07): 创业板(300/301)归到33, 与深市主板同组;
    仅科创688/689单独18, 北交4/8/920单独71。
    """
    code = str(code).zfill(6)
    if code[:3] in ("688", "689"):
        return "18"   # 科创板
    if code[0] == "6":
        return "17"   # 沪市主板
    if code[:3] in ("300", "301"):
        return "33"   # 创业板 (云端实测归33, 与深市主板同组)
    if code[0] in ("4", "8") or code[:3] == "920":
        return "71"   # 北交所
    if code[:2] in ("51", "56", "58"):
        return "20"   # 沪ETF
    if code[:2] == "15":
        return "36"   # 深ETF
    return "33"       # 深市主板(000/001/002/003)兜底


def _is_auth_msg(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in _AUTH_KEYWORDS)


class CloudWatchlist:
    def __init__(self, cookie: str, timeout: float = 10.0,
                 delete_guard_enable: bool = True,
                 delete_guard_max_pct: float = 0.30,
                 event_log=None):
        self.timeout = timeout
        # 删除护栏(裁定8, 2026-09-15): 待删/现有>阈值则中止一切删除
        # (v1整表替换与v2逐条回退两条路径都不可达)
        self.delete_guard_enable = delete_guard_enable
        self.delete_guard_max_pct = delete_guard_max_pct
        # 两段式二次确认参数由sync()从cfg读取(本类保持轻量, 见下); event_log
        # 由调度器注入用于留痕(2026-09-18采纳)
        self.event_log = event_log
        self.delete_guard_confirm_enable = False
        self.delete_guard_hard_pct = 0.60
        self.delete_guard_confirm_wait = 45.0
        self.delete_guard_confirm_max_diff = 2
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        cookies = parse_cookie(cookie)
        self.s.cookies.update(cookies)
        self.userid = cookies.get("userid", "")
        if not cookies:
            raise WatchlistAuthError("Cookie为空或格式错误")

    # ---------- v2 ----------

    def list_self(self) -> list:
        """返回 [(6位代码, 市场ID), ...], 顺序与云端一致。"""
        try:
            r = self.s.get(V2_LIST, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
        except requests.RequestException as e:
            raise WatchlistError(f"自选列表请求失败: {e}")
        if d.get("errorCode") != 0:
            msg = str(d.get("errorMsg", "未知错误"))
            if _is_auth_msg(msg):
                raise WatchlistAuthError(f"自选列表查询: {msg}")
            raise WatchlistError(f"自选列表查询失败: {msg}")
        items = []
        for e in d.get("result") or []:
            if not isinstance(e, dict) or e.get("code") is None:
                continue
            items.append((str(e["code"]).zfill(6), str(e.get("marketid", ""))))
        return items

    def _modify_v2(self, codes: list, op: str):
        """v2 逐条增删 (回退方案)。op='add'|'del'。"""
        for c in codes:
            try:
                r = self.s.get(V2_MODIFY,
                               params={"op": op,
                                       "stockcode": f"{c}_{hexin_market(c)}"},
                               timeout=self.timeout)
                d = r.json()
            except (requests.RequestException, ValueError) as e:
                log.warning("v2 %s %s 失败: %s", op, c, e)
                continue
            if d.get("errorCode") != 0:
                msg = str(d.get("errorMsg", ""))
                if _is_auth_msg(msg):
                    raise WatchlistAuthError(f"v2 {op}: {msg}")
                log.warning("v2 %s %s 失败: %s", op, c, msg)

    # ---------- v1 (整表替换) ----------

    def _query_v1(self) -> str:
        """返回当前云端版本号 (乐观锁)。"""
        headers = {"userid": self.userid} if self.userid else {}
        try:
            r = self.s.get(V1_QUERY,
                           params={"support_all": "0", "from": "thspc_hevo"},
                           headers=headers, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
        except requests.RequestException as e:
            raise WatchlistError(f"v1查询请求失败: {e}")
        if d.get("status_code") != 0:
            msg = str(d.get("status_msg", ""))
            if _is_auth_msg(msg):
                raise WatchlistAuthError(f"v1查询: {msg}")
            raise WatchlistError(f"v1查询失败: {msg}")
        return str((d.get("data") or {}).get("version", ""))

    def _replace_v1(self, codes: list):
        """整表替换'我的自选'为 codes (一次请求完成增+删)。版本冲突重试1次。"""
        codes = [str(c).zfill(6) for c in codes]
        body = {
            "selfstock": "|".join(codes) + "," + "|".join(hexin_market(c) for c in codes),
            "from": "thspc_hevo",
            "num": str(len(codes)),
        }
        headers = {"userid": self.userid} if self.userid else {}
        for attempt in (1, 2):
            body["version"] = self._query_v1()
            try:
                r = self.s.post(V1_MODIFY, data=body, headers=headers,
                                timeout=self.timeout)
                r.raise_for_status()
                d = r.json()
            except requests.RequestException as e:
                raise WatchlistError(f"v1替换请求失败: {e}")
            if d.get("status_code") == 0:
                return
            msg = str(d.get("status_msg", ""))
            if _is_auth_msg(msg):
                raise WatchlistAuthError(f"v1替换: {msg}")
            if attempt == 2:
                raise WatchlistError(f"v1替换失败: {msg}")
            log.warning("v1替换版本冲突, 重试: %s", msg)
            time.sleep(0.8)

    # ---------- 对外 ----------

    def sync(self, desired_codes: list) -> dict:
        """把'我的自选'整表同步为 desired_codes (6位代码列表, 保序去重)。

        返回 {added, removed, kept, failed_add, failed_del}。
        """
        desired = []
        seen = set()
        for c in desired_codes:
            c = str(c).zfill(6)
            if c not in seen:
                seen.add(c)
                desired.append(c)
        desired_set = set(desired)

        cur = self.list_self()
        cur_set = {c for c, _ in cur}
        added = [c for c in desired if c not in cur_set]
        removed = sorted(cur_set - desired_set)
        if not added and not removed:
            return {"added": [], "removed": [], "kept": len(cur_set),
                    "failed_add": [], "failed_del": []}

        # 删除护栏: 必须在 v1整表替换 / v2逐条删除 之前拦截。
        # 触发场景: Cookie串号读到他人账号、本地标的列表意外为空、接口返回异常
        # 短列表等; 持仓股由调用方(desired=热榜∪持仓)保证不进removed。
        ratio = len(removed) / len(cur_set)
        if (self.delete_guard_enable and cur_set and removed
                and ratio > self.delete_guard_max_pct):
            # 两段式二次确认(2026-09-18采纳): 非硬顶时fresh重拉, 差集收敛则放行
            proceed = False
            if (self.delete_guard_confirm_enable
                    and ratio <= self.delete_guard_hard_pct):
                proceed = self._confirm_delete(desired_set, removed, ratio)
            if not proceed:
                raise WatchlistDeleteGuardError(
                    f"本次待删除{len(removed)}只/现有{len(cur_set)}只 "
                    f"({ratio:.0%} > "
                    f"{self.delete_guard_max_pct:.0%}阈值), 已中止云自选同步"
                    f"(v1/v2两条删除路径均未执行), 待删样例={removed[:10]}")

        try:
            self._replace_v1(desired)
        except WatchlistAuthError:
            raise
        except Exception as e:
            log.warning("v1整表替换失败(%s), 回退v2逐条增删", e)
            self._modify_v2(added, "add")
            self._modify_v2(removed, "del")

        after = {c for c, _ in self.list_self()}
        return {
            "added": added,
            "removed": removed,
            "kept": len(after),
            "failed_add": [c for c in added if c not in after],
            "failed_del": [c for c in removed if c in after],
        }

    def _confirm_delete(self, desired_set: set, removed: list,
                        ratio: float) -> bool:
        """删除护栏二次确认: 等待缓存刷新后fresh重拉, 差集收敛则放行。

        差集<=confirm_max_diff只(接口瞬时抖动)放行; 否则/任何异常都维持中止
        (保守)。持仓股任何情况下不删除(desired由调用方含持仓)。
        """
        try:
            time.sleep(float(self.delete_guard_confirm_wait))
            cur2_set = {c for c, _ in self.list_self()}
            removed2 = sorted(cur2_set - desired_set)
            ratio2 = (len(removed2) / len(cur2_set)) if cur2_set else 0.0
            if self.event_log is not None:
                self.event_log.log(
                    "anomaly", stage="delete_guard_confirm",
                    removed_n=len(removed), ratio=round(ratio, 3),
                    removed2_n=len(removed2), ratio2=round(ratio2, 3))
            if len(removed2) <= int(self.delete_guard_confirm_max_diff):
                log.info("删除护栏二次拉取差集收敛到%d只, 放行同步",
                         len(removed2))
                return True
            log.warning("删除护栏二次拉取差集仍%d只, 维持中止", len(removed2))
            return False
        except Exception as e:
            log.error("删除护栏二次拉取失败, 维持中止: %s", e)
            return False
