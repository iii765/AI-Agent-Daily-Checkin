#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国内主流 AI Coding / Agent 桌面客户端每日自动签到领积分工具
已支持：MiniMax Code · WorkBuddy · TRAE CN · Qoder CN · 豆包 (Doubao)
架构：Playwright CDP 挂载 + 内部 IPC/RPC 直调（主力 L1） + DOM 按钮点击（兜底 L2）
支持跨盘符自动寻路，纯 Win32 原生气泡通知（免杀软误报），开箱即用，开源发布。
"""

import os
import sys
import json
import time
import logging
import argparse
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

import httpx
from playwright.async_api import async_playwright, Page

# 确保本地 CDP 连接不受系统代理（如 Clash/v2ray）干扰
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_FILE = LOG_DIR / "checkin_history.json"
LOG_FILE = LOG_DIR / "checkin.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("auto_checkin")

APP_CONFIGS = {
    "minimax_code": {
        "name": "MiniMax Code",
        "rel_paths": [
            r"Programs\MiniMax Code\MiniMax Code.exe",
            r"MiniMax Code\MiniMax Code.exe",
        ],
        "port": 19221,
        "process_name": "MiniMax Code.exe",
        "url_keywords": ["archon"],
    },
    "workbuddy": {
        "name": "WorkBuddy",
        "rel_paths": [
            r"Programs\WorkBuddy\WorkBuddy.exe",
            r"WorkBuddy\WorkBuddy.exe",
        ],
        "port": 19222,
        "process_name": "WorkBuddy.exe",
        "url_keywords": ["renderer/index.html", "workbuddy"],
    },
    "trae_cn": {
        "name": "TRAE CN",
        "rel_paths": [
            r"Programs\Trae CN\Trae CN.exe",
            r"Trae CN\Trae CN.exe",
            r"Programs\Trae\Trae.exe",
        ],
        "port": 19223,
        "process_name": "Trae CN.exe",
        "url_keywords": ["solo-lite.html", "workbench.html"],
    },
    "qoder_cn": {
        "name": "Qoder CN",
        "rel_paths": [
            r"Programs\Qoder CN\Qoder CN.exe",
            r"Qoder CN\Qoder CN.exe",
        ],
        "port": 19224,
        "process_name": "Qoder CN.exe",
        "url_keywords": ["qoder", "workbenchscope", "renderer"],
    },
    "doubao": {
        "name": "豆包",
        "rel_paths": [
            r"Doubao\Doubao.exe",
            r"Programs\Doubao\Doubao.exe",
        ],
        "port": 19225,
        "process_name": "Doubao.exe",
        "url_keywords": ["doubao-chat/chat", "doubao-chat"],
    },
}


def resolve_app_exe(cfg: Dict[str, Any]) -> Optional[str]:
    """跨盘符与环境变量自动探测客户端安装路径（支持自定义盘符安装，适合开源通用）"""
    # 1. 支持通过环境变量覆盖
    env_key = f"CHECKIN_EXE_{cfg['name'].upper().replace(' ', '_')}"
    if os.environ.get(env_key) and Path(os.environ[env_key]).exists():
        return os.environ[env_key]

    username = os.environ.get("USERNAME") or Path.home().name
    candidate_roots: List[Path] = []
    if os.environ.get("LOCALAPPDATA"):
        candidate_roots.append(Path(os.environ["LOCALAPPDATA"]))
    if os.environ.get("PROGRAMFILES"):
        candidate_roots.append(Path(os.environ["PROGRAMFILES"]))

    # 扫描 C/D/E/F/G 盘下的 Users\<user>\AppData\Local 与 Program Files
    for drive in ["C", "D", "E", "F", "G"]:
        candidate_roots.append(Path(f"{drive}:/Users/{username}/AppData/Local"))
        candidate_roots.append(Path(f"{drive}:/Program Files"))
        candidate_roots.append(Path(f"{drive}:/"))

    for root in candidate_roots:
        for rel in cfg.get("rel_paths", []):
            p = root / rel
            if p.exists():
                return str(p)
    return None


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_process_running(exe_name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).decode("gbk", errors="ignore")
        return exe_name.lower() in out.lower()
    except Exception:
        return False


def launch_electron_minimized(exe_path: str, port: int) -> subprocess.Popen:
    """以最小化且不抢占焦点（SW_SHOWMINNOACTIVE = 7）的方式启动客户端并开启 CDP 端口"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7  # SW_SHOWMINNOACTIVE: 最小化启动，不打断当前鼠标/键盘焦点
    return subprocess.Popen(
        [exe_path, f"--remote-debugging-port={port}"],
        startupinfo=si,
    )


