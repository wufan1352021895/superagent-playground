# -*- coding: utf-8 -*-
"""
01. 备货.py  —  TEMU 销售管理每日备货辅助（Python 版）

设计原则（对齐原 PowerShell 备货\\相关文件\\restock\\run-temu-restock-daily.ps1）：
  * 不自己写登录/浏览器底层，统一 import temu_py_common（TemuBrowser / CliApp），
    驱动经过长期验证的 browser-act.exe（会话路由/登录/网络抓包保持一致）
  * Python 只做：编排 + state/JS 结果解析 + 报告/标签
  * 改状态操作（提交备货申请）与源完全一致：
      - 6 个库存类快速筛选逐个扫：热销款库存不足/已缺货/已断货/已断码/即将断码/建议备货
      - 切筛选前先取消蓝色选中卡片；一个 SKC 全程只处理一次
      - 弹窗逐 SKU 按「备货范围 0~N」上限填写，全部解析成功才点确认
      - 成功依据只能是「备货申请提交成功」或行内「今日已备货」
  * 与 bat 并存：
      - 试用(--trial / 菜单2)：产物写 *_py，不发飞书、不实物打印、不跑 enrich，绝不覆盖 bat 产物
      - 正式(--run)：写原生产物路径 备货\\相关文件\\logs\\restock_latest.json，
        成功后与 bat 一致调用 enrich-restock-onsale.ps1（print-restock-label.ps1 已删除）

双击运行：弹出菜单（真实运行 / 试用对比运行 / 退出），跑完停住显示结果。
命令行：
      python "01. 备货.py" --trial        # 试用对比（中控台一键对比用）
      python "01. 备货.py" --run          # 正式执行
      python "01. 备货.py" --run --no-feishu --no-print
"""

import sys, os, re, json, time, base64, subprocess
from datetime import datetime

# --------------------------------------------------------------------------- 路径与常量
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
sys.path.insert(0, DESKTOP)
import temu_py_common as tpc  # 公共库：禁止重造浏览器/登录底层

RESTOCK_ROOT = os.path.join(DESKTOP, "备货")
INTERNAL_DIR = os.path.join(RESTOCK_ROOT, "相关文件")
LOGS_DIR     = os.path.join(INTERNAL_DIR, "logs")
RESTOCK_PS_DIR = os.path.join(INTERNAL_DIR, "restock")

SHOP_NAME = os.environ.get("TEMU_STORE_NAME") or "Wei Qi Measurement"  # 多机迁移 2026-10-12：吴凡机店名 Sewing tools
REPORT_SESSION_NAME = "weiqi-measurement-restock"
AUTOMATION_NAME     = "weiqi-measurement-restock"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/56779969-da03-4b99-89a5-b5b4579f1881"

SALE_URL = tpc.SALES_URL

# --------------------------------------------------------------------------- 回读底座（第2道）
# 第2道「平台回读/业务对账」的适配层与 17 步共用底座。
# 加载失败不算"可以照旧报成功"：explorer 会让 ok 判失败（fail-closed），
# 因为"装不上防线就当自己成功了"正是要根治的毛病。
AI6_STEPS_DIR = os.path.join(os.path.expanduser("~"), "ai6_steps")
if AI6_STEPS_DIR not in sys.path:
    sys.path.insert(0, AI6_STEPS_DIR)

# 第5道 小批量灰度闸：本次最多真提交几条备货申请。0 = 不限（生产默认）。
# 灰度时设 TEMU_RESTOCK_MAX_SUBMIT=1，先只真做 1 条，人工到平台核这条对不对。
SUBMIT_LIMIT = int(os.environ.get("TEMU_RESTOCK_MAX_SUBMIT", "0") or 0)

# --------------------------------------------------------------------------- 幂等防重复（gates17 外加判定，2026-11-06）
# 同店同日同 SKC 已成功提交过的（行内已确认「今日已备货」），补扫/重跑一律跳过，不再重复申请。
# 账本 = gates17/idempotency.py，键 sha256(店|步|操作|业务键|日)，成功确认后才落账。
# 主提交段（JS 批量）为提交后落账审计 + 平台「今日已备货」天然幂等；补扫逐条段为提交前查账硬拦截。
# 账本框架加载失败 → fail-open 照旧跑（gates_hook 判定钩子仍在兜底）。
try:
    _GATES17_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "Temu中控台", "gates17")
    if _GATES17_DIR not in sys.path:
        sys.path.insert(0, _GATES17_DIR)
    from temu_gates import StoreContext as _GatesStoreContext  # noqa: E402
    from idempotency import IdemLedger as _GatesIdemLedger  # noqa: E402
    IDEM = _GatesIdemLedger(_GatesStoreContext("store_local.json"))
except Exception as _e:  # fail-open
    print("[idempotency] 账本不可用(%s)，本次按无账本运行" % _e)
    IDEM = None


def idem_done(skc):
    """只查不记：该 SKC 今日是否已成功提交过。"""
    if IDEM is None:
        return False
    try:
        return IDEM.lookup("1", "restock_submit", str(skc)) is not None
    except Exception:
        return False


def idem_mark(skc, meta=None):
    """成功确认后落账；失败静默（不影响业务），判定钩子仍会记账。"""
    if IDEM is None:
        return
    try:
        IDEM.record("1", "restock_submit", str(skc), meta=meta)
    except Exception as _e:
        print("[idempotency] 落账失败(%s)：skc=%s" % (_e, skc))

# --------------------------------------------------------------------------- step1 可配置操作
# 2026-10-28：阶段开关与快速筛选卡以 Temu中控台/data/step1_ops.json 为准。
DEFAULT_STEP1_STAGES = [
    {"id": "open", "label": "打开 browser-act 会话", "enabled": True},
    {"id": "login", "label": "登录守卫", "enabled": True},
    {"id": "store", "label": "确认目标店铺", "enabled": True},
    {"id": "clear_overlays", "label": "清理遮挡弹窗", "enabled": True},
    {"id": "review", "label": "提交前快速筛选复查", "enabled": True},
    {"id": "submit", "label": "提交备货申请", "enabled": True},
    {"id": "guide_scan", "label": "引导列筛选补扫", "enabled": True},
    {"id": "final_review", "label": "提交后最终复查", "enabled": True},
    {"id": "report", "label": "写报告与快照", "enabled": True},
    {"id": "post", "label": "成功后 enrich/标签", "enabled": True},
]
DEFAULT_STEP1_FILTERS = ["热销款库存不足", "已缺货", "已断货", "已断码", "即将断码", "建议备货"]
STEP1_HOST = (os.environ.get("TEMU_STEP_HOST") or ("wufan" if "吴凡" in os.path.expanduser("~") or os.environ.get("COMPUTERNAME", "").upper() != "DESKTOP-VHV1N0K" else "local")).strip().lower()
STEP1_OPS_PATH = os.environ.get("TEMU_STEP1_OPS_PATH") or os.path.join(DESKTOP, "Temu中控台", "data", "step1_ops.json")


def _default_step1_ops():
    return {
        "stages": [dict(stage) for stage in DEFAULT_STEP1_STAGES],
        "knobs": {"filters": list(DEFAULT_STEP1_FILTERS)},
    }


def _load_step1_ops(path=STEP1_OPS_PATH):
    cfg = _default_step1_ops()
    if not os.path.isfile(path):
        print("[step1-ops] config not found, using built-in defaults: %s" % path)
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("root must be an object")
        hosts = raw.get("hosts") if isinstance(raw.get("hosts"), dict) else {}
        scoped = hosts.get(STEP1_HOST) if isinstance(hosts, dict) else None
        if isinstance(scoped, dict):
            raw = scoped
        elif STEP1_HOST != "local":
            raise ValueError("hosts.%s missing" % STEP1_HOST)
        raw_stages = raw.get("stages")
        if not isinstance(raw_stages, list):
            raise ValueError("stages must be an array")
        by_id = {str(item.get("id")): item for item in raw_stages if isinstance(item, dict)}
        for stage in cfg["stages"]:
            item = by_id.get(stage["id"])
            if item is not None and isinstance(item.get("enabled"), bool):
                stage["enabled"] = item["enabled"]
        raw_filters = raw.get("knobs", {}).get("filters") if isinstance(raw.get("knobs"), dict) else None
        if isinstance(raw_filters, list):
            filters = [str(item).strip() for item in raw_filters if str(item).strip()]
            if filters:
                cfg["knobs"]["filters"] = filters
            else:
                print("[step1-ops] WARN empty filters, using built-in defaults")
        else:
            print("[step1-ops] WARN filters missing/invalid, using built-in defaults")
    except Exception as e:
        print("[step1-ops] WARN load failed, using built-in defaults: %s" % e)
        return _default_step1_ops()
    disabled = [s["id"] for s in cfg["stages"] if not s["enabled"]]
    print("[step1-ops] loaded %s host=%s; filters=%s; disabled=%s" % (
        path, STEP1_HOST, json.dumps(cfg["knobs"]["filters"], ensure_ascii=False),
        json.dumps(disabled, ensure_ascii=False)))
    return cfg


STEP1_OPS = _load_step1_ops()
FILTERS = list(STEP1_OPS["knobs"]["filters"])
STEP1_STAGE_ENABLED = {stage["id"]: bool(stage["enabled"]) for stage in STEP1_OPS["stages"]}
STEP1_STAGE_LABELS = {stage["id"]: stage["label"] for stage in STEP1_OPS["stages"]}
# 已知但非备货类卡片：存在即正常，不报警（页面共 8 卡）
KNOWN_NON_STOCK_CARDS = ["站外广告曝光中", "建议生产/采购"]


