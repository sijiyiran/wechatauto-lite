"""红包 / 转账卡片解析。

微信把红包和转账都存成 type="文件/链接/卡片"，靠 appmsg 里的
``<type>`` 区分：

    2000 = 微信转账
    2001 = 微信红包

实测的转账 XML（2026-11-05，四季亦然发来的 39.93 元）：

    <appmsg>
      <title><![CDATA[微信转账]]></title>
      <des><![CDATA[收到转账39.93元。如需收钱，请点此升级至最新版本]]></des>
      <type>2000</type>
      <wcpayinfo>
        <paysubtype>3</paysubtype>
        <feedesc><![CDATA[￥39.93]]></feedesc>
        <transcationid><![CDATA[530100...]]></transcationid>
        <transferid><![CDATA[1000050...]]></transferid>
        <invalidtime><![CDATA[1793963928]]></invalidtime>       ← 过期时刻
        <begintransfertime><![CDATA[1793877528]]></begintransfertime>
        <effectivedate><![CDATA[1]]></effectivedate>
        <receiver_username><![CDATA[wxid_mjw5wt0rc46w21]]></receiver_username>
        <payer_username><![CDATA[]]></payer_username>
      </wcpayinfo>
    </appmsg>

**``receiver_username`` 是「这钱转给谁」—— 用户特别强调的：
「收转账要看清是不是转给自己的，不然会报错」**
"""
import re
import time

KIND_TRANSFER = "transfer"
KIND_REDPACKET = "redpacket"

# appmsg 的 type 值
APPMsg_TRANSFER = 2000
APPMsg_REDPACKET = 2001


def _cdata(tag: str, blob: str) -> str:
    """取 <tag><![CDATA[..]]></tag> 或 <tag>..</tag>。"""
    m = re.search(
        r"<" + tag + r">(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</" + tag + r">",
        blob or "", re.S)
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()


def parse_card(content: str) -> dict | None:
    """解析一条红包/转账卡片。不是红包转账返回 None。

    返回:
        {
          "kind": "transfer" | "redpacket",
          "title": 卡片标题,
          "des": 卡片描述,
          "amount": "￥39.93" 或 "",
          "receiver": 收款人 wxid（红包通常为空）,
          "payer": 付款人 wxid,
          "invalid_ts": 过期时间戳（int，0 表示没有）,
          "expired": bool,
          "is_group": 是否来自群聊（由调用方补充）,
        }
    """
    if not content or "<appmsg" not in content:
        return None

    m = re.search(r"<type>(\d+)</type>", content)
    if not m:
        return None
    try:
        atype = int(m.group(1))
    except ValueError:
        return None

    if atype == APPMsg_TRANSFER:
        kind = KIND_TRANSFER
    elif atype == APPMsg_REDPACKET:
        kind = KIND_REDPACKET
    else:
        return None

    amount = _cdata("feedesc", content)
    if not amount:
        # 只写了「微信号」没写金额的情况（没绑卡/旧版）
        d = _cdata("des", content)
        am = re.search(r"([￥¥]\s*[\d.]+)", d)
        amount = am.group(1) if am else ""

    invalid_raw = _cdata("invalidtime", content)
    try:
        invalid_ts = int(invalid_raw) if invalid_raw else 0
    except ValueError:
        invalid_ts = 0

    now = int(time.time())
    expired = bool(invalid_ts and now > invalid_ts)

    return {
        "kind": kind,
        "title": _cdata("title", content),
        "des": _cdata("des", content),
        "amount": amount,
        "receiver": _cdata("receiver_username", content),
        "payer": _cdata("payer_username", content),
        "invalid_ts": invalid_ts,
        "expired": expired,
    }


def is_for_me(card: dict, my_wxid: str) -> tuple[bool, str]:
    """这笔钱是不是给「我」的。返回 (能不能收, 原因)。

    **用户特别强调的规则：收转账要看清是不是转给自己的，不然会报错。**
    所以这里宁可漏收，也不乱点：
      - 转账：receiver_username 必须等于我的 wxid，为空或对不上都不碰
      - 红包：微信的红包卡片一般不带 receiver（群红包谁都能领），
              但如果带了 receiver 就必须是我
      - 已过期的不碰
    """
    if not card:
        return False, "不是红包/转账"

    if card.get("expired"):
        return False, "已过期"

    my = (my_wxid or "").strip()
    if not my:
        return False, "拿不到自己的 wxid"

    receiver = (card.get("receiver") or "").strip()
    payer = (card.get("payer") or "").strip()
    kind = card.get("kind")

    # **付款人是我自己 -> 这是我转出去的，不是收到的**（用户 2026-11-09 反馈）
    if payer and payer == my:
        return False, "是我自己转出去的"

    if kind == KIND_TRANSFER:
        if not receiver:
            # 旧版/信息不全：收款人字段为空，不敢点
            return False, "转账卡片没有收款人字段"
        if receiver != my:
            return False, f"转给别人的（收款人 {receiver[:12]}…）"
        return True, "转给我的"

    if kind == KIND_REDPACKET:
        if receiver and receiver != my:
            return False, f"专属红包不是给我的（{receiver[:12]}…）"
        return True, "红包"

    return False, "未知类型"