def kill_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def kill_temp_app_processes(pid: Optional[int], proc_name: str) -> None:
    """彻底清理脚本临时拉起的客户端进程树。
    针对多层 Launcher Stub 架构应用（如 豆包 Doubao.exe 启动器拉起 app\\Doubao.exe 后即自行退出），
    在按 PID 杀树后按进程映像名兜底强杀残留子进程树，确保签完即走不留后台/桌面窗口。
    """
    if pid:
        kill_process_tree(pid)
        time.sleep(0.3)

    if proc_name and is_process_running(proc_name):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", proc_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass


async def wait_for_cdp(port: int, url_keywords: list[str], timeout_sec: int = 25) -> bool:
    client = httpx.Client(trust_env=False, timeout=1.5)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            resp = client.get(f"http://127.0.0.1:{port}/json")
            targets = resp.json()
            for t in targets:
                u = (t.get("url") or "").lower()
                title = (t.get("title") or "").lower()
                if any(kw.lower() in u or kw.lower() in title for kw in url_keywords):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.8)
    return False


async def find_target_page(browser, url_keywords: list[str]) -> Optional[Page]:
    # 轮询等待真正的主窗口渲染页面就绪（排除后台 background 服务页）
    for _ in range(15):
        for ctx in browser.contexts:
            for pg in ctx.pages:
                u = pg.url.lower()
                if "background" in u:
                    continue
                if any(kw.lower() in u for kw in url_keywords):
                    return pg
        await asyncio.sleep(0.8)

    for ctx in browser.contexts:
        for pg in ctx.pages:
            if "background" not in pg.url.lower():
                return pg

    if browser.contexts and browser.contexts[0].pages:
        return browser.contexts[0].pages[0]
    return None


# ============================================================================
# 1. MiniMax Code 签到实现（L1: Webpack Axios 实例直调 + L2: DOM 按钮点击兜底）
# ============================================================================
async def checkin_minimax_code(page: Page) -> Dict[str, Any]:
    for _ in range(25):
        try:
            has_wp = await page.evaluate("() => Array.isArray(window.webpackChunk_N_E) && window.webpackChunk_N_E.length > 0")
            if has_wp:
                break
        except Exception:
            pass
        await asyncio.sleep(0.8)
    await asyncio.sleep(2.5)

    # L1: 直接通过内部已鉴权带签名的 Axios (module 98304) 调用签到接口
    try:
        res = await page.evaluate(
            """async () => {
            if (!Array.isArray(window.webpackChunk_N_E)) {
                return { ok: false, error: 'webpackChunk_N_E 未就绪' };
            }
            let wpReq;
            window.webpackChunk_N_E.push([[Symbol()], {}, r => { wpReq = r; }]);
            if (!wpReq) {
                return { ok: false, error: '获取 webpack require 失败' };
            }
            const http = wpReq(98304).ZP;
            const statusResp = await http.get('/minimax-cloud/api/v1/signin/status', { showToast: false });
            const data = statusResp.data?.data;
            const todayInfo = data?.days?.find(d => d.is_today) || data?.days?.find(d => d.status === 2);
            const claimable = data?.days?.some(d => d.status === 2);
            const claimedToday = data?.days?.some(d => d.is_today && d.status === 3);

            if (claimedToday && !claimable) {
                return {
                    ok: true,
                    already_claimed: true,
                    points: todayInfo?.points || 0,
                    day_no: todayInfo?.day_no || 0,
                    method: 'L1_WEBPACK_API'
                };
            }
            if (claimable) {
                const claimResp = await http.post('/minimax-cloud/api/v1/signin/claim', {}, { showToast: false });
                const cData = claimResp.data?.data;
                return {
                    ok: true,
                    already_claimed: false,
                    points: cData?.points || todayInfo?.points || 0,
                    day_no: cData?.day_no || todayInfo?.day_no || 0,
                    method: 'L1_WEBPACK_API'
                };
            }
            return { ok: false, error: '未找到可签到状态', raw: statusResp.data };
        }"""
        )
        if res and res.get("ok"):
            return res
    except Exception as e:
        logger.warning(f"[MiniMax Code] L1 接口直调异常，切换 L2 DOM 点击兜底: {e}")

    # L2: Playwright DOM 选择器点击兜底
    btn = page.locator("button.signin-claim-button")
    if await btn.count() > 0:
        await btn.first.click()
        await asyncio.sleep(2)
        return {"ok": True, "already_claimed": False, "points": 0, "method": "L2_DOM_CLICK"}

    return {"ok": False, "error": "L1 与 L2 均未完成签到"}