# =========================================================================== 页面 JS（忠实摘自源脚本）
LOGIN_GUARD_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const clickLikeHuman = el => {
    if (!el) return false;
    el.scrollIntoView?.({ block: 'center', inline: 'center' });
    const reactPropsKey = Object.keys(el).find(k => k.startsWith('__reactProps'));
    const reactOnClick = reactPropsKey && el[reactPropsKey] && el[reactPropsKey].onClick;
    if (typeof reactOnClick === 'function') {
      reactOnClick({ preventDefault(){}, stopPropagation(){}, target: el, currentTarget: el, nativeEvent: {} });
      return true;
    }
    el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    el.click?.();
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return true;
  };
  const candidates = () => [...document.querySelectorAll('button,a,div,span,li,[role=button]')].filter(visible);
  const shortest = items => items.sort((a, b) => norm(a.innerText || a.textContent).length - norm(b.innerText || b.textContent).length)[0];
  const findExact = target => shortest(candidates().filter(el => norm(el.innerText || el.textContent) === target));
  const findText = re => shortest(candidates().filter(el => re.test(norm(el.innerText || el.textContent))));
  const text = norm(document.body.innerText);
  const href = location.href;
  const onAuthPage = href.includes('/auth/authentication');
  const onSellerLoginPage = href.includes('seller.kuajingmaihuo.com/login') || /扫码登录|手机号登录|邮箱登录|扫描二维码|卖家中心APP/.test(text);
  const hasPasswordInput = !!document.querySelector('input[type=password]');
  const hasCaptcha = /滑块|captcha|安全验证|人机验证|请完成验证|拖动滑块/i.test(text);
  const saleReady = href.includes('/stock/fully-mgt/sale-manage') && /热销款库存不足|已缺货|已断货|已断码|即将断码|建议备货|销售管理|申请备货/.test(text);

  if (saleReady) {
    return { ok: true, stage: 'sale-management-ready', url: href, title: document.title };
  }

  if (href.includes('/stock/fully-mgt/sale-manage')) {
    return { ok: false, stage: 'sale-management-loading', url: href, title: document.title };
  }

  if (!onSellerLoginPage && !hasPasswordInput && !hasCaptcha && href.includes('seller.kuajingmaihuo.com')) {
    const authModal = [...document.querySelectorAll('[class*=MDL_container], [role=dialog]')]
      .find(el => visible(el) && /即将前往 Seller Central|确认授权并前往/.test(norm(el.innerText || el.textContent)));
    if (authModal) {
      const checkbox = [...authModal.querySelectorAll('label')].find(visible);
      if (checkbox) clickLikeHuman(checkbox);
      const authButton = [...authModal.querySelectorAll('button')]
        .find(el => visible(el) && /确认授权并前往/.test(norm(el.innerText || el.textContent)));
      if (authButton) {
        clickLikeHuman(authButton);
        await sleep(1000);
        return { ok: false, stage: 'clicked-seller-central-auth-confirm', url: href, title: document.title };
      }
    }

    const saleManageEntry = [...document.querySelectorAll('a,button,div,span,li,[role=button]')]
      .filter(visible)
      .filter(el => (el.href || el.getAttribute('href') || '').includes('/stock/fully-mgt/sale-manage/main') || norm(el.innerText || el.textContent) === '销售管理')
      .sort((a, b) => {
        const ah = (a.href || a.getAttribute('href') || '').includes('/stock/fully-mgt/sale-manage/main') ? 0 : 1;
        const bh = (b.href || b.getAttribute('href') || '').includes('/stock/fully-mgt/sale-manage/main') ? 0 : 1;
        return ah - bh || a.getBoundingClientRect().top - b.getBoundingClientRect().top;
      })[0] || findText(/^销售管理$/);
    if (saleManageEntry) {
      clickLikeHuman(saleManageEntry);
      await sleep(1000);
      return { ok: false, stage: 'clicked-seller-sale-management-entry', url: href, title: document.title };
    }
    location.href = 'https://agentseller.temu.com/stock/fully-mgt/sale-manage/main';
    return { ok: false, stage: 'force-seller-sale-management-url', url: href, title: document.title };
  }

  if (!onAuthPage && !onSellerLoginPage && !hasPasswordInput && !hasCaptcha && href.includes('agentseller.temu.com')) {
    location.href = 'https://agentseller.temu.com/stock/fully-mgt/sale-manage/main';
    return { ok: false, stage: 'force-sale-management-after-login', url: href, title: document.title };
  }

  const authReturnFromSeller = /[?&](uniqueId|asCode)=/.test(href);
  const merchantCenter = [...document.querySelectorAll('.authentication_goto__bprbG, [class*=authentication_regionItem], a,button,div,span')]
    .filter(visible)
    .sort((a, b) => {
      const ag = String(a.className || '').includes('authentication_goto') ? 0 : 1;
      const bg = String(b.className || '').includes('authentication_goto') ? 0 : 1;
      return ag - bg || norm(a.innerText || a.textContent).length - norm(b.innerText || b.textContent).length;
    })
    .find(el => {
      const itemText = norm(el.innerText || el.textContent);
      const cls = String(el.className || '');
      return cls.includes('authentication_goto') || itemText === '中国地区 商家中心' || itemText === '商家中心';
    });
  if (onAuthPage && authReturnFromSeller && merchantCenter) {
    clickLikeHuman(merchantCenter);
    await sleep(1200);
    return { ok: false, stage: 'clicked-auth-merchant-center-react', url: href, title: document.title };
  }

  if (!onAuthPage && /Agent Center/i.test(document.title + ' ' + text)) {
    const topMerchantCenter = findExact('商家中心') || findText(/^商家中心$/);
    if (topMerchantCenter) {
      clickLikeHuman(topMerchantCenter);
      await sleep(800);
      return { ok: false, stage: 'clicked-top-merchant-center', url: href, title: document.title };
    }
  }

  if (onAuthPage) {
    const globalSelect = document.querySelector('input[value="全球"]');
    if (globalSelect && !/扫码登录|手机号登录|邮箱登录/.test(text)) {
      const selectWrapper = globalSelect.closest('[class*=ST_outerWrapper]') || globalSelect.closest('[class*=ST_head]') || globalSelect.closest('[class*=IPT_outerWrapper]') || globalSelect;
      selectWrapper.click();
      let merchantOption = null;
      for (let i = 0; i < 20; i++) {
        await sleep(250);
        merchantOption = [...document.querySelectorAll('[class*=ST_dropdown] [class*=cIL_item], [class*=PP_dropdown] [class*=cIL_item], [class*=ST_itemRendererLabel], li[role=option]')]
          .filter(el => el.offsetParent !== null)
          .find(el => norm(el.innerText || el.textContent) === '商家中心');
        if (merchantOption) break;
      }
      if (merchantOption) {
        clickLikeHuman(merchantOption);
        return { ok: false, stage: 'selected-merchant-center-site', url: href, title: document.title };
      }
    }

    const merchantCenter2 = [...document.querySelectorAll('.authentication_goto__bprbG, a,button,div,span')]
      .filter(visible)
      .find(el => {
        const itemText = norm(el.innerText || el.textContent);
        const cls = String(el.className || '');
        return cls.includes('authentication_goto') || itemText === '商家中心';
      });
    if (authReturnFromSeller && merchantCenter2) {
      clickLikeHuman(merchantCenter2);
      return { ok: false, stage: 'clicked-merchant-center', url: href, title: document.title };
    }
  }

  if (onSellerLoginPage) {
    const agreeLogin = findText(/确认登录|同意并登录/);
    if (agreeLogin) {
      clickLikeHuman(agreeLogin);
      return { ok: false, stage: 'clicked-confirm-login', url: href, title: document.title };
    }

    const phoneTab = findExact('手机号登录');
    const phoneTabClass = phoneTab ? String(phoneTab.className || '') : '';
    const phoneTabSelected = /Checked|active|selected|current/i.test(phoneTabClass) || (/手机号登录/.test(text) && /请输入手机号|手机号码|手机号/.test(text) && !/扫码登录/.test(norm(phoneTab?.parentElement?.innerText || '')));
    if (phoneTab && !phoneTabSelected) {
      clickLikeHuman(phoneTab);
      return { ok: false, stage: 'selected-phone-login', url: href, title: document.title };
    }

    const submitLogin = findExact('登录') || shortest([...document.querySelectorAll('button')].filter(visible).filter(el => /^登录$/.test(norm(el.innerText || el.textContent))));
    if (submitLogin) {
      clickLikeHuman(submitLogin);
      return { ok: false, stage: 'clicked-login-button', url: href, title: document.title };
    }

    if (hasCaptcha || /验证码|短信验证码|请输入验证码|获取验证码|二维码|扫码/.test(text)) {
      return { ok: false, stage: 'manual-login-required', method: 'verification-required', url: href, title: document.title, hasPasswordInput, hasCaptcha, text: text.slice(0, 220) };
    }

    return { ok: false, stage: 'seller-login-waiting-for-auto-advance', url: href, title: document.title, hasPasswordInput, hasCaptcha, text: text.slice(0, 220) };
  }

  const loginButton = [...document.querySelectorAll('a,button,div,span')]
    .filter(el => el.offsetParent !== null)
    .find(el => /^登录$|确认登录|同意并登录|登入|Sign in|Log in/i.test(norm(el.innerText || el.textContent)));
  if (loginButton) {
    clickLikeHuman(loginButton);
    return { ok: false, stage: 'clicked-generic-login', url: href, title: document.title };
  }

  const loggedOut = href.includes('/login') || hasPasswordInput || !!loginButton;
  if (loggedOut || hasCaptcha) {
    return { ok: false, stage: 'manual-login-required', url: href, title: document.title, hasPasswordInput, hasCaptcha, text: text.slice(0, 220) };
  }

  location.href = 'https://agentseller.temu.com/stock/fully-mgt/sale-manage/main';
  return { ok: false, stage: 'navigate-sale-management', url: href, title: document.title };
})()
"""

CLEAR_OVERLAYS_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const clicked = [];

  const closeModalIcon = document.querySelector('[class*=MDL_headerCloseIcon]');
  if (closeModalIcon) {
    closeModalIcon.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    clicked.push('modal close icon');
  }

  const feedbackClose = document.querySelector('.feedBack-close');
  if (feedbackClose) {
    feedbackClose.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    clicked.push('feedback close');
  }

  const knownButton = [...document.querySelectorAll('button,a,div')]
    .find(el => /我知道了|关闭|取消/.test(norm(el.innerText)) && el.offsetParent !== null);
  if (knownButton) {
    knownButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    clicked.push(norm(knownButton.innerText).slice(0, 20));
  }

  const messagePanel = document.querySelector('[class*=new-bell_list]');
  if (messagePanel) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    const msg = [...document.querySelectorAll('[class*=header-icon_commonIcon], [class*=Badge_container]')]
      .find(el => /消息/.test(norm(el.innerText)));
    if (msg) msg.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    clicked.push('message panel');
  }

  return { clicked };
})()
"""

ENSURE_STORE_JS_TPL = r"""
(() => {
  const targetName = TARGET_STORE_PLACEHOLDER;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const storeEl = document.querySelector('[class*=account-info_mallInfo] [class*=elli_outerWrapper], [class*=account-info_mallInfo]');
  const current = norm(storeEl && storeEl.innerText);
  if (current.includes(targetName)) {
    return { ok: true, changed: false, currentStore: current, reason: 'already on target store' };
  }

  let candidates = Array.from(document.querySelectorAll('button,a,div,span,li')).filter(visible);
  const openers = candidates.filter(el => {
    const text = norm(el.innerText || el.textContent);
    const cls = String(el.className || '');
    return text.includes(current) || /account-info|mallInfo|store|shop|switch/i.test(cls) || /店铺|切换/.test(text);
  });
  for (const opener of openers.slice(0, 12)) {
    opener.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    opener.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  }

  candidates = Array.from(document.querySelectorAll('button,a,div,span,li')).filter(visible);
  const option = candidates
    .filter(el => norm(el.innerText || el.textContent).includes(targetName))
    .sort((a, b) => norm(a.innerText).length - norm(b.innerText).length)[0];
  if (!option) {
    return { ok: false, changed: false, currentStore: current, reason: 'target store option not found: ' + targetName };
  }

  option.scrollIntoView({ block: 'center', inline: 'center' });
  option.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  return { ok: true, changed: true, previousStore: current, selectedStore: targetName };
})()
"""

DETECT_STORE_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const el = document.querySelector('[class*=account-info_mallInfo] [class*=elli_outerWrapper], [class*=account-info_mallInfo]');
  return norm(el?.innerText) || '';
})()
"""

# 复查 JS（只读）：逐卡片取消选中→点目标→最大每页条数→翻页收集行→need/skip 分类 + 未知卡片告警
REVIEW_JS_TPL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const filters = [ACTIVE_FILTER_PLACEHOLDER];
  const allFilters = FILTERS_PLACEHOLDER;
  // 2026-10-08：按用户要求补入「库存安全/可备货」这一档（建议备货引导列三档之一）。
  const needRe = /库存安全|可备货|库存告急|库存偏低|建议备货|建议立刻备货|已断货建议备货|已缺货建议备货/;
  // skipRe 里的「库存充足」与「库存安全」语义相邻；若整行同时命中两者，
  // need&&!skip 会把它重新排除掉，导致新增档位形同虚设。故让「库存安全/可备货」优先，命中则不 skip。
  const skipRe = /无需备货|暂无需申请备货|今日已备货|库存充足/;
  const needReWins = /库存安全|可备货/;
  const knownNonStockCards = KNOWN_NON_STOCK_PLACEHOLDER;
  const reports = [];

  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const numOf = s => { const m = String(s || '').match(/(\d+)/); return m ? +m[1] : NaN; };

  const hasNextPage = () => {
    const next = document.querySelector('li[class*=PGT_next_]');
    if (!next) return false;
    if (/disabled/.test(next.className || '')) return false;
    if (!visible(next)) return false;
    return true;
  };

  const clickNextPage = async () => {
    const next = document.querySelector('li[class*=PGT_next_]');
    if (!next) return false;
    next.click();
    await sleep(2500);
    return true;
  };

  const setMaxPageSize = async () => {
    const sizer = document.querySelector('li[class*=PGT_sizeChanger_]') ||
                  document.querySelector('[class*=pagination] [class*=size]');
    if (!sizer) return { ok: false, reason: '没找到每页大小控件' };
    sizer.click();
    await sleep(600);
    const opts = [...document.querySelectorAll('li[class*=cIL_item_], [class*=select-item], [role=option]')]
      .filter(visible).filter(o => !isNaN(numOf(o.textContent)));
    if (!opts.length) {
      document.body.click();
      await sleep(300);
      return { ok: false, reason: '展开后没找到数字选项' };
    }
    const best = opts.reduce((a, b) => numOf(b.textContent) > numOf(a.textContent) ? b : a);
    const size = numOf(best.textContent);
    best.click();
    await sleep(1500);
    return { ok: true, size };
  };

  const cardSel = '[class*="icon-card_card"]';
  const isSel = el => /rgb\(64,\s*124,\s*255\)|#407cff/i.test(el.getAttribute('style') || '')
    || /active|selected|checked|current/i.test(el.className || '');
  const cardName = el => norm(el.innerText).split(/\s+/)[0] || norm(el.innerText);

  for (const name of filters) {
    // 清卡：真点→回读该卡是否仍选中；仍选中重试（≤2次），到上限仍选中→该 filter 记复查异常（不产出伪总数）
    let cards = [...document.querySelectorAll(cardSel)];
    let clearError = null;
    for (const card of cards) {
      if (!isSel(card)) continue;
      const label = cardName(card);
      let cleared = false;
      for (let attempt = 0; attempt <= 2; attempt++) {
        card.scrollIntoView({ block: 'center' });
        card.click();
        await sleep(700);
        const after = [...document.querySelectorAll(cardSel)].find(el => cardName(el) === label);
        if (!after || !isSel(after)) { cleared = true; break; }
      }
      if (!cleared) { clearError = '复查清卡失败：卡「' + label + '」重试后回读仍选中'; break; }
    }
    if (clearError) {
      reports.push({ filter: name, error: clearError });
      continue;
    }

    // 选卡：真点→回读该卡确为选中态（style/class）；未选中重试（≤2次），仍不成→该 filter 记复查异常（不产出伪总数）
    cards = [...document.querySelectorAll(cardSel)];
    const target = cards.find(el => norm(el.innerText).startsWith(name));
    if (!target) {
      reports.push({ filter: name, error: 'filter not found' });
      continue;
    }

    target.scrollIntoView({ block: 'center' });
    let selectedOk = false;
    let lastRead = '卡不存在';
    for (let attempt = 0; attempt <= 2; attempt++) {
      target.click();
      await sleep(3500);
      const after = [...document.querySelectorAll(cardSel)].find(el => norm(el.innerText).startsWith(name));
      lastRead = after ? (isSel(after) ? '选中' : '未选中') : '卡不存在';
      if (after && isSel(after)) { selectedOk = true; break; }
    }
    if (!selectedOk) {
      reports.push({ filter: name, error: '复查选卡失败：卡「' + name + '」重试后回读仍' + lastRead });
      continue;
    }

    const sizeResult = await setMaxPageSize();

    const allRows = [];
    const seenSkcs = new Set();
    let pageNum = 1;
    let totalMatch = null;
    do {
      const pageText = norm(document.querySelector('#page_container_id')?.innerText || document.body.innerText);
      if (!totalMatch) totalMatch = pageText.match(/共有\s*(\d+)\s*条/);
      const rows = [...document.querySelectorAll('tbody tr')]
        .map((tr, i) => ({ i, text: norm(tr.innerText) }))
        .filter(r => r.text && !/^合计\b/.test(r.text));
      for (const r of rows) {
        const skc = r.text.match(/SKC：\s*(\d+)/)?.[1];
        if (skc && seenSkcs.has(skc)) continue;
        if (skc) seenSkcs.add(skc);
        allRows.push(r);
      }
      if (!hasNextPage()) break;
      if (pageNum >= 300) break;
      await clickNextPage();
      pageNum++;
    } while (true);

    // SKU 子行无 SKC 编号，继承本页上一条父行 SKC
    let revLastSkc = '';
    const items = allRows.map(r => {
      let skc = r.text.match(/SKC：\s*(\d+)/)?.[1] || '';
      const ownSkc = !!skc;
      if (!skc) { skc = revLastSkc; }
      if (ownSkc) { revLastSkc = skc; }
      const sku = r.text.match(/SKU ID：\s*(\d+)/)?.[1] || '';
      const need = needRe.test(r.text);
      const skip = skipRe.test(r.text) && !needReWins.test(r.text);
      return { skc, sku, need, skip, snippet: r.text.slice(0, 260) };
    });

    reports.push({
      filter: name,
      pageSizeOk: sizeResult.ok,
      pageSize: sizeResult.size || null,
      pages: pageNum,
      total: totalMatch ? Number(totalMatch[1]) : null,
      unhandledAll: items.filter(x => x.need && !x.skip).length,
      items
    });
  }

  const cardLabel = el => norm(el.innerText).split(/\s+/)[0] || norm(el.innerText);
  const cardCount = el => { const m = norm(el.innerText).match(/(\d+)/); return m ? Number(m[1]) : null; };
  const allCardNames = [...document.querySelectorAll('[class*="icon-card_card"]')].map(cardLabel);
  const unknownCards = [...new Set(allCardNames)]
    .filter(n => n && !allFilters.includes(n) && !knownNonStockCards.includes(n))
    .map(n => ({ name: n, count: cardCount([...document.querySelectorAll('[class*="icon-card_card"]')].find(el => cardLabel(el) === n)) }));

  const json = JSON.stringify({ reports, unknownCards });
  return btoa(unescape(encodeURIComponent(json)));
})()
"""

