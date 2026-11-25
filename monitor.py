import difflib
import hashlib
import json
import os
import re
import time
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

# ================= 配置区 =================
TARGETS = [
    {
        "name": "KOBROX 官网",
        "url": "https://www.kobrox.net",
        "required_emails": ["manager@kobrox.net"],  # 必须存在的邮箱，若丢失或出现其他未知邮箱立即告警
        "required_texts": [],                       # 必须存在的关键文字
        "check_qr": False,                          # 是否监控二维码图片
        "qr_selector": ""                           # 可选：二维码精准 CSS 选择器
    },
    {
        "name": "FAVILLA 官网",
        "url": "https://www.favillacigar.com",
        "required_emails": [],
        "required_texts": ["13587447654"],          # 必须存在的企业微信号
        "check_qr": True,                           # 开启二维码图片监控（校验图片地址+文件哈希）
        "qr_selector": ""                           # 留空自动识别含 qr/wechat/wx 等关键词的图片；也可填精准选择器
    }
    # 如果产品价格在子页面（例如 /products），可按上面格式再复制一段加在这里
]

STATE_FILE = "last_state.json"
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK")
QR_KEYWORDS = ("qr", "wechat", "weixin", "wx", "code", "码")
# ==========================================


def send_dingtalk(title, text):
    if not DINGTALK_WEBHOOK:
        print("错误：未读取到 DINGTALK_WEBHOOK 环境变量")
        return
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text
        }
    }
    resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=15)
    resp.raise_for_status()
    print("钉钉推送响应:", resp.text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_image_source(src: str) -> str:
    """下载图片并计算二进制哈希，防止只替换图片内容但文件名不变的情况"""
    if src.startswith("data:image"):
        return sha256_bytes(src.encode("utf-8"))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(src, headers=headers, timeout=15)
    resp.raise_for_status()
    return sha256_bytes(resp.content)


def fetch_site_data(browser, target):
    """使用无头浏览器访问网页，带 1 次失败重试，避免偶发网络波动误报"""
    url = target["url"]
    last_err = None

    for attempt in range(2):
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None:
                raise RuntimeError("服务器无响应")
            if response.status >= 400:
                raise RuntimeError(f"HTTP 状态码异常: {response.status}")

            # 等待前端脚本、价格、图片渲染完成
            page.wait_for_timeout(4000)

            visible_text = page.inner_text("body")
            raw_html = page.content()

            # 清理空行，提取用于对比的正文行
            lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
            normalized_text = "\n".join(lines)
            text_hash = sha256_bytes(normalized_text.encode("utf-8"))

            # 提取页面中的所有邮箱（正文 + HTML 中的 mailto）
            emails = sorted(set(
                e.lower() for e in re.findall(
                    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                    normalized_text + "\n" + raw_html
                )
            ))

            # 提取二维码图片信息
            qr_hashes = {}
            if target.get("check_qr"):
                selector = target.get("qr_selector") or "img"
                imgs = page.eval_on_selector_all(
                    selector,
                    """els => els.map(el => ({
                        src: el.getAttribute('src') || '',
                        alt: el.getAttribute('alt') || '',
                        cls: el.getAttribute('class') || ''
                    }))"""
                )
                for img in imgs:
                    src = img["src"].strip()
                    if not src:
                        continue
                    full_src = src if src.startswith("data:image") else urljoin(url, src)

                    # 如果未指定选择器，则按关键词筛选二维码图片
                    if not target.get("qr_selector"):
                        meta = f"{full_src} {img['alt']} {img['cls']}".lower()
                        if not any(k in meta for k in QR_KEYWORDS):
                            continue

                    try:
                        qr_hashes[full_src] = hash_image_source(full_src)
                    except Exception as img_err:
                        qr_hashes[full_src] = f"DOWNLOAD_ERROR:{img_err}"

            context.close()
            return {
                "text_hash": text_hash,
                "lines": lines,
                "emails": emails,
                "qr_hashes": qr_hashes
            }
        except Exception as e:
            last_err = e
            context.close()
            if attempt == 0:
                time.sleep(5)

    raise last_err


def get_text_diff(old_lines, new_lines, max_lines=12):
    """生成精简的文本变动对比，方便在钉钉里直接看改了什么"""
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))
    changes = [
        line for line in diff
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    ]
    if not changes:
        return ""
    preview = changes[:max_lines]
    if len(changes) > max_lines:
        preview.append(f"...（其余 {len(changes) - max_lines} 处变动已省略）")
    return "\n".join(f"> {c}" for c in preview)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    saved_state = load_state()
    critical_alerts = []  # 宕机、邮箱/微信号/二维码异常
    change_alerts = []    # 信息、价格变动
    init_reports = []     # 首次初始化报告
    state_updated = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for target in TARGETS:
            name = target["name"]
            url = target["url"]

            try:
                current = fetch_site_data(browser, target)
            except Exception as e:
                print(f"[{name}] 无法打开: {e}")
                critical_alerts.append(
                    f"#### ❌ {name} 无法打开\n"
                    f"- **网址**：{url}\n"
                    f"- **原因**：`{e}`"
                )
                continue

            # ---------- 1. 关键防篡改规则实时校验 ----------
            site_issues = []

            # 校验邮箱
            req_emails = [e.lower() for e in target.get("required_emails", [])]
            if req_emails:
                for req_email in req_emails:
                    if req_email not in current["emails"]:
                        site_issues.append(f"正确邮箱 `{req_email}` **已消失或被篡改**！当前检测到的邮箱：`{current['emails']}`")
                extra_emails = [e for e in current["emails"] if e not in req_emails]
                if extra_emails:
                    site_issues.append(f"发现异常/新增邮箱：`{extra_emails}`")

            # 校验指定文字（如企业微信号）
            full_text = "\n".join(current["lines"])
            for req_text in target.get("required_texts", []):
                if req_text not in full_text:
                    site_issues.append(f"关键信息 `{req_text}` **已消失或被篡改**！")

            # ---------- 2. 历史状态对比 ----------
            if url not in saved_state:
                saved_state[url] = current
                state_updated = True
                qr_info = f"识别到 {len(current['qr_hashes'])} 张二维码图片" if target.get("check_qr") else "未开启"
                init_reports.append(
                    f"- **{name}** ({url})\n"
                    f"  - 检测到邮箱：`{current['emails'] or '无'}`\n"
                    f"  - 二维码监控：{qr_info}"
                )
            else:
                prev = saved_state[url]

                # 对比二维码图片是否变动
                if target.get("check_qr"):
                    prev_qr = prev.get("qr_hashes", {})
                    curr_qr = current["qr_hashes"]
                    if curr_qr != prev_qr:
                        site_issues.append(
                            f"**二维码图片发生变化**！（原数量：{len(prev_qr)}，现数量：{len(curr_qr)}，图片链接或图像内容已被修改）"
                        )
                        saved_state[url]["qr_hashes"] = curr_qr
                        state_updated = True

                # 对比正文和价格变动
                if current["text_hash"] != prev.get("text_hash"):
                    diff_preview = get_text_diff(prev.get("lines", []), current["lines"])
                    change_alerts.append(
                        f"#### 📝 {name} 内容/价格变动\n"
                        f"- **网址**：[点击核对]({url})\n"
                        f"- **主要差异**（`-` 为旧内容，`+` 为新内容）：\n\n{diff_preview}"
                    )
                    saved_state[url]["text_hash"] = current["text_hash"]
                    saved_state[url]["lines"] = current["lines"]
                    saved_state[url]["emails"] = current["emails"]
                    state_updated = True
                else:
                    print(f"[{name}] 页面内容无变动。")

            if site_issues:
                issue_list = "\n".join(f"- {item}" for item in site_issues)
                critical_alerts.append(f"#### 🚨 {name} 关键信息异常\n- **网址**：[点击核对]({url})\n{issue_list}")

        browser.close()

    if state_updated:
        save_state(saved_state)

    # ---------- 3. 分级推送钉钉通知（标题均包含“监控”关键词） ----------
    if init_reports:
        send_dingtalk(
            "网页监控初始化成功",
            "### ✅ 网页监控已启动\n\n已记录初始基准，请核对以下识别结果：\n\n" + "\n".join(init_reports)
        )

    if critical_alerts:
        send_dingtalk(
            "【紧急】网页监控异常告警",
            "### 🚨 网页监控紧急告警\n\n" + "\n\n---\n\n".join(critical_alerts)
        )

    if change_alerts:
        send_dingtalk(
            "网页监控内容/价格变动提醒",
            "### 🔔 检测到网页监控内容变动\n\n" + "\n\n---\n\n".join(change_alerts)
        )


if __name__ == "__main__":
    main()