# ============================================================================
# 2. WorkBuddy 签到实现（L1: Preload __wbInvoke IPC 直调 + L2: DOM 按钮点击兜底）
# ============================================================================
async def checkin_workbuddy(page: Page) -> Dict[str, Any]:
    for _ in range(20):
        has_bridge = await page.evaluate("() => typeof window.__wbInvoke === 'function'")
        if has_bridge:
            break
        await asyncio.sleep(0.8)
    await asyncio.sleep(2.5)

    # L1: 直接调用 Electron Preload 暴露的 __wbInvoke('auth:claimDailyCheckin')
    try:
        res = await page.evaluate(
            """async () => {
            const status = await window.__wbInvoke('auth:getCheckinStatus');
            if (!status) {
                return { ok: false, error: '获取签到状态为空' };
            }
            if (status.today_checked_in) {
                return {
                    ok: true,
                    already_claimed: true,
                    points: status.today_credit || status.daily_credit || 100,
                    streak_days: status.streak_days || 0,
                    total_credits: status.total_credits || 0,
                    method: 'L1_WB_IPC'
                };
            }
            const claim = await window.__wbInvoke('auth:claimDailyCheckin');
            if (claim && (claim.credit || claim.streak_days)) {
                return {
                    ok: true,
                    already_claimed: false,
                    points: claim.credit || status.today_credit || 100,
                    streak_days: claim.streak_days || (status.streak_days + 1),
                    method: 'L1_WB_IPC'
                };
            }
            return { ok: false, error: claim?.msg || '签到返回异常', raw: claim };
        }"""
        )
        if res and res.get("ok"):
            return res
    except Exception as e:
        logger.warning(f"[WorkBuddy] L1 IPC 直调异常，切换 L2 DOM 点击兜底: {e}")

    # L2: Playwright DOM 点击兜底
    claim_btn = page.locator("button:has-text('立即领取'), button:has-text('签到领积分')")
    if await claim_btn.count() > 0:
        await claim_btn.first.click()
        await asyncio.sleep(2)
        return {"ok": True, "already_claimed": False, "points": 100, "method": "L2_DOM_CLICK"}

    return {"ok": False, "error": "L1 与 L2 均未完成签到"}