# 提交 JS：逐卡片翻页，对 need&&!skip 的 SKC（全程去重）走申请备货弹窗，逐 SKU 填范围上限后确认
SUBMIT_JS_TPL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const filters = FILTERS_PLACEHOLDER;
  const dryRun = DRY_RUN_PLACEHOLDER;
  const needRe = /库存安全|可备货|库存告急|库存偏低|建议备货|建议立刻备货|已断货建议备货|已缺货建议备货/;
  const skipRe = /无需备货|暂无需申请备货|今日已备货|库存充足/;
  // 同 REVIEW_JS_TPL：新增档位需优先于「库存充足」，否则 need&&!skip 会把它重新排除。
  const needReWins = /库存安全|可备货/;
  const platformRejectRe = /平台拒绝|申请被拒绝|拒绝备货|拒绝[^。！？\n]{0,30}备货|备货申请[^。！？\n]{0,30}拒绝|商品处于清退中，不可创建备货单|不可创建备货单|不支持申请备货|无法申请备货|不能申请备货|不允许申请备货|创建备货单失败|申请失败/;
  const submitted = [];
  const skipped = [];
  const failed = [];
  const planned = [];

  // 清空快速筛选卡：真点→回读确认已取消选中→清不干净必须抛错（禁止只警告继续成功）
  const clearSelectedFilters = async () => {
    const cardSel = '[class*="icon-card_card"]';
    const isSel = el => /rgb\(64,\s*124,\s*255\)|#407cff/i.test(el.getAttribute('style') || '')
      || /active|selected|checked|current/i.test(el.className || '');
    const cardName = el => norm(el.innerText).split(/\s+/)[0] || '?';
    if (!document.querySelector(cardSel)) {
      throw new Error('快速筛选卡清空失败：卡片区不存在 (not-found)');
    }
    for (let round = 1; round <= 8; round++) {
      const on = [...document.querySelectorAll(cardSel)].filter(isSel);
      if (!on.length) return; // 回读确认：当前无任何选中的快速筛选卡（本轮点0次）
      on[0].scrollIntoView({ block: 'center' });
      on[0].click();
      await sleep(700);
    }
    const left = [...document.querySelectorAll(cardSel)].filter(isSel)
      .map(cardName).filter(Boolean);
    throw new Error('快速筛选卡清空失败：真点重试 8 轮仍选中 ' + left.join('/') + '（清卡未生效）');
  };

  const closeCurrentModal = () => {
    const modal = [...document.querySelectorAll('[class*=MDL_outerWrapper], [role=dialog]')].find(el => visible(el));
    if (!modal) return false;
    const close = modal.querySelector('[class*=MDL_headerCloseIcon]') || [...modal.querySelectorAll('button')].find(el => visible(el) && /取消|关闭|我知道了/.test(norm(el.innerText)));
    if (close) {
      close.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      return true;
    }
    return false;
  };

  const setInputValue = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const extractPlatformRejectReason = text => {
    const match = norm(text).match(platformRejectRe);
    return match ? match[0] : '';
  };

  const numOf = s => { const m = String(s || '').match(/(\d+)/); return m ? +m[1] : NaN; };

  const hasNextPage = () => {
    const next = document.querySelector('li[class*=PGT_next_]');
    if (!next) return false;
    if (/disabled/.test(next.className || '')) return false;
    if (!visible(next)) return false;
    return true;
  };

  const clickNextPage = async () => {
    const next = document.querySelector('li[class*=PGT_next_]');
    if (!next) return false;
    next.click();
    await sleep(2500);
    return true;
  };

  const setMaxPageSize = async () => {
    const sizer = document.querySelector('li[class*=PGT_sizeChanger_]') ||
                  document.querySelector('[class*=pagination] [class*=size]');
    if (!sizer) return { ok: false, reason: '没找到每页大小控件' };
    sizer.click();
    await sleep(600);
    const opts = [...document.querySelectorAll('li[class*=cIL_item_], [class*=select-item], [role=option]')]
      .filter(visible).filter(o => !isNaN(numOf(o.textContent)));
    if (!opts.length) {
      document.body.click();
      await sleep(300);
      return { ok: false, reason: '展开后没找到数字选项' };
    }
    const best = opts.reduce((a, b) => numOf(b.textContent) > numOf(a.textContent) ? b : a);
    const size = numOf(best.textContent);
    best.click();
    await sleep(1500);
    return { ok: true, size };
  };

  // [gray-scale] 灰度闸：最多真提交几条。0=不限（生产默认）。
  // 第5道"小批量灰度+真人首件"用：先只真做 1 条，人工到平台核这条对不对，再放开批量。
  const submitLimit = SUBMIT_LIMIT_PLACEHOLDER;
  const limitReached = () => submitLimit > 0 && submitted.length >= submitLimit;

  for (const filterName of filters) {
    if (limitReached()) break;
    await clearSelectedFilters();
    let cards = [...document.querySelectorAll('[class*="icon-card_card"]')];
    const target = cards.find(el => norm(el.innerText).startsWith(filterName));
    if (!target) {
      failed.push({ filter: filterName, skc: '', sku: '', reason: 'filter not found' });
      continue;
    }
    target.scrollIntoView({ block: 'center' });
    target.click();
    await sleep(3500);

    await setMaxPageSize();

    const processedSkcs = new Set();
    let pageNum = 1;
    do {
      if (limitReached()) break;
      const rows = [...document.querySelectorAll('tbody tr')]
        .filter(tr => norm(tr.innerText) && !/^合计\b/.test(norm(tr.innerText)));
      // 缺 SKC 的 SKU 子行继承本页上一条父行 SKC；一个 SKC 只处理一次
      let lastRowSkc = '';
      const targets = rows.map((row, index) => {
        const text = norm(row.innerText);
        let skc = text.match(/SKC：\s*(\d+)/)?.[1] || '';
        const ownSkc = !!skc;
        if (!skc) { skc = lastRowSkc; }
        if (ownSkc) { lastRowSkc = skc; }
        return {
          index,
          skc,
          ownSkc,
          sku: text.match(/SKU ID：\s*(\d+)/)?.[1] || '',
          code: text.match(/SKC货号：\s*(\d+)/)?.[1] || '',
          need: needRe.test(text),
          skip: skipRe.test(text) && !needReWins.test(text),
          text
        };
      }).filter(item => item.need && !item.skip && item.skc && !processedSkcs.has(item.skc));

      for (const item of targets) {
        if (limitReached()) break;
        processedSkcs.add(item.skc);
      const row = [...document.querySelectorAll('tbody tr')].find(tr => norm(tr.innerText).includes(`SKC：${item.skc}`));
      const action = row && [...row.querySelectorAll('button,a,span,div')].find(el => visible(el) && norm(el.innerText || el.textContent) === '申请备货');
      if (!action) {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: '申请备货入口未找到' });
        continue;
      }

      action.scrollIntoView({ block: 'center' });
      action.click();
      await sleep(3000);

      let modal = [...document.querySelectorAll('[class*=MDL_outerWrapper], [role=dialog]')]
        .find(el => visible(el) && norm(el.innerText).includes('申请备货'));
      if (!modal) {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: '申请备货弹窗未出现' });
        continue;
      }

      let modalText = norm(modal.innerText);
      if (/暂无需申请备货|无需备货|今日已备货/.test(modalText)) {
        skipped.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: modalText.match(/暂无需申请备货|无需备货|今日已备货/)?.[0] || '弹窗显示无需处理' });
        closeCurrentModal();
        await sleep(1200);
        continue;
      }
      let rejectReason = extractPlatformRejectReason(modalText);
      if (rejectReason) {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: `平台拒绝备货申请：${rejectReason}` });
        closeCurrentModal();
        await sleep(1200);
        continue;
      }

      const continueButton = [...modal.querySelectorAll('button,a,span,div')]
        .filter(el => visible(el) && norm(el.innerText || el.textContent) === '继续申请备货')
        .sort((a, b) => ({ BUTTON: 0, A: 1, SPAN: 2, DIV: 3 }[a.tagName] ?? 9) - ({ BUTTON: 0, A: 1, SPAN: 2, DIV: 3 }[b.tagName] ?? 9))[0];
      if (continueButton) {
        continueButton.scrollIntoView({ block: 'center' });
        continueButton.focus?.();
        continueButton.click();
        for (let waitIndex = 0; waitIndex < 20; waitIndex++) {
          await sleep(500);
          modal = [...document.querySelectorAll('[class*=MDL_outerWrapper], [role=dialog]')]
            .find(el => visible(el) && norm(el.innerText).includes('申请备货')) || modal;
          if ([...modal.querySelectorAll('input')].some(el => visible(el) && /备货范围\s*0\s*~\s*\d+/.test(el.placeholder || ''))) break;
        }
        modalText = norm(modal.innerText);
        rejectReason = extractPlatformRejectReason(modalText);
        if (rejectReason) {
          failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: `平台拒绝备货申请：${rejectReason}` });
          closeCurrentModal();
          await sleep(1200);
          continue;
        }
      }

      // 弹窗内每个 SKU 一行、各带一个「备货范围 0~N」输入框，逐个按上限填写
      let rangeInputs = [...modal.querySelectorAll('input')].filter(el => visible(el) && /备货范围\s*0\s*~\s*\d+/.test(el.placeholder || ''));
      if (!rangeInputs.length) {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: '备货范围输入框未找到' });
        closeCurrentModal();
        await sleep(1200);
        continue;
      }
      const findSkuForInput = inp => {
        let node = inp;
        for (let i = 0; i < 8 && node; i++) {
          node = node.parentElement;
          if (!node) break;
          const t = norm(node.innerText);
          if (t.length >= 600) continue;
          const m = t.match(/SKU ID：\s*(\d+)/);
          if (m) return m[1];
        }
        return '';
      };
      const filled = [];
      const fillOne = rangeInput => {
        const max = rangeInput.placeholder.match(/备货范围\s*0\s*~\s*(\d+)/)?.[1];
        if (!max) return false;
        try { rangeInput.scrollIntoView({ block: 'center' }); } catch {}
        rangeInput.focus();
        setInputValue(rangeInput, max);
        filled.push({ sku: findSkuForInput(rangeInput), max: +max });
        return true;
      };
      for (const rangeInput of rangeInputs) { fillOne(rangeInput); await sleep(250); }
      await sleep(500);
      // 二次确认：懒渲染出现的新输入框补齐
      const extraInputs = [...modal.querySelectorAll('input')]
        .filter(el => visible(el) && /备货范围\s*0\s*~\s*\d+/.test(el.placeholder || '') && !rangeInputs.includes(el));
      for (const rangeInput of extraInputs) {
        if (fillOne(rangeInput)) { rangeInputs.push(rangeInput); }
        await sleep(250);
      }
      if (filled.length !== rangeInputs.length) {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: `部分SKU备货范围无法解析（已填 ${filled.length}/${rangeInputs.length}），为防漏填已放弃本次提交` });
        closeCurrentModal();
        await sleep(1200);
        continue;
      }
      const max = String(filled.reduce((m, f) => Math.max(m, f.max), 0));
      const confirm = [...modal.querySelectorAll('button')].find(el => visible(el) && norm(el.innerText) === '确认');
      if (!confirm) {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: '确认按钮未找到' });
        continue;
      }
      if (dryRun) {
        // 干跑：数量已按上限填好，但绝不点「确认」，只登记本会提交的内容并安全关弹窗
        planned.push({ filter: filterName, skc: item.skc, sku: item.sku, code: item.code, range: `0~${max}`, quantity: max, skuCount: rangeInputs.length, skus: filled.map(f => ({ sku: f.sku, qty: f.max })), result: 'dry-run：未点击确认' });
        closeCurrentModal();
        await sleep(1200);
        continue;
      }
      // [readback-v3] 点确认前的"成功提示"基线。
      // 原来只看整页有没有「提交成功」四个字 —— 上一行 SKC 的提示还挂在页面上，
      // 就会把本行也判成提交成功（虚增 A）。改成只认"点完之后新出现的"。
      const _succRe = /备货申请提交成功|提交成功|申请成功/g;
      const _succBefore = (norm(document.body.innerText).match(_succRe) || []).length;
      confirm.click();
      await sleep(5000);

      const pageText = norm(document.body.innerText);
      const latestRow = [...document.querySelectorAll('tbody tr')].find(tr => norm(tr.innerText).includes(`SKC：${item.skc}`));
      const latestText = latestRow ? norm(latestRow.innerText) : '';
      const _succAfter = (pageText.match(_succRe) || []).length;
      // [readback-v3] 共用口径（与 1361 补扫段一字不差）：
      //   1) 「今日已备货」只看**本行** —— 别行的平台态不能代表这一行；
      //   2) 「提交成功」只认**新增** —— 别行残留的提示不能算本次提交；
      //   3) 平台拒绝 -> failed；三条都读不到 -> failed，绝不乐观默认成功。
      if (/今日已备货/.test(latestText)) {
        // 走到确认点击的行，点击前必然没有「今日已备货」（targets 已排除 skip 行）；
        // 确认后行内新出现 = 平台刚为本次确认建了单，必须计 A（2026-11-03 实测：
        // 旧口径把这种真提交记成 skipped，导致 A=0/B=1 对账假红）。
        submitted.push({ filter: filterName, skc: item.skc, sku: item.sku, code: item.code, range: `0~${max}`, quantity: max, skuCount: rangeInputs.length, skus: filled.map(f => ({ sku: f.sku, qty: f.max })), result: '确认后行内出现今日已备货（平台已建单）' });
      }
      else if (_succAfter > _succBefore) {
        submitted.push({ filter: filterName, skc: item.skc, sku: item.sku, code: item.code, range: `0~${max}`, quantity: max, skuCount: rangeInputs.length, skus: filled.map(f => ({ sku: f.sku, qty: f.max })), result: '备货申请提交成功' });
      }
      else if ((rejectReason = extractPlatformRejectReason(pageText))) {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: `平台拒绝备货申请：${rejectReason}` });
        closeCurrentModal();
      }
      else {
        failed.push({ filter: filterName, skc: item.skc, sku: item.sku, reason: '提交后未看到新出现的成功提示，本行也无今日已备货' });
        closeCurrentModal();
      }
      await sleep(1200);
    }

      if (!hasNextPage()) break;
      if (pageNum >= 300) break;
      await clickNextPage();
      pageNum++;
    } while (true);
  }

  const json = JSON.stringify({ submitted, skipped, failed, planned });
  return btoa(unescape(encodeURIComponent(json)));
})()
"""

# ============================================================================
# 只读预览 dump（2026-10-09）：把页面真实结构 dump 出来，供"改判定逻辑前先看效果"用。
# 🔞 硬约束：本段**绝不 click 任何按钮**，只读表头/单元格文本/关键词计数。
# 存在的意义：今天连续三次因"没看真实页面就动手"返工（选品状态误读 / 卡片未清 /
# eval 超时），固化成标准前置动作。
GUIDE_PREVIEW_JS_TPL = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));

  // 表头是「两级」结构：thead 第一行是合并的大标题（如「库存数据」跨5 列），
  // 第二行才是真实列名。平铺取 thead th 会让标签和单元格错位（实测[建议备货引导] 对上 0）。
  // 所以只取每个单元格数最多的那一行 th 作为真实列名。
  const theadRows = [...document.querySelectorAll('thead tr')];
  const allTh = [...document.querySelectorAll('thead th')];
  let heads = allTh.map(th => norm(th.innerText));
  for (const tr of theadRows) {
    const ths = [...tr.querySelectorAll('th')];
    if (ths.length > heads.length) heads = ths.map(th => norm(th.innerText));
  }
  const bodyRows = [...document.querySelectorAll('tbody tr')]
    .filter(tr => norm(tr.innerText) && !/^合计\b/.test(norm(tr.innerText)));
  const sampleRows = bodyRows.slice(0, 3).map(tr => [...tr.querySelectorAll('td')].map(td => norm(td.innerText)));

  const pageText = norm(document.body.innerText);
  const KW = ['建议备货', '可备货', '库存安全', '库存偏低', '库存告急', '建议立刻备货',
              '无需备货', '暂无需申请备货', '今日已备货', '库存充足', '申请备货',
              '已加入站点', '待创建首单', '已创建首单', '已下架'];
  const keywords = {};
  for (const k of KW) keywords[k] = pageText.split(k).length - 1;

  // 错位自检：列数不一致时直接报出来，避免"标签对不上内容"误导判读
  const warnings = [];
  const headCount = heads.length;
  for (let i = 0; i < sampleRows.length; i++) {
    if (sampleRows[i].length !== headCount) {
      warnings.push('第' + (i + 1) + ' 行有 ' + sampleRows[i].length
        + ' 个单元格，表头有 ' + headCount + ' 列 → 标签可能对不上，别据此下结论');
    }
  }
  // 关键列自查：**按内容特征找列，不按表头位置**（实测表头两级嵌套 + 表尾多列，
  // 按位置取会错位：th 标「建议备货引导」的那格值其实是 0，真正的文案在另一格）。
  // 特征 = 该格文本含备货引导类关键词，且不含「申请备货」按钮文案。
  const GUIDE_TXT = /库存安全|可备货|库存告急|库存偏低|建议备货|建议立刻备货|无需备货|暂无需申请备货|今日已备货|库存充足/;
  let guideCol = -1;
  const colVotes = {};
  for (const tr of bodyRows.slice(0, 8)) {
    const tds = [...tr.querySelectorAll('td')];
    for (let j = 0; j < tds.length; j++) {
      const t = norm(tds[j].innerText);
      if (!t || t.length > 30) continue;
      if (GUIDE_TXT.test(t) && !/申请备货|备货详情|发起调价/.test(t)) {
        colVotes[j] = (colVotes[j] || 0) + 1;
      }
    }
  }
  let bestVotes = 0;
  for (const j in colVotes) {
    if (colVotes[j] > bestVotes) { bestVotes = colVotes[j]; guideCol = Number(j); }
  }
  const guideHead = (guideCol >= 0 && guideCol < heads.length) ? heads[guideCol] : '(按内容定位，未对齐到表头)';
  if (guideCol < 0) {
    warnings.push('本页没有任何单元格含备货引导类文案 —— 说明当前确实全部是「无需备货」类，或列结构变了');
  } else {
    warnings.push('「建议备货引导」实际在第 ' + guideCol + ' 列（按内容定位），表头那一列标的是「'
      + guideHead + '」—— 两者不一致属正常，别按表头位置取单元格');
  }
  const guideValues = [];
  if (guideCol >= 0) {
    for (const tr of bodyRows.slice(0, 8)) {
      const tds = [...tr.querySelectorAll('td')];
      guideValues.push(norm(tds[guideCol] ? tds[guideCol].innerText : ''));
    }
  }

  // 当前页真正满足 need&&!skip 的 SKC（只算，不处理）
  const needRe = /库存安全|可备货|库存告急|库存偏低|建议备货|建议立刻备货|已断货建议备货|已缺货建议备货/;
  const skipRe = /无需备货|暂无需申请备货|今日已备货|库存充足/;
  const needReWins = /库存安全|可备货/;
  let last = '';
  const candidateSkcs = [];
  for (const tr of bodyRows) {
    const text = norm(tr.innerText);
    let skc = text.match(/SKC：\s*(\d+)/)?.[1] || '';
    if (!skc) skc = last; else last = skc;
    if (!skc) continue;
    const need = needRe.test(text);
    const skip = skipRe.test(text) && !needReWins.test(text);
    if (need && !skip && candidateSkcs.indexOf(skc) < 0) candidateSkcs.push(skc);
  }

  const json = JSON.stringify({
    ok: true, heads, sampleRows, keywords, candidateSkcs,
    guideCol, guideHead, guideValues, warnings,
    total: (pageText.match(/共有\s*(\d+)\s*条/) || [])[1] || null,
    bodyRowCount: bodyRows.length
  });
  return btoa(unescape(encodeURIComponent(json)));
})()
"""

# ============================================================================
# 2026-10-08 用户补充：设筛选条件 → 点查询 → 翻页扫「建议备货引导」列三档并申请备货。
# 与 SUBMIT_JS_TPL 的区别：不用 6 张快速筛选卡，而是走筛选区条件，
# 因此能覆盖"不在快速筛选卡里、但建议备货引导列显示该申请"的 SKU。
# 筛选条件（实测 2026-10-08：658 条 → 42 条）：
#   ① SKU剩余库存 >= 1  ② 选品状态 = 已加入站点（只选这一项，其余三项有残留则取消）
# ⚠️ 顶部 6 张快速筛选卡片本身也是一个"筛选状态"，上一步提交后仍会选中最后一张，
#    不清掉会与筛选区条件叠加 → 补扫范围变形。STEP 0 先全部取消选中。
# 三档判定：①库存安全/可备货 ②库存偏低/建议备货 ③库存告急/建议立刻备货
# ⚠️ 拆成 4 段的原因（2026-10-08 实测踩坑）：
# browser-act 的 eval 单次超时只有 60 秒（tpc.evaljs 默认 kind="fast"）。
# 原来把「清卡片 + 设筛选 + 点查询 + 逐页弹窗申请」全塞进一次 eval，
# 实测直接 "browser-act timed out (60s, kind=fast)"，补扫整段作废。
# 改成每段都是短流程，由 Python 侧 run_guide_scan() 驱动循环，
# 每段独立返回 JSON，单段远小于 60s；某段失败不影响已得结果。
#
# 筛选条件（实测 2026-10-08：658 条 → 42 条）：
#   ① SKU剩余库存 >= 1  ② 选品状态 = 已加入站点（只选这一项，其余三项有残留则取消）
# ⚠️ 顶部 6 张快速筛选卡片本身也是一个"筛选状态"，上一步提交后仍会选中最后一张，
#    不清掉会与筛选区条件叠加 → 补扫范围变形。SETUP 段先全部取消选中。
# 三档判定：①库存安全/可备货 ②库存偏低/建议备货 ③库存告急/建议立刻备货