# ============================================================================
# 3. TRAE CN 签到实现（L1: vscode.ipcRenderer 沙盒通道直调 + 抽奖检测）
# ============================================================================
async def checkin_trae_cn(page: Page) -> Dict[str, Any]:
    for _ in range(25):
        has_ipc = await page.evaluate("() => Boolean(window.vscode?.ipcRenderer?.invoke)")
        if has_ipc:
            break
        await asyncio.sleep(0.8)
    await asyncio.sleep(3.5)

    try:
        res = await page.evaluate(
            """async () => {
            const ipc = window.vscode.ipcRenderer;
            const statusResp = await ipc.invoke('vscode:sandbox::main-invoke-fetchCheckinCreditsStatus');
            const sData = statusResp?.data;
            if (!sData) {
                return { ok: false, error: '获取 TRAE 签到状态为空', raw: statusResp };
            }
            const totalPts = (sData.credits || 0) + (sData.extra_credits || 0);
            if (sData.checked_in || sData.did_checked_in) {
                return {
                    ok: true,
                    already_claimed: true,
                    points: totalPts,
                    credits: sData.credits || 0,
                    extra_credits: sData.extra_credits || 0,
                    method: 'L1_VSCODE_IPC'
                };
            }
            if (sData.enable) {
                const claimResp = await ipc.invoke('vscode:sandbox::main-invoke-claimCheckinCredits', 'account_menu');
                const cCode = claimResp?.data?.code;
                // 0 表示成功，9095 表示该设备今日已参与签到
                if (claimResp?.status === 200 && (cCode === 0 || cCode === 9095)) {
                    // 同时检查是否有签到抽奖活动可参与
                    let lotteryRes = null;
                    try {
                        const lot = await ipc.invoke('vscode:sandbox::main-invoke-getCheckinLottery');
                        if (lot?.state?.kind === 'ready' && lot?.state?.eligible) {
                            lotteryRes = await ipc.invoke('vscode:sandbox::main-invoke-participateCheckinLottery', 'account_menu');
                        }
                    } catch (e) {}
                    return {
                        ok: true,
                        already_claimed: cCode === 9095,
                        points: totalPts,
                        credits: sData.credits || 0,
                        extra_credits: sData.extra_credits || 0,
                        lottery: lotteryRes,
                        method: 'L1_VSCODE_IPC'
                    };
                }
                return { ok: false, error: claimResp?.data?.message || `错误码 ${cCode}`, raw: claimResp };
            }
            return { ok: false, error: '当前账号未开启签到活动', raw: sData };
        }"""
        )
        if res and res.get("ok"):
            return res
        return res or {"ok": False, "error": "未知响应"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# 4. Qoder CN 签到实现（L1: Preload qoderDesktop + 权益中心 Surface 直达）
# ============================================================================
async def checkin_qoder_cn(page: Page) -> Dict[str, Any]:
    for _ in range(25):
        has_qd = await page.evaluate("() => Boolean(window.qoderDesktop?.getCampaignStatus)")
        if has_qd:
            break
        await asyncio.sleep(0.8)
    await asyncio.sleep(2.0)

    # 读取当前活动状态与账户已分配额度
    status = await page.evaluate(
        """async () => {
        try {
            const camp = await window.qoderDesktop.getCampaignStatus();
            const usage = await window.qoderDesktop.getAccountUsage();
            return { ok: true, camp, usage };
        } catch (e) {
            return { ok: false, error: e.message };
        }
    }"""
    )

    has_claimable = status.get("camp", {}).get("hasClaimableCampaign", False)
    credits_pts = status.get("usage", {}).get("qoderUsage", {}).get("addOnQuota", {}).get("remaining", 100)

    # 尝试触发打开用户菜单 -> 权益中心
    try:
        user_menu = page.locator('button[aria-label="打开用户菜单"]')
        if await user_menu.count() == 0:
            user_menu = page.locator('button:has([class*="avatar"]), div[class*="avatar"]')

        if await user_menu.count() > 0:
            await user_menu.first.click()
            await asyncio.sleep(1.2)

            rewards_item = page.locator("text='权益中心'")
            if await rewards_item.count() > 0:
                await rewards_item.first.click()
                await asyncio.sleep(2.5)
    except Exception as e:
        logger.warning(f"[Qoder CN] 打开权益中心菜单异常: {e}")

    # 查找权益中心 iframe
    iframe = None
    for _ in range(12):
        for f in page.frames:
            if "activity-iframe" in f.url:
                iframe = f
                break
        if iframe:
            break
        await asyncio.sleep(0.5)

    if iframe:
        try:
            claim_btns = iframe.locator('button:has-text("立即领取"), button:has-text("领取")')
            btn_cnt = await claim_btns.count()
            claimed_now = False
            for i in range(btn_cnt):
                btn = claim_btns.nth(i)
                txt = (await btn.inner_text()).strip()
                if txt in ["立即领取", "领取"] and not await btn.is_disabled():
                    logger.info(f"[Qoder CN] 发现可领取按钮 [{txt}]，正在点击...")
                    await btn.click()
                    claimed_now = True
                    await asyncio.sleep(2.0)
                    break

            iframe_text = await iframe.evaluate("() => document.body.innerText")
            is_claimed = "已领取" in iframe_text or not has_claimable

            # 按 Escape 平滑关闭弹窗
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            return {
                "ok": True,
                "already_claimed": not claimed_now and is_claimed,
                "points": credits_pts or 100,
                "method": "L1_QODER_SURFACE",
            }
        except Exception as e:
            logger.warning(f"[Qoder CN] 权益中心 iframe 操作异常: {e}")

    if status.get("ok"):
        return {
            "ok": True,
            "already_claimed": not has_claimable,
            "points": credits_pts or 100,
            "method": "L1_QODER_DESKTOP",
        }

    return {"ok": False, "error": status.get("error", "未找到 Qoder 签到入口")}


# ============================================================================
# 5. 豆包 (Doubao) 签到实现（L1: 任务专区页面一键签到 + Webpack API 状态校验）
# ============================================================================
async def checkin_doubao(page: Page) -> Dict[str, Any]:
    # 豆包桌面客户端基于 Chromium，在同一会话下打开任务专区执行无感签到
    ctx = page.context
    explore_page = await ctx.new_page()
    try:
        await explore_page.goto("https://www.doubao.com/pc/agent-explore?scroll_to=tasks", timeout=25000)

        # 轮询等待任务专区页面渲染完成（从服务端拉取任务列表）
        for _ in range(15):
            try:
                has_content = await explore_page.evaluate(
                    "() => document.body.innerText.includes('每日签到') || document.body.innerText.includes('已签到') || document.body.innerText.includes('算力')"
                )
                if has_content:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.8)
        await asyncio.sleep(1.5)

        # 查找每日签到按钮
        claimed_now = False
        checkin_btn = explore_page.locator("button:has-text('签到'), button:has-text('去签到')")
        btn_count = await checkin_btn.count()

        for i in range(btn_count):
            b = checkin_btn.nth(i)
            txt = (await b.inner_text()).strip()
            if "已签" not in txt and "签到" in txt and not await b.is_disabled():
                logger.info(f"[豆包] 发现未签到任务按钮 [{txt}]，正在点击签到...")
                await b.click()
                claimed_now = True
                await asyncio.sleep(2.5)
                break

        page_text = await explore_page.evaluate("() => document.body.innerText")
        is_checked_in = "已签到" in page_text or claimed_now or "签到成功" in page_text or "每日签到" in page_text

        if is_checked_in:
            return {
                "ok": True,
                "already_claimed": not claimed_now,
                "points": 200,
                "method": "L1_DOUBAO_EXPLORE",
            }

        # 兜底：通过内置 Webpack 模块检验签到状态
        try:
            res = await page.evaluate(
                """async () => {
                let webpackReq;
                const w = window["@flow-web/desktop:stable"];
                if (Array.isArray(w)) {
                    w.push([[Symbol()], {}, r => { webpackReq = r; }]);
                }
                if (!webpackReq) return null;
                const ugApi = webpackReq(534252);
                if (!ugApi?.k) return null;
                const lp = await ugApi.k.GetLandingPage({ scene: 'pc_incentive_task_mvp_v3' });
                return lp?.data;
            }"""
            )
            if res:
                chosen = res.get("chosen_type")
                return {
                    "ok": True,
                    "already_claimed": chosen == 2,
                    "points": 200,
                    "method": "L1_DOUBAO_API",
                }
        except Exception:
            pass

        return {"ok": False, "error": "未找到豆包签到状态或任务已下线"}
    finally:
        try:
            await explore_page.close()
        except Exception:
            pass