# ---------------------------------------------------------------- 段1：设筛选条件
# 清空快速筛选卡 → 展开筛选行 → SKU剩余库存>=1 → 选品状态=已加入站点 → 点查询 → 每页最大档
GUIDE_SETUP_JS_TPL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const stageLog = [];
  // 任一关键设置未达成：统一从这里返回 ok:false（硬失败契约）
  const failOut = msg => {
    const j = JSON.stringify({ ok: false, error: msg, stageLog });
    return btoa(unescape(encodeURIComponent(j)));
  };

  const ROW_SEL = 'div[class*="index-module__row___"]';
  const findRow = name => [...document.querySelectorAll(ROW_SEL)].find(r => {
    const l = r.querySelector('div[class*="index-module__row_label"]');
    return l && norm(l.textContent) === name;
  });
  const numOf = s => Number(String(s || '').replace(/[^\d]/g, '')) || 0;
  const setInputValue = (el, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(el, String(value));
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  // 多选下拉点完选项不会自动收起：Esc + 点空白双保险
  const closeDropdown = async () => {
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
    await sleep(300);
    document.body.click();
    await sleep(400);
  };

  // STEP 0：清空顶部快速筛选卡（卡片本身也是筛选状态，不清会与筛选区叠加）
  const cardSel = '[class*="icon-card_card"]';
  const cardSelected = el => /rgb\(64,\s*124,\s*255\)|#407cff/i.test(el.getAttribute('style') || '')
    || /active|selected|checked|current/i.test(el.className || '');
  {
    const cleared = [];
    if (!document.querySelector(cardSel)) {
      stageLog.push('✗ 快速筛选卡清空失败：卡片区不存在 (not-found)');
      const j = JSON.stringify({ ok: false, error: '快速筛选卡清空失败：卡片区不存在 (not-found)', stageLog });
      return btoa(unescape(encodeURIComponent(j)));
    }
    for (let round = 1; round <= 8; round++) {
      const on = [...document.querySelectorAll(cardSel)].filter(cardSelected);
      if (!on.length) break;
      on[0].scrollIntoView({ block: 'center' });
      on[0].click();
      cleared.push(norm(on[0].innerText).split(/\s+/)[0] || '?');
      await sleep(1800);
    }
    const left = [...document.querySelectorAll(cardSel)].filter(cardSelected)
      .map(c => norm(c.innerText).split(/\s+/)[0]).filter(Boolean);
    if (left.length) {
      const msg = '快速筛选卡清空失败：真点重试 ' + cleared.length + ' 轮仍选中 ' + left.join('/') + '（清卡未生效）';
      stageLog.push('✗ ' + msg);
      const j = JSON.stringify({ ok: false, error: msg, stageLog });
      return btoa(unescape(encodeURIComponent(j)));
    }
    stageLog.push('已清空快速筛选卡（已回读确认）' + (cleared.length ? '（原选中：' + [...new Set(cleared)].join('/') + '）' : '（本就未选中）'));
  }

  // STEP 0.5：展开折叠筛选行（做→回读→重试≤2→未达成必失败）
  {
    const fold = document.querySelector('div[class*="index-module__fold-btn"]');
    if (!fold) {
      stageLog.push('筛选区已展开（未找到折叠按钮，视为已展开）');
    } else {
      let expanded = norm(fold.textContent) !== '展开';
      if (!expanded) {
        for (let t = 0; t < 2 && !expanded; t++) {
          fold.click(); await sleep(1500);
          expanded = norm(fold.textContent) !== '展开';
        }
      }
      if (!expanded) {
        const msg = '设筛选条件失败：展开折叠筛选行（点击后回读仍显示「展开」，未生效）';
        stageLog.push('✗ ' + msg);
        return failOut(msg);
      }
      stageLog.push('已展开筛选行（已回读确认）');
    }
  }

  // STEP 1：SKU剩余库存 >= 1（做→回读真实状态→重试≤2→未达成必失败）
  {
    const row = findRow('SKU剩余库存');
    if (!row || !visible(row)) {
      return failOut('设筛选条件失败：SKU剩余库存（行不可见）');
    }
    let cmpOk = false, numOk = false, cmpNow = '', numNow = '';
    for (let t = 0; t < 2; t++) {
      const cmpHeader = row.querySelector('[data-testid="beast-core-select-header"]');
      if (!cmpHeader) break;
      const cur = row.querySelector('input[readonly]');
      if (!(cur && norm(cur.value) === '>=')) {
        cmpHeader.click(); await sleep(900);
        const ge = [...document.querySelectorAll('[role="option"],[class*="ST_itemRendererLabel"],[class*="cIL_item"]')]
          .filter(e => visible(e) && norm(e.textContent) === '>=')[0];
        if (ge) { ge.click(); await sleep(800); stageLog.push('比较符已改为 >='); }
        await closeDropdown();
      }
      const num = [...row.querySelectorAll('input')].find(i => !i.readOnly);
      if (num && norm(num.value) !== '1') { setInputValue(num, '1'); num.blur(); await sleep(700); stageLog.push('SKU剩余库存 = 1'); }
      const cur2 = row.querySelector('input[readonly]');
      cmpNow = cur2 ? norm(cur2.value) : '';
      const num2 = [...row.querySelectorAll('input')].find(i => !i.readOnly);
      numNow = num2 ? norm(num2.value) : '';
      cmpOk = cmpNow === '>='; numOk = numNow === '1';
      if (cmpOk && numOk) break;
    }
    if (cmpOk && numOk) stageLog.push('SKU剩余库存>=1（已回读确认：比较符=' + cmpNow + '，数值=' + numNow + '）');
    else return failOut('设筛选条件失败：SKU剩余库存>=1（未生效：比较符=' + (cmpNow || '未读到') + '，数值=' + (numNow || '未读到') + '）');
  }

  // STEP 2：选品状态 = 只选「已加入站点」（做→回读表头→重试≤2→未达成必失败）
  {
    const ALL_OPTS = ['已加入站点', '待创建首单', '已创建首单', '已下架'];
    const WANT_ON = '已加入站点';
    const row = findRow('选品状态');
    if (!row || !visible(row)) {
      return failOut('设筛选条件失败：选品状态（行不可见）');
    }
    const hdr = row.querySelector('[data-testid="beast-core-select-header"]');
    if (!hdr) {
      return failOut('设筛选条件失败：选品状态（未找到下拉头）');
    }
    const before = norm(hdr.innerText || hdr.textContent);
    let after = before, others = [], prob = [];
    for (let t = 0; t < 2; t++) {
      prob = [];
      for (const want of ALL_OPTS) {
        const shouldOn = (want === WANT_ON);
        hdr.click(); await sleep(800);
        const opt = [...document.querySelectorAll('[role="option"],[class*="ST_itemRendererLabel"],[class*="cIL_item"]')]
          .filter(e => visible(e) && norm(e.textContent) === want)[0];
        if (!opt) { if (shouldOn) prob.push(want + '(无此选项)'); await closeDropdown(); continue; }
        const on = opt.getAttribute('aria-selected') === 'true'
          || /selected|checked|active/i.test(opt.className || '')
          || /rgb\(64,\s*124,\s*255\)|#407cff/i.test(opt.getAttribute('style') || '');
        if (on === shouldOn) { await closeDropdown(); continue; }  // 状态已对，不碰
        opt.click(); await sleep(700); await closeDropdown();
      }
      after = norm(hdr.innerText || hdr.textContent);
      others = ALL_OPTS.filter(w => w !== WANT_ON && after.includes(w));
      if (after.includes(WANT_ON) && !others.length && !prob.length) break;
    }
    if (after.includes(WANT_ON) && !others.length && !prob.length) {
      stageLog.push('选品状态 表头：' + JSON.stringify(before) + ' → ' + JSON.stringify(after)
        + '（已回读确认：只选中「' + WANT_ON + '」，无残留）');
    } else {
      return failOut('设筛选条件失败：选品状态（未生效：表头=' + JSON.stringify(after)
        + (others.length ? '，残留=' + others.join('/') : '')
        + (prob.length ? '，异常=' + prob.join(',') : '') + '）');
    }
  }

  // STEP 3：点「查询」→ 回读「共有N条」（读不到数=查询未生效，重试≤2→必失败）
  let filteredTotal = null;
  {
    let clicked = false;
    for (let t = 0; t < 2; t++) {
      const queryBtn = [...document.querySelectorAll('button')].filter(visible)
        .find(b => norm(b.textContent) === '查询');
      if (queryBtn) { queryBtn.click(); clicked = true; await sleep(2500); }
      const m = norm(document.body.innerText).match(/共有\s*(\d+)\s*条/);
      if (m) { filteredTotal = Number(m[1]); break; }
    }
    if (!clicked) return failOut('设筛选条件失败：点查询（未找到「查询」按钮）');
    if (filteredTotal === null) return failOut('设筛选条件失败：点查询（未生效：查询后读不到「共有N条」）');
    stageLog.push('已点「查询」（已回读确认：共有' + filteredTotal + '条）');
  }

  // STEP 4：每页条数取最大档（做→回读当前档→重试≤2→未达成必失败；无sizeChanger降级partial）
  let pageSize = null;
  {
    const sizer = document.querySelector('li[class*=PGT_sizeChanger_]');
    if (!sizer) {
      stageLog.push('每页条数=无sizeChanger（环境差异，降级partial继续）');
    } else {
      let okSz = false, szNow = norm(sizer.textContent), diag = '';
      for (let t = 0; t < 2 && !okSz; t++) {
        const trigger = sizer.querySelector('[data-testid="beast-core-select-header"]') || sizer;
        trigger.click(); await sleep(900);
        let opts = [...document.querySelectorAll('[role="option"],[class*="ST_itemRendererLabel"],[class*="cIL_item"],[class*="select-item"]')]
          .filter(visible).filter(o => !isNaN(numOf(o.textContent)) && numOf(o.textContent) > 0);
        const withTiao = opts.filter(o => /条/.test(o.textContent));
        if (withTiao.length) opts = withTiao;
        if (opts.length) {
          const best = opts.reduce((a, b) => (numOf(b.textContent) > numOf(a.textContent) ? b : a));
          pageSize = numOf(best.textContent);
          best.click(); await sleep(1500);
        } else {
          diag = '（诊断：可见数值选项0个，sizer内含下拉头=' + !!sizer.querySelector('[data-testid="beast-core-select-header"]') + '）';
          document.body.click(); await sleep(400);
        }
        const sizerNow = document.querySelector('li[class*=PGT_sizeChanger_]');
        szNow = sizerNow ? norm(sizerNow.textContent) : '';
        const rows = document.querySelectorAll('tbody tr').length;
        okSz = pageSize !== null && (szNow.includes(String(pageSize)) || rows > 0);
        if (okSz) await closeDropdown();
      }
      if (okSz) stageLog.push('每页条数=' + pageSize + '（已回读确认：sizer=' + szNow + '）');
      else return failOut('设筛选条件失败：每页最大档（未生效：pageSize=' + (pageSize === null ? '未读到' : pageSize) + '，sizer=' + (szNow || '未读到') + '）' + diag);
    }
  }

  // 独立判据：读平台自己的总数，不靠"我点了查询"自报成功（null=未达成，必失败）
  const pageText = norm(document.body.innerText);
  const totalMatch = pageText.match(/共有\s*(\d+)\s*条/);
  filteredTotal = totalMatch ? Number(totalMatch[1]) : null;
  stageLog.push('筛选后总条数=' + (filteredTotal === null ? '未读到' : filteredTotal));
  if (filteredTotal === null) {
    return failOut('设筛选条件失败：查询结果总数（未生效：读不到「共有N条」）');
  }

  const json = JSON.stringify({ ok: true, stageLog, filteredTotal, pageSize });
  return btoa(unescape(encodeURIComponent(json)));
})()
"""

# ---------------------------------------------------------------- 段2：扫当前页，返回可申请清单（不点任何按钮）
GUIDE_SCAN_PAGE_JS_TPL = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const needRe = /库存安全|可备货|库存告急|库存偏低|建议备货|建议立刻备货|已断货建议备货|已缺货建议备货/;
  const skipRe = /无需备货|暂无需申请备货|今日已备货|库存充足/;
  const needReWins = /库存安全|可备货/;

  const rows = [...document.querySelectorAll('tbody tr')]
    .filter(tr => norm(tr.innerText) && !/^合计\b/.test(norm(tr.innerText)));
  let lastRowSkc = '';
  const items = [];
  for (const row of rows) {
    const text = norm(row.innerText);
    let skc = text.match(/SKC：\s*(\d+)/)?.[1] || '';
    const ownSkc = !!skc;
    if (!skc) skc = lastRowSkc;
    if (ownSkc) lastRowSkc = skc;
    const need = needRe.test(text);
    const skip = skipRe.test(text) && !needReWins.test(text);
    if (!(need && !skip && ownSkc && skc)) continue;
    const rowEl = [...document.querySelectorAll('tbody tr')]
      .find(tr => norm(tr.innerText).includes('SKC：' + skc));
    const hasAction = !!(rowEl && [...rowEl.querySelectorAll('button,a,span,div')]
      .find(el => visible(el) && norm(el.innerText || el.textContent) === '申请备货'));
    items.push({
      skc,
      sku: text.match(/SKU ID：\s*(\d+)/)?.[1] || '',
      code: text.match(/SKC货号：\s*(\d+)/)?.[1] || '',
      hasAction
    });
  }
  const next = document.querySelector('li[class*=PGT_next_]');
  const hasNext = !!(next && !/disabled/.test(next.className || '') && visible(next));
  const json = JSON.stringify({ ok: true, items, rowsScanned: rows.length, hasNext });
  return btoa(unescape(encodeURIComponent(json)));
})()
"""

# ---------------------------------------------------------------- 段3：翻到下一页
GUIDE_NEXT_PAGE_JS_TPL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const next = document.querySelector('li[class*=PGT_next_]');
  if (!next || /disabled/.test(next.className || '') || !visible(next)) {
    return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, moved: false, reason: 'no next page' }))));
  }
  next.click();
  await sleep(2500);
  const json = JSON.stringify({ ok: true, moved: true });
  return btoa(unescape(encodeURIComponent(json)));
})()
"""

# ---------------------------------------------------------------- 段4：对单个 SKC 走申请备货弹窗
# 入参 TARGET_SKC_PLACEHOLDER（JSON 字符串）；返回单条结果
GUIDE_APPLY_ONE_JS_TPL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const dryRun = DRY_RUN_PLACEHOLDER;
  const target = TARGET_SKC_PLACEHOLDER;

  const closeCurrentModal = () => {
    const modal = [...document.querySelectorAll('[class*=MDL_outerWrapper], [role=dialog]')].find(el => visible(el));
    if (!modal) return false;
    const close = modal.querySelector('[class*=MDL_headerCloseIcon]')
      || [...modal.querySelectorAll('button')].find(el => visible(el) && /取消|关闭|我知道了/.test(norm(el.innerText)));
    if (close) { close.dispatchEvent(new MouseEvent('click', { bubbles: true })); return true; }
    return false;
  };
  const setInputValue = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(input, String(value));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const fail = reason => ({ source: 'guide-scan', skc: target.skc, sku: target.sku, code: target.code, reason });

  const row = [...document.querySelectorAll('tbody tr')]
    .find(tr => norm(tr.innerText).includes('SKC：' + target.skc));
  if (!row) return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: fail('行未找到（可能已翻页）') }))));

  const action = [...row.querySelectorAll('button,a,span,div')]
    .find(el => visible(el) && norm(el.innerText || el.textContent) === '申请备货');
  if (!action) return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: fail('申请备货入口未找到') }))));

  action.scrollIntoView({ block: 'center' });
  action.click();
  await sleep(3000);

  // 注意：判"弹窗出现"必须排除表格行本身，且要求两个独立特征（标题 + 备货范围）
  let modal = [...document.querySelectorAll('[class*=MDL_outerWrapper], [role=dialog]')]
    .find(el => visible(el) && !el.closest('tbody')
      && /申请备货/.test(norm(el.innerText)) && /备货范围|SKC/.test(norm(el.innerText)));
  if (!modal) {
    const anyModal = [...document.querySelectorAll('[class*=MDL_outerWrapper], [role=dialog]')].find(el => visible(el) && !el.closest('tbody'));
    const txt = anyModal ? norm(anyModal.innerText) : '';
    if (/暂无需申请备货|无需备货|今日已备货/.test(txt)) {
      return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: { source: 'guide-scan', skc: target.skc, sku: target.sku, reason: txt.match(/暂无需申请备货|无需备货|今日已备货/)[0], kind: 'skip' } }))));
    }
    return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: fail('申请备货弹窗未出现') }))));
  }

  const continueButton = [...modal.querySelectorAll('button,a,span,div')]
    .filter(el => visible(el) && norm(el.innerText || el.textContent) === '继续申请备货')
    .sort((a, b) => ({ BUTTON: 0, A: 1, SPAN: 2, DIV: 3 }[a.tagName] ?? 9) - ({ BUTTON: 0, A: 1, SPAN: 2, DIV: 3 }[b.tagName] ?? 9))[0];
  if (continueButton) {
    continueButton.scrollIntoView({ block: 'center' });
    continueButton.click();
    for (let w = 0; w < 16; w++) {
      await sleep(500);
      modal = [...document.querySelectorAll('[class*=MDL_outerWrapper], [role=dialog]')]
        .find(el => visible(el) && !el.closest('tbody')
          && /申请备货/.test(norm(el.innerText)) && /备货范围|SKC/.test(norm(el.innerText))) || modal;
      if ([...modal.querySelectorAll('input')].some(el => visible(el) && /备货范围\s*0\s*~\s*\d+/.test(el.placeholder || ''))) break;
    }
  }

  const rangeInputs = [...modal.querySelectorAll('input')]
    .filter(el => visible(el) && /备货范围\s*0\s*~\s*\d+/.test(el.placeholder || ''));
  if (!rangeInputs.length) {
    closeCurrentModal(); await sleep(1000);
    return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: fail('备货范围输入框未找到') }))));
  }

  const filled = [];
  for (const ri of rangeInputs) {
    const mx = ri.placeholder.match(/备货范围\s*0\s*~\s*(\d+)/)?.[1];
    if (!mx) continue;
    ri.scrollIntoView({ block: 'center' });
    ri.focus();
    setInputValue(ri, mx);
    filled.push({ sku: '', max: +mx });
    await sleep(250);
  }
  if (filled.length !== rangeInputs.length) {
    closeCurrentModal(); await sleep(1000);
    return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: fail('部分SKU备货范围无法解析（' + filled.length + '/' + rangeInputs.length + '）') }))));
  }

  const maxStr = String(filled.reduce((m, f) => Math.max(m, f.max), 0));
  const confirm = [...modal.querySelectorAll('button')].find(el => visible(el) && norm(el.innerText) === '确认');
  if (!confirm) return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: fail('确认按钮未找到') }))));

  const base = { source: 'guide-scan', skc: target.skc, sku: target.sku, code: target.code,
                 range: '0~' + maxStr, quantity: maxStr, skuCount: rangeInputs.length };

  if (dryRun) {
    closeCurrentModal(); await sleep(1000);
    return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result: { ...base, kind: 'planned', result: 'dry-run：未点击确认' } }))));
  }

  // 对齐快速筛选提交强判据（L788）：确认后必须回读到平台真实成功/跳过/拒绝证据才算结果；
  // 三者都读不到（弹窗残留/网络慢/文案变化）→ failed，禁止乐观默认成功。有限轮询≤6次×1.5s。
  const platformRejectRe = /平台拒绝|申请被拒绝|拒绝备货|拒绝[^。！？\n]{0,30}备货|备货申请[^。！？\n]{0,30}拒绝|商品处于清退中，不可创建备货单|不可创建备货单|不支持申请备货|无法申请备货|不能申请备货|不允许申请备货|创建备货单失败|申请失败/;
  const extractPlatformRejectReason = text => {
    const match = norm(text).match(platformRejectRe);
    return match ? match[0] : '';
  };
  const _succRe = /备货申请提交成功|提交成功|申请成功/g;
  const _succBefore = (norm(document.body.innerText).match(_succRe) || []).length;
  confirm.click();
  let result = null;
  let lastPageText = '';
  for (let poll = 1; poll <= 6; poll++) {
    await sleep(1500);
    lastPageText = norm(document.body.innerText);
    const latestRow = [...document.querySelectorAll('tbody tr')].find(tr => norm(tr.innerText).includes('SKC：' + target.skc));
    const latestText = latestRow ? norm(latestRow.innerText) : '';
    // [readback-v3] 与快速筛选段（817）共用同一套口径，一字不差：
    //   1) 「今日已备货」只看本行 —— 原来还看 lastPageText（整页），
    //      别行有今日已备货就会把本行判成跳过，等于把真提交抹掉；
    //   2) 「提交成功」只认点完之后新增的 —— 原来只看整页有没有，
    //      上一行的提示还挂着就会虚增 A。
    if (/今日已备货/.test(latestText)) {
      // 与 817 段同口径：确认后行内新出现「今日已备货」= 平台刚为本次确认建单，计 A。
      result = { ...base, kind: 'submitted', result: '确认后行内出现今日已备货（平台已建单）' };
      break;
    }
    const _succNow = (lastPageText.match(_succRe) || []).length;
    if (_succNow > _succBefore) {
      result = { ...base, kind: 'submitted', result: '备货申请提交成功' };
      break;
    }
    const rejectReason = extractPlatformRejectReason(lastPageText);
    if (rejectReason) {
      result = { ...base, kind: 'failed', reason: '平台拒绝备货申请：' + rejectReason };
      break;
    }
  }
  if (!result) {
    result = { ...base, kind: 'failed',
               reason: '提交后未看到成功提示（SKC：' + target.skc + '，回读6次未见成功/拒绝/今日已备货）' };
  }
  closeCurrentModal();
  await sleep(1200);
  return btoa(unescape(encodeURIComponent(JSON.stringify({ ok: true, result }))));
})()
"""



# =========================================================================== 引擎
class RestockEngine:
    def __init__(self, trial=False, send_feishu=True, do_print=True):
        self.trial = trial
        self.send_feishu = send_feishu and not trial
        self.do_print = do_print and not trial

        self.started = datetime.now()
        self.token_usage = os.environ.get("CODEX_TOKEN_USAGE", "")
        ts = self.started.strftime("%Y%m%d_%H%M%S")
        self.session = "temu_restock_keepopen_" + ts
        self.stage = "init"

        # 原生产物：run-temu-restock-daily.ps1 的 Save-RestockLatestSnapshot 固定写这个文件
        suffix = "_py" if trial else ""
        self.snapshot_path = os.path.join(LOGS_DIR, "restock_latest%s.json" % suffix)

        # 公共库浏览器客户端（禁止重造）
        self.tb = tpc.TemuBrowser(self.session, SALE_URL)

        self.review = []
        self.unknown_cards = []
        self.submit_data = {"submitted": [], "skipped": [], "failed": [], "planned": []}
        # 2026-10-08 新增：筛选条件翻页扫「建议备货引导」列的结果
        self.guide_data = {"submitted": [], "skipped": [], "failed": [], "planned": [],
                           "stageLog": [], "filteredTotal": None}

        # [readback-v2] 第2道平台对账：三段探针结果 + 适配层
        self.rb = None
        self.rb_before = None
        self.rb_after = None
        self.rb_new = None
        self.readback_errors = []
        self.readback_snapshot = None
        try:
            from restock_readback_adapter import RestockReadbackAdapter as _RRA
            self.rb = _RRA(tb=self.tb, logs_dir=LOGS_DIR,
                           run_id="restock-%s" % ts)
        except Exception as _e:
            self.readback_errors.append("adapter 加载失败: %s" % _e)
            print("[readback] adapter 加载失败（本次将判失败，不报成功）: %s" % _e)

    # ------------------------------------------------------------ 第2道：平台回读
    def _readback_enabled(self):
        """TEMU_RESTOCK_READBACK=0 可显式关掉（仅限排障）；默认开。"""
        return os.environ.get("TEMU_RESTOCK_READBACK", "1") != "0"

    def _probe(self, label, fn):
        """跑一段只读探针。任何异常都记下来并返回 None —— 由收尾判失败，
        绝不把"探针没跑成"当成"平台侧没问题"。"""
        if not self._readback_enabled():
            print("[readback] %s skipped (TEMU_RESTOCK_READBACK=0)" % label)
            self.readback_errors.append("%s 被 TEMU_RESTOCK_READBACK=0 关闭" % label)
            return None
        if self.rb is None:
            print("[readback] %s 不可用：adapter 未加载" % label)
            return None
        try:
            v = fn()
            print("[readback] %s ok: %s" % (label, json.dumps(v, ensure_ascii=False)[:400]))
            return v
        except Exception as e:
            print("[readback] %s FAILED: %s: %s" % (label, type(e).__name__, e))
            self.readback_errors.append("%s: %s: %s" % (label, type(e).__name__, e))
            return None

    def _run_platform_readback(self, verdict):
        """把三段探针灌进底座，跑金等式（A==B==C）。返回 (ok, reason, snapshot)。

        fail-closed 三条：
          1) 任一探针缺 → 失败，理由写清是哪一段；
          2) 金等式不等 → 失败，底座抛 ReadbackMismatch，理由带三数；
          3) 小票过不了 assert_snapshot_complete → 失败。
        """
        if not self._readback_enabled():
            return False, "平台回读被 TEMU_RESTOCK_READBACK=0 关闭，无法对账", None
        if self.rb is None:
            return False, "平台回读适配层未加载: %s" % ("; ".join(self.readback_errors) or "未知"), None
        missing = [n for n, v in (("pending_before", self.rb_before),
                                  ("pending_after", self.rb_after),
                                  ("platform_new_orders", self.rb_new)) if v is None]
        if missing:
            return False, "平台回读取数不全，缺: %s（%s）" % (
                "、".join(missing), "; ".join(self.readback_errors) or "无细节"), None
        try:
            snap = self.rb.build_snapshot(
                submit_data=self.submit_data,
                guide_data=self.guide_data,
                before_probe=self.rb_before,
                after_probe=self.rb_after,
                new_orders_probe=self.rb_new,
            )
            self.readback_snapshot = snap
            # build_snapshot 在对账不等/闸门不过时会返回 ok=False 的半张票，
            # 那也算失败 —— 不能因为"函数没抛异常"就当成通过（这就是假绿）。
            if not snap.get("ok"):
                return False, snap.get("failure_reason") or "对账未通过（无理由字段）", snap
            return True, "", snap
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e), None

    def _stage_enabled(self, stage_id):
        return bool(STEP1_STAGE_ENABLED.get(stage_id, True))

    def _skip_stage(self, stage_id):
        label = STEP1_STAGE_LABELS.get(stage_id, stage_id)
        print("[step1-ops] SKIP stage=%s (%s) enabled=false; continue." % (stage_id, label))

    # ------------------------------------------------------------ base64 JS 结果解码
    @staticmethod
    def decode_b64_json(b64_text):
        raw = b64_text.strip().strip("'").strip('"')
        data = base64.b64decode(raw)
        return json.loads(data.decode("utf-8"))

    # ------------------------------------------------------------ 补扫：多段短 eval 驱动
    # 为什么不一次 eval：browser-act 的 eval 单次超时 60s（tpc.evaljs 默认 kind="fast"），
    # 实测把「清卡片+设筛选+查询+逐页弹窗」塞进一次 eval 必然timed out，整段作废。
    # 现在拆成 setup / scan_page / apply_one / next_page 四段，每段都远小于 60s；
    # 每段独立 try，某段失败只记 failed 并继续，已得结果不丢。
    MAX_GUIDE_PAGES = int(os.environ.get("TEMU_GUIDE_MAX_PAGES", "60"))

    # ------------------------------------------------------------ 只读预览（改代码前必跑）
    # 2026-10-09 加。教训来源：今天连续三次因"没先看真实页面就动手"返工——
    #   ①把「选品状态都选了」误读成四项全勾 ②补扫没清快速筛选卡导致范围变形
    #   ③一次 eval 塞太多撞 60 秒超时。
    # 所以固化成标准动作：**任何补扫/判定逻辑改动前先跑这个**，用真实页面数据说话，别猜。
    # 它只读：设筛选条件（页面状态，可逆）+ 点查询，**绝不点任何"申请备货/确认"按钮**。
    def run_guide_preview(self):
        out = {"ok": False, "note": "只读预览：不点任何申请/确认按钮"}
        ok, setup = self._eval_js(tpc.apply_dry_run(GUIDE_SETUP_JS_TPL, self.trial))
        out["setup"] = setup
        if not ok:
            out["error"] = "设筛选阶段失败: %s" % setup.get("error")
            return out
        ok, dump = self._eval_js(GUIDE_PREVIEW_JS_TPL)
        out["dump"] = dump
        out["ok"] = bool(ok)
        if not ok:
            out["error"] = "页面 dump 失败: %s" % dump.get("error")
        return out

    def preview_lines(self):
        """把 run_guide_preview() 的结果压成几行人能读的文字，直接 print 出来。"""
        r = self.run_guide_preview()
        print("\n" + "=" * 66)
        print("【只读预览】以下为页面真实状态，本次未点任何申请/确认按钮")
        print("=" * 66)
        for line in r.get("setup", {}).get("stageLog", []) or []:
            print("  筛选| " + line)
        if not r.get("ok"):
            print("  ✗ 失败: " + str(r.get("error")))
            print("=" * 66 + "\n")
            return r
        d = r["dump"]
        if d.get("warnings"):
            print("  ── ⚠️ 自检告警 ──")
            for w in d["warnings"]:
                print("   ! " + w)
        print("  ── 表头列（%d 列）──" % len(d.get("heads") or []))
        print("  " + " | ".join(d.get("heads") or []))
        for i, row in enumerate(d.get("sampleRows") or []):
            print("  ── 第%d 行单元格 ──" % (i + 1))
            for j, cell in enumerate(row):
                label = (d.get("heads") or [])[j] if j < len(d.get("heads") or []) else "col%d" % j
                print("      [%s] %s" % (label, cell))
        gc = d.get("guideCol")
        if gc is not None and gc >= 0:
            print("  ──「建议备货引导」真实内容（第 %d 列，表头标的是「%s」）──"
                  % (gc, d.get("guideHead")))
            for i, v in enumerate(d.get("guideValues") or []):
                print("      行%d: %s" % (i + 1, v if v else "(空)"))
        print("  ── 关键词命中次数（整页）──")
        for k, v in (d.get("keywords") or {}).items():
            mark = "★" if v else " "
            print("   %s %-16s %s" % (mark, k, v))
        print("  ── 当前页可申请 SKC ──")
        print("   " + (", ".join(d.get("candidateSkcs") or []) or "(无)"))
        print("=" * 66 + "\n")
        return r

    def _eval_js(self, js, timeout_note=""):
        """跑一段补扫 JS，返回 (ok, parsed_dict)。超时/异常都不抛到主流程。"""
        try:
            parsed = self.decode_b64_json(self.tb.evaljs(js))
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                return False, parsed
            return True, parsed
        except Exception as e:
            return False, {"error": "%s%s" % (e, timeout_note)}

    def run_guide_scan(self):
        result = {"submitted": [], "skipped": [], "failed": [], "planned": [],
                  "stageLog": [], "filteredTotal": None, "pagesScanned": 0, "rowsScanned": 0}
        processed = set()

        # ---- 段1：设筛选条件
        ok, setup = self._eval_js(tpc.apply_dry_run(GUIDE_SETUP_JS_TPL, self.trial))
        setup_err = str(setup.get("error") or "")
        if not ok and ("快速筛选卡" in setup_err or "设筛选条件失败" in setup_err):
            # 清快速筛选卡/设筛选条件失败＝硬失败：必须抛给 run() 兜底（退出码1），禁止只警告继续
            raise RuntimeError(setup_err)
        if not ok:
            result["failed"].append({"source": "guide-scan", "skc": "", "sku": "",
                                     "reason": "设筛选条件失败: %s" % setup.get("error")})
            result["stageLog"].append("⚠️ 设筛选条件阶段失败，补扫未执行")
            return result
        result["stageLog"].extend(setup.get("stageLog") or [])
        result["filteredTotal"] = setup.get("filteredTotal")
        # 独立判据：平台自己报的条数没读到 → 视为筛选未生效，不继续盲扫
        if result["filteredTotal"] is None:
            result["failed"].append({"source": "guide-scan", "skc": "", "sku": "",
                                     "reason": "筛选后总条数未读到，无法确认筛选已生效"})
            result["stageLog"].append("⚠️ 未读到「共有 N 条」，不继续翻页（避免在错范围里盲扫）")
            return result

        # ---- 段2+4：逐页扫 → 逐个申请 → 翻页
        page = 1
        while page <= self.MAX_GUIDE_PAGES:
            ok, scanned = self._eval_js(GUIDE_SCAN_PAGE_JS_TPL)
            if not ok:
                result["failed"].append({"source": "guide-scan", "skc": "", "sku": "",
                                         "reason": "第%d页扫描失败: %s" % (page, scanned.get("error"))})
                break
            result["pagesScanned"] = page
            result["rowsScanned"] += scanned.get("rowsScanned") or 0
            items = [i for i in (scanned.get("items") or []) if i.get("skc")
                     and i["skc"] not in processed]
            result["stageLog"].append("第%d页：扫 %s 行，命中 %s 个可申请 SKC"
                                      % (page, scanned.get("rowsScanned"), len(items)))

            # [gray-scale] 灰度闸要管整次运行：submit 段已经提交的数量也要算进来，
            # 否则 submit 交了 1 条、补扫再交 2 条，闸门等于没关。
            _already = len((self.submit_data or {}).get("submitted") or [])
            if SUBMIT_LIMIT > 0 and _already + len(result["submitted"]) >= SUBMIT_LIMIT:
                result["stageLog"].append(
                    "灰度闸：已达 %d 条上限（submit 段 %d 条），补扫不再申请"
                    % (SUBMIT_LIMIT, _already))
                break

            for item in items:
                if SUBMIT_LIMIT > 0 and _already + len(result["submitted"]) >= SUBMIT_LIMIT:
                    result["stageLog"].append(
                        "灰度闸：本页中途达到 %d 条上限，停止继续申请" % SUBMIT_LIMIT)
                    break
                processed.add(item["skc"])
                if idem_done(item["skc"]):
                    result["skipped"].append({"source": "guide-scan", "skc": item["skc"], "sku": item.get("sku"),
                                              "reason": "幂等账本：本店今日该 SKC 已成功提交过，跳过（防重复）"})
                    continue
                if not item.get("hasAction"):
                    result["failed"].append({"source": "guide-scan", "skc": item["skc"],
                                             "sku": item.get("sku"), "reason": "申请备货入口未找到"})
                    continue
                js = (tpc.apply_dry_run(GUIDE_APPLY_ONE_JS_TPL, self.trial)
                      .replace("TARGET_SKC_PLACEHOLDER", json.dumps(item, ensure_ascii=False)))
                ok, applied = self._eval_js(js)
                if not ok:
                    result["failed"].append({"source": "guide-scan", "skc": item["skc"],
                                             "sku": item.get("sku"),
                                             "reason": "申请执行异常: %s" % applied.get("error")})
                    continue
                r = applied.get("result") or {}
                if not r.get("kind") and r.get("reason") == "申请备货弹窗未出现":
                    # 弹窗未出现可能是页面忙（210207）或首击未生效：
                    # 确认前必无业务副作用，等 20s 后整段重试一次。
                    time.sleep(20)
                    ok, applied = self._eval_js(js)
                    if ok:
                        r = applied.get("result") or {}
                kind = r.get("kind")
                if kind == "submitted":
                    idem_mark(item["skc"], meta={"via": "guide-scan", "result": r.get("result")})
                    result["submitted"].append(r)
                elif kind == "planned":
                    result["planned"].append(r)
                elif kind == "skipped":
                    result["skipped"].append(r)
                else:
                    result["failed"].append(r)

            if not scanned.get("hasNext"):
                break
            ok, moved = self._eval_js(GUIDE_NEXT_PAGE_JS_TPL)
            if not ok or not moved.get("moved"):
                break
            page += 1

        if page > self.MAX_GUIDE_PAGES:
            result["stageLog"].append("⚠️ 已达翻页上限 %d 页，停止" % self.MAX_GUIDE_PAGES)
        return result

    def run_review_js(self):
        base_js = (REVIEW_JS_TPL
                   .replace("FILTERS_PLACEHOLDER", json.dumps(FILTERS, ensure_ascii=False))
                   .replace("KNOWN_NON_STOCK_PLACEHOLDER",
                            json.dumps(KNOWN_NON_STOCK_CARDS, ensure_ascii=False)))
        reports = []
        unknown_cards = []
        for index, filter_name in enumerate(FILTERS):
            js = base_js.replace(
                "ACTIVE_FILTER_PLACEHOLDER",
                json.dumps(filter_name, ensure_ascii=False))
            # 2026-10-15：REVIEW_JS 较长，页面忙时会撞 Error 210207（frame tree timed out）。
            # 失败先等 20s 重试，最多 3 次，再抛。
            raw = None
            last_exc = None
            for _try in range(1, 4):
                try:
                    raw = self.tb.evaljs(js)
                    break
                except Exception as exc:
                    last_exc = exc
                    print("review eval 异常（%s, 第 %d 次），20s 后重试: %s"
                          % (filter_name, _try, exc))
                    time.sleep(20)
            if raw is None:
                raise last_exc
            parsed = self.decode_b64_json(raw)
            reports.extend(parsed.get("reports", []))
            if index == 0:
                unknown_cards = parsed.get("unknownCards", [])
        self.review = reports
        self.unknown_cards = unknown_cards

    # ------------------------------------------------------------ 登录守卫（对齐 Ensure-AgentSellerLoggedIn）
    def ensure_logged_in(self):
        self.stage = "login guard"
        for attempt in range(1, 13):
            print("Login guard attempt %d/12" % attempt)
            # 2026-10-15：页面刚打开仍在提交导航 / Chrome 忙时，eval 会直接抛
            # Error 210207（Page target frame tree timed out）。这类瞬时错误吞掉后
            # 走下面的 wait_stable(60) 再试，避免整步被一次抖动打挂。
            try:
                result_text = self.tb.evaljs(LOGIN_GUARD_JS)
            except Exception as exc:
                print("login guard eval 异常（attempt %d），等待后重试: %s" % (attempt, exc))
                result_text = ""
            print(result_text)
            if "sale-management-ready" in result_text:
                return
            if "manual-login-required" in result_text:
                raise RuntimeError(
                    "AgentSeller 登录失效或需要验证码/人工验证。请在 browser-act 浏览器 "
                    "（chrome_local_101926808372379656）里登录一次后再运行。详情: " + result_text)
            self.tb.wait_stable(60)
        raise RuntimeError("登录守卫 12 次尝试后仍未进入销售管理页。")

    # ------------------------------------------------------------ 店铺切换（对齐 Ensure-TargetStore）
    def ensure_target_store(self):
        self.stage = "ensure target store"
        js = ENSURE_STORE_JS_TPL.replace(
            "TARGET_STORE_PLACEHOLDER", json.dumps(SHOP_NAME, ensure_ascii=False))
        result = self.tb.evaljs(js)
        print(result)
        # 同时兼容 JSON 与 Python repr 拼写
        if re.search(r"""['"]ok['"]\s*:\s*[Ff]alse\b""", result):
            raise RuntimeError("切换到目标店铺失败 %s: %s" % (SHOP_NAME, result))
        self.tb.wait_stable(30)

        detected = self.tb.evaljs_fast(DETECT_STORE_JS).strip().strip("'").strip('"')
        if detected and SHOP_NAME in detected:
            self.store_name = detected
        else:
            self.store_name = SHOP_NAME
        print("Store name: " + self.store_name)

    # ------------------------------------------------------------ 快照/报告（对齐源）
    def duration_text(self, end):
        secs = int((end - self.started).total_seconds())
        h, rem = divmod(max(secs, 0), 3600)
        m, s = divmod(rem, 60)
        if h >= 1:
            return "%02d:%02d:%02d" % (h, m, s)
        return "%02d:%02d" % (m, s)

    def format_report(self, end, succeeded, error_message="", failure_page_state=""):
        L = []
        L.append("【Temu备货复查最终日报】")
        L.append("")
        L.append("执行结果：%s" % ("已完成" if succeeded else "失败"))
        L.append("运行时间：%s" % self.duration_text(end))
        L.append("开始时间：%s" % self.started.strftime("%Y-%m-%d %H:%M:%S"))
        L.append("结束时间：%s" % end.strftime("%Y-%m-%d %H:%M:%S"))
        L.append("店铺名称：%s" % self.store_name)
        L.append("会话名称：%s" % REPORT_SESSION_NAME)
        L.append("自动化名称：%s" % AUTOMATION_NAME)
        L.append("使用了多少token：%s" % (self.token_usage or "未获取"))
        L.append("自动提交备货申请：是（发现可处理项时按备货范围最大值提交）")
        gd = getattr(self, "guide_data", None) or {}
        gstage = "、".join(gd.get("stageLog") or [])
        L.append("补扫（筛选条件翻页扫建议备货引导）：%s"
                 % (gstage if gstage else "未执行"))
        L.append("浏览器 session：%s（本次创建，结束前关闭）" % self.session)
        L.append("")

        if not succeeded:
            L.append("失败阶段：脚本运行或页面复查")
            L.append("页面 URL/标题：%s" % (failure_page_state or "未获取"))
            L.append("错误原因：")
            L.append(error_message.strip())
            L.append("已完成步骤：已启动自动化并进入失败 trap；详见脚本输出。")
            L.append("下一步建议：检查登录/验证码/页面稳定性或 browser-act 单步错误后重新运行。")
            return "\n".join(L)

        submitted = self.submit_data["submitted"]
        skipped = self.submit_data["skipped"]
        failed = self.submit_data["failed"]
        # 2026-08 新增：筛选条件翻页扫的结果并入报告，避免"做了但报告里看不到"
        g_sub = self.guide_data.get("submitted") or []
        g_skip = self.guide_data.get("skipped") or []
        g_fail = self.guide_data.get("failed") or []
        if g_sub:
            submitted = submitted + g_sub
        if g_skip:
            skipped = skipped + g_skip
        if g_fail:
            failed = failed + g_fail

        L.append("一、成功提交")
        if submitted:
            for i, item in enumerate(submitted, 1):
                L.append("%d. SKC：%s" % (i, item.get("skc", "")))
                L.append("   SKU：%s" % item.get("sku", ""))
                L.append("   备货范围：%s" % item.get("range", ""))
                L.append("   填写数量：%s" % item.get("quantity", ""))
                # 一个 SKC 多 SKU 时逐 SKU 列出填写量
                sku_details = item.get("skus") or []
                if item.get("skuCount") and int(item["skuCount"]) > 1 and len(sku_details) > 1:
                    L.append("   SKU 数：%s（逐 SKU 按下限最大值填写）" % item["skuCount"])
                    for sd in sku_details:
                        L.append("   - SKU：%s  数量：%s"
                                 % (sd.get("sku") or "未识别", sd.get("qty")))
                L.append("   页面提示：%s" % item.get("result", ""))
        else:
            L.append("无")
        L.append("")

        L.append("二、跳过商品")
        if skipped:
            for i, item in enumerate(skipped, 1):
                L.append("%d. SKC：%s" % (i, item.get("skc", "")))
                L.append("   SKU：%s" % item.get("sku", ""))
                L.append("   原因：%s" % item.get("reason", ""))
        else:
            L.append("无")
        L.append("")

        L.append("三、失败商品")
        if failed:
            for i, item in enumerate(failed, 1):
                L.append("%d. SKC：%s" % (i, item.get("skc", "")))
                L.append("   SKU：%s" % item.get("sku", ""))
                L.append("   失败原因：%s" % item.get("reason", ""))
        else:
            L.append("无")
        L.append("")

        L.append("四、最终复查结果")
        has_unhandled = False
        for entry in self.review:
            total = entry.get("total")
            total = "未知" if total is None else total
            # 字段名是 unhandledAll
            unhandled = entry.get("unhandledAll")
            unhandled = 0 if unhandled is None else int(unhandled)
            L.append("%s：%s 条，可处理/未处理项 %d 条"
                     % (entry.get("filter", ""), total, unhandled))
            if unhandled > 0:
                has_unhandled = True
                for item in entry.get("items", []):
                    if item.get("need") and not item.get("skip"):
                        L.append("- SKC：%s" % (item.get("skc") or "未识别"))
                        L.append("  SKU：%s" % (item.get("sku") or "未识别"))

        if not has_unhandled:
            L.append("")
            L.append("结论：未发现可处理/未处理备货项。")

        # 未知库存类卡片醒目告警，防止 Temu 新增卡片后静默漏扫
        unknown = [c for c in self.unknown_cards if c.get("name")]
        if unknown:
            L.append("")
            L.append("⚠️⚠️ 警告：发现未纳入自动扫描的筛选卡片，可能漏备货 ⚠️⚠️")
            for c in unknown:
                cnt = "未知" if c.get("count") is None else c.get("count")
                L.append("- 未扫描卡片：%s（显示 %s 条）" % (c["name"], cnt))
            L.append("请人工点开以上卡片确认是否需要备货，并通知 AI 把新卡片加入筛选清单。")

        return "\n".join(L)

    # [readback-v1] 业务真伪判定器：成功与否只看真实业务结果，不看脚本自己跑完没
    def business_verdict(self):
        def _reason(x):
            if isinstance(x, dict):
                return str(x.get("reason") or "")
            return str(x)

        def _skc(x):
            if isinstance(x, dict):
                return str(x.get("skc") or "")
            return ""

        def bucket(name):
            rows = []
            for src in (self.submit_data or {}, self.guide_data or {}):
                v = src.get(name) or []
                if isinstance(v, list):
                    rows.extend([x for x in v if x is not None])
            return rows

        submitted = bucket("submitted")
        failed = bucket("failed")
        skipped = bucket("skipped")
        reject = [x for x in failed if "平台拒绝" in _reason(x)]
        unconfirmed = [x for x in failed if "平台拒绝" not in _reason(x)]
        ok = len(failed) == 0
        reason = ""
        if not ok:
            head = "；".join("%s/%s" % (_skc(x), _reason(x)) for x in failed[:3])
            reason = "failed=%d（平台拒绝=%d，提交未确认=%d）：%s" % (
                len(failed), len(reject), len(unconfirmed), head)
        return {
            "ok": ok,
            "submitted_count": len(submitted),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "reject_count": len(reject),
            "unconfirmed_count": len(unconfirmed),
            "reason": reason,
        }

    def save_snapshot(self, end, report_text, succeeded, error_message=""):
        snapshot = {
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "succeeded": succeeded,
            "startTime": self.started.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end.strftime("%Y-%m-%d %H:%M:%S"),
            "storeName": self.store_name,
            "automationName": AUTOMATION_NAME,
            "reportSessionName": REPORT_SESSION_NAME,
            "reviewOnly": False,
            "submitted": self.submit_data["submitted"],
            "skipped": self.submit_data["skipped"],
            "failed": self.submit_data["failed"],
            "review": self.review,
            "reportText": report_text,
            "errorMessage": error_message,
            "logPath": "",
        }
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(self.snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    def send_feishu_text(self, text):
        if not self.send_feishu or not FEISHU_WEBHOOK:
            return
        tpc.send_feishu(FEISHU_WEBHOOK, [text])
        print("Final daily report sent to Feishu.")

    def _attach_readback_snapshot(self, rb_snap, ok_final):
        """把平台对账结果并进小票（restock_latest.json 的 readback 段）。

        save_snapshot 的旧字段保持不动（bat / 面板还在读它），只**追加** readback 段：
        这样"脚本自报"和"平台事实"并排躺在一张票里，以后核验不用二选一，
        也不会因为新增字段把老读取方打挂。
        """
        try:
            with open(self.snapshot_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            snap["readback"] = rb_snap
            snap["readback_ok"] = bool(ok_final)
            snap["readback_state"] = "platform_readback" if ok_final else "readback_failed"
            if self.readback_errors:
                snap["readback_errors"] = self.readback_errors
            with open(self.snapshot_path, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
            print("[readback] 已写入小票 readback 段: " + self.snapshot_path)
        except Exception as e:
            print("[readback] 写小票 readback 段失败: %s" % e)
            self.readback_errors.append("attach 小票失败: %s" % e)

    # ------------------------------------------------------------ 收尾：enrich + 标签（仅正式运行，对齐 worker）
    def run_post_success(self):
        if self.trial:
            # 试用：不 enrich、不打印、不触碰 bat 的 restock_latest.json
            print("[trial] skip enrich / label print; product: " + self.snapshot_path)
            return
        # 与 一键运行备货.ps1 worker 一致：先 enrich（只读，失败不阻塞），再打印标签
        # 2026-10-11：enrich 输出必须落盘——之前 capture_output 吞掉，失败时零痕迹可查
        enrich = os.path.join(RESTOCK_PS_DIR, "enrich-restock-onsale.ps1")
        if os.path.exists(enrich):
            try:
                r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy",
                                    "Bypass", "-File", enrich],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=900)
                try:
                    logp = os.path.join(LOGS_DIR, "enrich_last_run.log")
                    with open(logp, "w", encoding="utf-8") as f:
                        f.write("== enrich run %s (rc=%s) ==\n" % (
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r.returncode))
                        f.write("--- stdout ---\n%s\n" % (r.stdout or ""))
                        f.write("--- stderr ---\n%s\n" % (r.stderr or ""))
                except Exception as le:
                    print("enrich log write failed (ignored): %s" % le)
                print("enrich done (rc=%s), output -> logs/enrich_last_run.log" % r.returncode)
            except Exception as e:
                print("enrich failed (ignored): %s" % e)
        # 2026-11-04 用户要求：备货步收尾不再用标签机打印标签（原 --no-print 逻辑常开）
        print("[print] label print disabled (user 2026-11-04): skip label print")

    # ------------------------------------------------------------ 主流程
    def run(self):
        self.store_name = SHOP_NAME
        # 【只读预览模式】改补扫/判定逻辑前先跑这个：--preview
        # 只设筛选 + 点查询 + dump 页面结构，绝不点任何申请/确认按钮，跑完就退出。
        if os.environ.get("TEMU_GUIDE_PREVIEW") == "1":
            try:
                self.tb.open()
                self.tb.wait_stable(60)
                self.ensure_logged_in()
                self.ensure_target_store()
                self.tb.evaljs(CLEAR_OVERLAYS_JS)
                self.tb.wait_stable(15)
                self.preview_lines()
            except Exception as e:
                print("PREVIEW FAILED: %s" % e)
            finally:
                try:
                    self.tb.close()
                except Exception:
                    pass
            return 0
        try:
            # 1) 打开本脚本自有的 browser-act 会话（公共库，含重连/标签清理）
            if self._stage_enabled("open"):
                self.stage = "open browser-act session"
                self.tb.open()
                self.tb.wait_stable(60)
            else:
                self._skip_stage("open")

            # 2) 登录守卫（auth 地区页→商家中心→销售管理页）
            if self._stage_enabled("login"):
                self.stage = "login guard"
                self.ensure_logged_in()
            else:
                self._skip_stage("login")

            # 3) 确认/切换目标店铺
            if self._stage_enabled("store"):
                self.stage = "confirm target store"
                self.ensure_target_store()
            else:
                self._skip_stage("store")

            # 4) 清理残留遮挡弹窗
            if self._stage_enabled("clear_overlays"):
                self.stage = "clear blocking overlays"
                self.tb.evaljs(CLEAR_OVERLAYS_JS)
                self.tb.wait_stable(15)
            else:
                self._skip_stage("clear_overlays")

            # 4.5) [readback-v2] 基线必须在 review 之前取。
            #   review 段会「清空快速筛选卡」重置前端状态，卡片上的待备货条数随之归零，
            #   在 review 之后取 before 会稳定读到 0（2026-10-31 实测：before=0 / after=3，
            #   金等式算出 list_decreased=-3 冤枉报红）。此时页面刚登录完、是最干净的状态。
            self.rb_before = self._probe(
                "pending_before", self.rb.probe_pending_before if self.rb else (lambda: None))

            # 5) 提交前快速筛选复查（只读，按配置筛选卡逐页收集 need/skip）
            if self._stage_enabled("review"):
                self.stage = "quick-filter review before submit"
                if self.trial:
                    print("Running quick-filter review (dry-run: nothing will be submitted).")
                else:
                    print("Running quick-filter review. Processable items will be submitted.")
                self.run_review_js()
                print(json.dumps({"reports_summary": [
                    {"filter": e.get("filter"), "total": e.get("total"),
                     "unhandledAll": e.get("unhandledAll")} for e in self.review],
                    "unknownCards": self.unknown_cards}, ensure_ascii=False))
            else:
                self._skip_stage("review")

            # 6) 逐卡片翻页提交可处理备货项（SKC 去重、逐 SKU 填范围上限）
            if self._stage_enabled("submit"):
                self.stage = "submit restock applications"
                print("Submitting processable restock items found by quick filters.")
                if SUBMIT_LIMIT > 0:
                    print("[gray-scale] 灰度闸打开：本次最多真提交 %d 条（TEMU_RESTOCK_MAX_SUBMIT）"
                          % SUBMIT_LIMIT)
                js = tpc.apply_dry_run(SUBMIT_JS_TPL, self.trial).replace(
                    "FILTERS_PLACEHOLDER", json.dumps(FILTERS, ensure_ascii=False)).replace(
                    "SUBMIT_LIMIT_PLACEHOLDER", str(SUBMIT_LIMIT))
                self.submit_data = self.decode_b64_json(self.tb.evaljs(js))
                # 幂等落账：主提交段行内已确认「今日已备货」的才计 submitted，逐条落账（审计+防补扫重复）
                for _s in (self.submit_data or {}).get("submitted") or []:
                    if _s.get("skc"):
                        idem_mark(_s["skc"], meta={"via": "submit", "result": _s.get("result")})
                print(json.dumps(self.submit_data, ensure_ascii=False))
            else:
                self._skip_stage("submit")

            # 6.5) 按筛选条件过滤后翻页扫「建议备货引导」列。
            #      guide_scan=false 与既有 TEMU_SKIP_GUIDE_SCAN=1 使用相同跳过语义。
            self.guide_data = {"submitted": [], "skipped": [], "failed": [], "planned": [],
                               "stageLog": [], "filteredTotal": None}
            if not self._stage_enabled("guide_scan"):
                print("[guide-scan] skipped (step1_ops stages.guide_scan=false)")
            elif os.environ.get("TEMU_SKIP_GUIDE_SCAN") == "1":
                print("[guide-scan] skipped (TEMU_SKIP_GUIDE_SCAN=1)")
            else:
                self.stage = "guide scan by filter conditions"
                print("Guide scan: applying filter conditions, then paging through "
                      "to submit SKUs whose 建议备货引导 is 库存安全/可备货/库存偏低/建议备货/库存告急/建议立刻备货.")
                try:
                    self.guide_data = self.run_guide_scan()
                except Exception as e:
                    # 清快速筛选卡/设筛选条件失败＝硬失败：向上抛给 run() 兜底（退出码1）
                    if "快速筛选卡" in str(e) or "设筛选条件失败" in str(e):
                        raise
                    # 补扫失败不得让整条流程崩掉：登记失败并继续走最终复查/报告
                    self.guide_data = {"submitted": [], "skipped": [], "failed": [],
                                       "planned": [], "stageLog": ["guide-scan 执行异常: %s" % e],
                                       "filteredTotal": None}
                    print("[guide-scan] FAILED (ignored, continue): %s" % e)
                print(json.dumps(self.guide_data, ensure_ascii=False))

            # 7) 提交后最终复查
            if self._stage_enabled("final_review"):
                self.stage = "final review after submit"
                print("Running final review after submit actions.")
                self.run_review_js()
            else:
                self._skip_stage("final_review")

            # 7.5) [readback-v2] 第2道：提交后平台侧回读
            #   必须在 submit + guide_scan 全部做完之后取，否则 C 段差值不含补扫那部分。
            if self._readback_enabled():
                self.stage = "platform readback after submit"
                self.rb_after = self._probe(
                    "pending_after", self.rb.probe_pending_after if self.rb else (lambda: None))
                self.rb_new = self._probe(
                    "platform_new_orders",
                    self.rb.probe_platform_new_orders if self.rb else (lambda: None))
            else:
                print("[readback] skipped (TEMU_RESTOCK_READBACK=0)：本次无法对账，将判失败")

            # 8) 报告 + 快照（原生产物路径）
            verdict = None
            if self._stage_enabled("report"):
                self.stage = "write report"
                end = datetime.now()
                verdict = self.business_verdict()
                # [readback-v2] 只有平台侧对账也过了，才算真成功。
                # 脚本自报 failed=0 只说明"前端没报错"，不说明平台真收了单。
                rb_ok, rb_reason, rb_snap = self._run_platform_readback(verdict)
                ok_final = bool(verdict["ok"]) and bool(rb_ok)
                report = self.format_report(end, ok_final)
                report += "\n[business] submitted=%d failed=%d rejected=%d unconfirmed=%d skipped=%d" % (
                    verdict["submitted_count"], verdict["failed_count"],
                    verdict["reject_count"], verdict["unconfirmed_count"],
                    verdict["skipped_count"])
                if not verdict["ok"]:
                    report += "\n[business] BUSINESS_FAIL: " + verdict["reason"]
                if rb_snap:
                    report += ("\n[readback] A(声称提交)=%s B(平台新增单)=%s C(列表减少)=%s -> %s"
                               % (rb_snap.get("submitted_count"),
                                  rb_snap.get("platform_new_orders"),
                                  rb_snap.get("list_decreased"),
                                  "对账通过" if rb_ok else "对账不符"))
                if not rb_ok:
                    report += "\n[readback] READBACK_FAIL: " + rb_reason
                self.save_snapshot(end, report, ok_final,
                                   (verdict["reason"] if not verdict["ok"] else "")
                                   or (rb_reason if not rb_ok else ""))
                if rb_snap:
                    self._attach_readback_snapshot(rb_snap, ok_final)
                print("")
                print(report)
                self.send_feishu_text(report)
                verdict = dict(verdict)
                verdict["ok"] = ok_final
                verdict["readback_ok"] = bool(rb_ok)
                verdict["readback_reason"] = rb_reason
            else:
                self._skip_stage("report")

            # 9) 关闭本脚本创建的会话不属于可配置业务阶段，始终执行清理
            close_msg = self.tb.close()
            print(close_msg)

            # 10) 成功后 enrich + 标签（正式运行；试用全跳过）
            if self._stage_enabled("post"):
                self.stage = "post success enrich/tags"
                self.run_post_success()
            else:
                self._skip_stage("post")
            print("Done. Snapshot: " + self.snapshot_path)
            if verdict is not None and not verdict["ok"]:
                print("[business] EXIT_CODE=2 reason=business_fail")
                return 2
            return 0
        except Exception as e:
            err = str(e)
            print("ERROR: " + err)
            try:
                if self._stage_enabled("report"):
                    page_state = "unavailable"
                    try:
                        page_state = self.tb.evaljs_fast(
                            "(() => location.href + ' | ' + document.title)()")
                    except Exception:
                        pass
                    end = datetime.now()
                    fail_report = self.format_report(
                        end, False, error_message=err, failure_page_state=page_state)
                    self.save_snapshot(end, fail_report, False, err)
                    self.send_feishu_text(fail_report)
                else:
                    self._skip_stage("report")
            except Exception as e2:
                print("Failure report failed: %s" % e2)
            try:
                print(self.tb.close())
            except Exception:
                pass
            return 1


# --------------------------------------------------------------------------- 菜单 / CLI
def build(trial=False, send_feishu=True, do_print=True):
    return RestockEngine(trial=trial, send_feishu=send_feishu, do_print=do_print)


if __name__ == "__main__":
    CliApp = tpc.CliApp("01", "备货", build)
    CliApp.main()