HANDLERS = {
    "minimax_code": checkin_minimax_code,
    "workbuddy": checkin_workbuddy,
    "trae_cn": checkin_trae_cn,
    "qoder_cn": checkin_qoder_cn,
    "doubao": checkin_doubao,
}


async def run_single_app(app_key: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    name = cfg["name"]
    exe = resolve_app_exe(cfg)
    port = cfg["port"]
    proc_name = cfg["process_name"]

    if not exe:
        logger.warning(f"[{name}] 未探测到客户端安装路径，跳过")
        return {"ok": False, "error": "客户端未安装"}

    already_running = is_process_running(proc_name)
    proc = None

    # 先探测是否已经开启了 CDP 端口（若通过桌面快捷方式打开，端口已就绪，无需重启直接无感附着！）
    cdp_ready = await wait_for_cdp(port, cfg["url_keywords"], timeout_sec=2)

    if not cdp_ready:
        if already_running:
            logger.info(f"[{name}] 检测到客户端已在运行但未开启调试端口，正在重启以挂载签到通道...")
            subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await asyncio.sleep(1.5)
            already_running = False

        logger.info(f"[{name}] 正在后台最小化启动 (CDP 端口: {port})...")
        proc = launch_electron_minimized(exe, port)
        cdp_ready = await wait_for_cdp(port, cfg["url_keywords"], timeout_sec=25)
    else:
        logger.info(f"[{name}] 检测到已开启 CDP 端口 ({port})，直接无感连接现有窗口...")

    if not cdp_ready:
        if proc and not already_running:
            kill_temp_app_processes(proc.pid, proc_name)
        return {"ok": False, "error": f"CDP 端口 {port} 连接超时"}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = await find_target_page(browser, cfg["url_keywords"])
            if not page:
                return {"ok": False, "error": "未找到有效渲染页面"}
            result = await HANDLERS[app_key](page)
            return result
    finally:
        # 如果原本未运行（由脚本临时拉起），签到完成后自动清理关闭进程；若用户本来就开着，则保持原样不关闭！
        if proc and not already_running:
            logger.info(f"[{name}] 签到流程结束，正在自动关闭临时客户端进程...")
            kill_temp_app_processes(proc.pid, proc_name)


def show_windows_notification(title: str, message: str) -> None:
    """使用纯 Python ctypes 直接调用 Windows 原生 Shell_NotifyIconW API 发送右下角气泡通知
    完全不调用 powershell.exe，100% 避免触发火绒/安全软件的「隐藏执行PowerShell」拦截规则！
    """
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004
        NIF_INFO = 0x00000010
        NIM_ADD = 0x00000000
        NIM_MODIFY = 0x00000001
        NIM_DELETE = 0x00000002
        NIIF_INFO = 0x00000001
        IDI_INFORMATION = 32516

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]

        # 创建隐藏消息窗口句柄承载托盘图标
        hinst = kernel32.GetModuleHandleW(None)
        hwnd = user32.CreateWindowExW(0, "STATIC", "CheckinNotify", 0, 0, 0, 0, 0, 0, 0, hinst, None)
        hicon = user32.LoadIconW(None, ctypes.cast(IDI_INFORMATION, wintypes.LPCWSTR))

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1001
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_INFO
        nid.hIcon = hicon
        nid.szTip = "AI 客户端自动签到"
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = NIIF_INFO
        nid.uTimeoutOrVersion = 5000

        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        time.sleep(5)
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        if hwnd:
            user32.DestroyWindow(hwnd)
    except Exception as e:
        logger.debug(f"发送原生气泡通知异常: {e}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="AI Agent 桌面应用每日自动签到工具")
    parser.add_argument("--force", action="store_true", help="忽略今日已签缓存，强制重新检查所有客户端")
    parser.add_argument("--app", choices=list(APP_CONFIGS.keys()), help="仅运行指定客户端")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    state = load_state()
    today_state = state.setdefault(today, {})

    target_keys = [args.app] if args.app else list(APP_CONFIGS.keys())
    summary_lines = []
    did_actual_work = False

    logger.info(f"========== 开始执行每日自动签到 ({today}) ==========")

    for key in target_keys:
        cfg = APP_CONFIGS[key]
        name = cfg["name"]

        if not args.force and today_state.get(key, {}).get("ok"):
            pts = today_state[key].get("points", 0)
            logger.info(f"[{name}] 今日记录已签到 (+{pts} 积分)，跳过启动（使用 --force 可强制复查）")
            summary_lines.append(f"✅ {name}: 今日已签 (+{pts} 积分)")
            continue

        did_actual_work = True
        res = await run_single_app(key, cfg)
        if res.get("ok"):
            pts = res.get("points", 0)
            status_tag = "今日已签" if res.get("already_claimed") else "刚刚签到成功"
            logger.info(f"[{name}] {status_tag}！获得积分: +{pts} (通道: {res.get('method')})")
            # 脱敏保存（过滤掉可能含账号 ID 的冗余字段，方便开源环境安全使用）
            safe_detail = {
                k: v for k, v in res.items() if k in ("ok", "already_claimed", "points", "day_no", "streak_days", "method")
            }
            today_state[key] = {
                "ok": True,
                "points": pts,
                "status": status_tag,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "detail": safe_detail,
            }
            summary_lines.append(f"✅ {name}: {status_tag} (+{pts} 积分)")
        else:
            err = res.get("error", "未知错误")
            logger.error(f"[{name}] 签到失败: {err}")
            today_state[key] = {
                "ok": False,
                "error": err,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
            summary_lines.append(f"❌ {name}: 失败 ({err})")

        save_state(state)

    summary_text = "\n".join(summary_lines)
    logger.info("========== 签到汇总 ==========\n" + summary_text)
    if did_actual_work or args.force:
        show_windows_notification("AI 客户端每日签到完成", summary_text)


if __name__ == "__main__":
    asyncio.run(main())
