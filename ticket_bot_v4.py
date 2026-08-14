"""
12306 高铁票订票助手（Selenium + CDP 网络监听版）
=================================================
流程：
  1. 浏览器登录（扫码）
  2. 填表单、点查询
  3. CDP 监听查票接口的 JSON 响应 → 毫秒级解析
  4. 下单走浏览器自动化

依赖：pip install selenium

订单配置见 config.json（本地，gitignore） / config.example.json（示例，随仓库分发）。
本脚本自身不含任何个人信息。
"""

import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

# ============ 常量 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "ticket_log_v4.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOGIN_TIMEOUT = 300          # 扫码登录等待上限（秒）
QUERY_TIMEOUT = 15           # 查票接口等待上限（秒）
QUERY_RETRY = 3              # 单笔订单查票重试次数
SALE_LEAD_TIME = 5           # 起售时刻提前量（秒），覆盖导航+填表耗时
SALE_SKIP_THRESHOLD = 6 * 3600  # 起售顺延次日后仍需等待超过该秒数 → 视为已过，立即查询
BOOK_BTN_TEXT = "预订"        # 12306 预订按钮文字

# 席别 → 查票接口字段索引（leftTicket 返回格式，字段以 | 分隔）
# 索引依据 2025 年公开解析源码（OpenCLI / 12306-skill）核对：
#   [3]车次 [8]出发 [9]到达 [10]历时 [25]特等 [26]无座 [29]硬座 [30]二等 [31]一等 [32]商务
SEAT_FIELD_MAP = {
    "硬座": 29,
    "二等座": 30,
    "一等座": 31,
    "特等座": 25,
    "商务座": 32,
    "无座": 26,
}


def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_exc(msg):
    """记录错误信息并附完整 traceback，便于定位问题"""
    log(msg)
    log(traceback.format_exc().strip())


def seat_available(val):
    """12306 余票值判断：数字(>0)或"有"=有票；无/--/空=没票"""
    if val == "有":
        return True
    if val in ("无", "--", ""):
        return False
    try:
        return int(val) > 0
    except (ValueError, TypeError):
        return False


def load_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TicketBot:
    """Selenium + CDP 监听版订票机器人"""

    def __init__(self, config):
        self.orders = config.get("orders", [])
        self.mode = config.get("mode", "sale_time")
        self.delay_seconds = config.get("delay_seconds", 5)
        self.debug_seat = config.get("debug_seat", False)
        self.mute_car = config.get("mute_car", False)           # 勾选静音车厢（车次支持时）
        self.preferred_seat = config.get("preferred_seat", "")  # 首选座位字母 A/B/C/D/F，空=不选座
        self.auto_submit = config.get("auto_submit", False)     # False=停在确认页人工提交
        self.driver = None
        self.stop_all = False

    # ============ 选择器（用户 F12 定位，已按真实元素写死） ============
    MUTE_CAR_XPATH = "//input[@id='seat-jy']"
    # 二等座座位图在 erdeng1 容器内（一等/特等/商务容器都是隐藏的，且 id 重复，必须限定容器）
    SEAT_CHOICE_XPATH = "//div[@id='erdeng1']//a[text()='{letter}']"
    STUDENT_DIALOG_OK_XPATH = "//a[contains(@id,'xsertcj') and text()='确认']"
    # 勾选静音车厢后弹出的规则说明弹窗的确定按钮
    WARNING_DIALOG_OK_XPATH = "//a[@id='qd_closeDefaultWarningWindowDialog_id']"
    # 点确认后弹出的学生票资质核验提示弹窗的确定按钮
    XSPN_DIALOG_OK_XPATH = "//a[@id='conf_xspnalert']"

    # ============ CDP 网络监听 ============
    def _setup_cdp_intercept(self):
        """开启网络监听，查询时用性能日志轮询查票接口"""
        self.driver.execute_cdp_cmd("Network.enable", {})

    def _drain_performance_logs(self):
        """读取并清空性能日志缓冲，避免登录期历史事件占满缓冲"""
        try:
            self.driver.get_log("performance")
        except Exception:
            pass

    def _get_response_body(self, request_id):
        """通过 CDP 获取响应体"""
        try:
            body = self.driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            return body.get("body", "")
        except Exception:
            return None

    def _find_query_response(self, timeout=QUERY_TIMEOUT):
        """
        轮询性能日志，等待本次查询产生的查票接口响应并返回 JSON。
        chromedriver 的 get_log 读取后即清空，天然增量；对响应体未就绪的
        requestId 保留重试，避免时序问题丢响应。
        """
        deadline = time.time() + timeout
        pending_ids = []
        tried = set()
        while time.time() < deadline:
            # 扫新增日志，收集查票接口的 requestId
            try:
                logs = self.driver.get_log("performance")
            except Exception:
                logs = []
            for entry in logs:
                try:
                    log_data = json.loads(entry["message"])
                    msg = log_data.get("message", {})
                    if msg.get("method") != "Network.responseReceived":
                        continue
                    response = msg.get("params", {}).get("response", {})
                    url = response.get("url", "")
                    if "leftTicket/query" in url:
                        rid = msg["params"]["requestId"]
                        if rid not in pending_ids:
                            pending_ids.append(rid)
                except Exception:
                    continue
            # 逐个取响应体；body 未就绪的 requestId 下一轮重试，不永久跳过
            for rid in pending_ids:
                if rid in tried:
                    continue
                body = self._get_response_body(rid)
                if not body:
                    continue
                tried.add(rid)
                try:
                    return json.loads(body)
                except Exception:
                    continue
            time.sleep(0.1)
        return None

    # ============ 登录 ============
    def login(self):
        log("启动浏览器，请扫码登录...")

        opts = Options()
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_argument("--disable-blink-features=AutomationControlled")
        # 关键：开启性能日志，CDP 网络监听（get_log("performance")）才能读到查票接口
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        # Chrome 启动偶发闪退（更新中/进程冲突），重试 3 次
        for attempt in range(1, 4):
            try:
                self.driver = webdriver.Chrome(opts)
                break
            except Exception as e:
                log(f"  Chrome 启动失败（第{attempt}/3次）: {str(e)[:120]}")
                if attempt == 3:
                    log_exc("  Chrome 启动失败次数用尽，退出")
                    raise
                time.sleep(2)

        self.driver.maximize_window()
        self._setup_cdp_intercept()
        self.driver.get("https://kyfw.12306.cn/otn/resources/login.html")

        # 等扫码成功：登录成功后页面跳离 login.html。加超时防止永久挂起
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            try:
                if "login.html" not in self.driver.current_url:
                    log("登录成功")
                    return
            except Exception:
                pass
            time.sleep(1)
        log("登录超时，请重新运行并尽快扫码")
        raise TimeoutError("等待扫码超时")

    # ============ 查票 ============
    def _fill_station(self, elem_id, station):
        """填站名并从联想列表选第一项，选中后校验"""
        wait = WebDriverWait(self.driver, 10)
        elem = wait.until(EC.element_to_be_clickable((By.ID, elem_id)))
        elem.click()
        elem.send_keys(station)
        wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "ralign")))
        self.driver.find_element(By.CLASS_NAME, "ralign").click()
        value = self.driver.find_element(By.ID, elem_id).get_attribute("value")
        if not value:
            raise ValueError(f"站点联想失败: {station}")

    def query_tickets(self, order):
        """填表单并点击查询，通过 CDP 监听拿到 JSON"""
        from_st = order["from_st"]
        to_st = order["to_st"]
        date = order["date"]
        time_range = order.get("depart_time_range", "")
        seat_type = order.get("seat_type", "二等座")

        self.driver.get("https://kyfw.12306.cn/otn/leftTicket/init")

        self._fill_station("fromStationText", from_st)
        time.sleep(0.5)
        self._fill_station("toStationText", to_st)
        time.sleep(0.5)

        date_input = self.driver.find_element(By.ID, "train_date")
        date_input.clear()
        date_input.send_keys(date)
        time.sleep(0.3)

        log(f"  查询: {from_st} → {to_st}  {date}")

        # 点击查询前清空性能日志缓冲，只认本次查询新产生的响应
        self._drain_performance_logs()
        self.driver.find_element(By.ID, "query_ticket").click()

        query_data = self._find_query_response(timeout=QUERY_TIMEOUT)
        if not query_data:
            log("  未监听查票接口响应，尝试从 DOM 解析")
            return self._parse_from_dom(time_range, seat_type)
        return self._parse_query_json(query_data, time_range, seat_type)

    def _parse_query_json(self, data, time_range, seat_type):
        """解析 CDP 监听到的 JSON"""
        status = data.get("status")
        httpstatus = data.get("httpstatus", 0)
        if status is not True or httpstatus != 200:
            log(f"  查票失败: status={status}, httpstatus={httpstatus}")
            return None

        result = data.get("data", {}).get("result", [])
        seat_idx = SEAT_FIELD_MAP.get(seat_type, 30)

        log(f"  查到 {len(result)} 趟车")

        candidates = []
        for item in result:
            fields = item.split("|")
            if len(fields) < 32:
                continue

            train_code = fields[3]      # 车次代码，如 G2917（[2] 是内部 train_no）
            start_time = fields[8]
            arrive_time = fields[9]
            duration = fields[10]
            seat_count = fields[seat_idx] if seat_idx < len(fields) else "无"

            # 该席别没票的车次直接排除，避免选了无票车次下单失败
            if not seat_available(seat_count):
                continue

            # 时段筛选
            if time_range and "-" in time_range:
                t_start, t_end = time_range.split("-", 1)
                if not (t_start <= start_time <= t_end):
                    continue

            candidates.append({
                "train_code": train_code,
                "start_time": start_time,
                "arrive_time": arrive_time,
                "duration": duration,
                "seat_count": seat_count,
            })

        # 打印前 10 趟
        for t in candidates[:10]:
            log(f"    {t['train_code']}  {t['start_time']}→{t['arrive_time']}  "
                f"历时{t['duration']}  {seat_type}:{t['seat_count']}张")

        return candidates[0] if candidates else None

    def _parse_from_dom(self, time_range, seat_type):
        """CDP 监听失败时的降级方案：从页面 DOM 解析"""
        try:
            rows = self.driver.find_elements(By.XPATH, '//tr[contains(@id,"ticket_")]')
        except Exception:
            return None

        t_start, t_end = ("", "")
        if time_range and "-" in time_range:
            t_start, t_end = time_range.split("-", 1)

        for row in rows:
            text = row.text.strip()
            if not text:
                continue
            match = re.search(r"(\d{2}:\d{2})", text)
            if not match:
                continue
            dep_time = match.group(1)

            if time_range and not (t_start <= dep_time <= t_end):
                continue

            try:
                btn = row.find_element(By.XPATH, f'.//a[text()="{BOOK_BTN_TEXT}"]')
                log("  找到可预订车次")
                return {"book_btn": btn, "row_text": text, "seat_type": seat_type}
            except Exception:
                continue

        log("  未找到可预订车次")
        return None

    # ============ 下单 ============
    def book_ticket(self, order, train_info):
        """在浏览器里完成选座、选人、提交"""
        passengers = order.get("passengers", [])
        seat_type = order.get("seat_type", "")

        # 点预订按钮
        if "book_btn" in train_info:
            try:
                train_info["book_btn"].click()
            except Exception as e:
                log(f"  点击预订失败: {e}")
                return False
        else:
            # CDP JSON 来的，回 DOM 根据车次号找预订按钮
            train_code = train_info.get("train_code", "")
            if train_code:
                try:
                    rows = self.driver.find_elements(By.XPATH, '//tr[contains(@id,"ticket_")]')
                    for row in rows:
                        text = row.text.strip()
                        if train_code in text:
                            row.find_element(By.XPATH, f'.//a[text()="{BOOK_BTN_TEXT}"]').click()
                            log(f"  已点击预订: {train_code}")
                            break
                except Exception as e:
                    log(f"  在页面上未找到预订按钮: {e}")
                    return False

        time.sleep(2)

        # 调试模式：点预订后暂停，人工查看选座 UI
        if self.debug_seat:
            log("  调试模式：已暂停，请查看选座界面。按 Enter 继续...")
            input()

        # 选乘客
        for i, p in enumerate(passengers):
            try:
                cb = self.driver.find_element(
                    By.XPATH,
                    f'//label[contains(text(),"{p["name"]}")]/preceding-sibling::input',
                )
                cb.click()
                time.sleep(0.3)
            except Exception:
                log(f"  未找到乘客（第{i+1}位）")

        # 学生票提示弹窗：选完乘车人后出现，检测并点确认关掉
        self._close_student_dialog()

        # 席别下拉框（如果页面上有的话）
        if seat_type:
            try:
                st = self.driver.find_element(By.XPATH, '//select[contains(@id,"seat")]')
                Select(st).select_by_visible_text(seat_type)
                log(f"  已选席别: {seat_type}")
            except Exception:
                pass

        # 点"提交订单"，弹出订单确认框（选座/静音车厢都在框内，点之前不存在）
        try:
            submit_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "submitOrder_id"))
            )
        except Exception as e:
            log_exc(f"  找不到提交按钮: {e}")
            return False
        try:
            submit_btn.click()
        except Exception:
            log("  提交按钮被遮挡，用 JS 点击绕过")
            self.driver.execute_script("arguments[0].click();", submit_btn)
        log("  已打开订单确认框")
        time.sleep(1)

        # 确认框内：勾静音车厢 + 选座（车次支持时，找不到就跳过）
        if self.mute_car:
            self._try_select_mute_car()
        if self.preferred_seat:
            self._try_select_seat(self.preferred_seat)

        # 人工确认模式：停在确认框的"确认"按钮前，人工核对后手动点击
        if not self.auto_submit:
            log("  已就绪：请人工核对确认框内的车次/乘车人/席别/座位，手动点击“确认”完成下单")
            input("  核对并手动确认后，按回车继续下一笔订单...")
            # 点确认后可能弹出学生票资质核验提示弹窗，自动关掉
            self._close_xspn_dialog()
            log("  请尽快完成支付；未支付前 12306 无法购买其他车票")
            return None

        # 自动确认
        try:
            self.driver.find_element(By.ID, "qr_submit_id").click()
            log("  已确认，等待系统处理...")
            self._close_xspn_dialog()
            return self._wait_order_result()
        except Exception:
            log_exc("  确认失败")
            return None

    def _close_xspn_dialog(self):
        """点掉确认后弹出的学生票资质核验提示弹窗。无弹窗则静默跳过。
        元素: <div id="xspnalerttext">在校资质核验仅对您的在校学生身份进行核验...</div>
              <a id="conf_xspnalert">确认</a>
        """
        try:
            btn = WebDriverWait(
                self.driver, 5, ignored_exceptions=(WebDriverException,)
            ).until(
                EC.presence_of_element_located((By.XPATH, self.XSPN_DIALOG_OK_XPATH))
            )
            try:
                btn.click()
            except Exception as e:
                log(f"  核验弹窗确认按钮普通点击失败({str(e)[:80]})，改用 JS 点击")
                self.driver.execute_script("arguments[0].click();", btn)
            log("  已关闭学生票资质核验提示弹窗")
            time.sleep(0.5)
        except Exception:
            pass  # 无弹窗，正常继续

    # ============ 静音车厢 / 选座（车次不支持则跳过） ============
    def _close_student_dialog(self):
        """选完乘车人后检测学生票弹窗：出现则点确认关闭，未出现则记录。
        元素: <div id="dialog_xsertcj_msg">您是要购买学生票吗？...</div>
              <a id="dialog_xsertcj_ok">确认</a> / <a id="dialog_xsertcj_cancel">取消</a>
        """
        try:
            btn = WebDriverWait(
                self.driver, 5, ignored_exceptions=(WebDriverException,)
            ).until(
                EC.presence_of_element_located((By.XPATH, self.STUDENT_DIALOG_OK_XPATH))
            )
            # 普通点击被弹窗遮罩/动画挡住时，用 JS 点击绕过
            try:
                btn.click()
            except Exception as e:
                log(f"  确认按钮普通点击失败({str(e)[:80]})，改用 JS 点击")
                self.driver.execute_script("arguments[0].click();", btn)
            log("  学生票弹窗出现，已点确认关闭")
            time.sleep(0.5)
        except Exception:
            log("  未出现学生票弹窗")

    def _try_select_mute_car(self):
        """勾选静音车厢。找不到元素说明本车次不支持，跳过不报错。
        勾选后会弹出静音车厢规则说明弹窗，需点确定关掉，否则挡住选座。
        """
        try:
            cb = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, self.MUTE_CAR_XPATH))
            )
            if not cb.is_selected():
                cb.click()
            log("  已勾选静音车厢")
            self._close_warning_dialog()
        except Exception:
            log("  本车次无静音车厢选项，跳过")

    def _close_warning_dialog(self):
        """关闭勾选静音车厢后弹出的规则说明弹窗。无弹窗则静默跳过。"""
        try:
            btn = WebDriverWait(
                self.driver, 5, ignored_exceptions=(WebDriverException,)
            ).until(
                EC.presence_of_element_located((By.XPATH, self.WARNING_DIALOG_OK_XPATH))
            )
            try:
                btn.click()
            except Exception as e:
                log(f"  提示弹窗确定按钮普通点击失败({str(e)[:80]})，改用 JS 点击")
                self.driver.execute_script("arguments[0].click();", btn)
            log("  已关闭静音车厢说明弹窗")
            time.sleep(0.5)
        except Exception:
            pass  # 无弹窗，正常继续

    def _try_select_seat(self, letter):
        """点击座位图字母按钮（优先分配）。车次不支持选座则跳过。"""
        try:
            btn = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable(
                    (By.XPATH, self.SEAT_CHOICE_XPATH.format(letter=letter))
                )
            )
            btn.click()
            log(f"  已选择优先分配座位: {letter}")
        except Exception:
            log(f"  本车次无选座功能或未找到 {letter} 座按钮，跳过")

    def _wait_order_result(self, timeout=60):
        """
        主动检测订单处理结果。
        成功条件：页面出现订单号或订单确认相关文字
        失败条件：页面出现"订票失败""取消次数过多"等提示
        """
        deadline = time.time() + timeout
        last_url = self.driver.current_url

        while time.time() < deadline:
            time.sleep(1)
            try:
                page_text = self.driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                continue

            # 成功标志：优先匹配订单号
            order_id_match = re.search(r"[Ee]\d{10,}", page_text)
            if order_id_match:
                log(f"  订单完成: {order_id_match.group()}")
                return True
            if "订单号" in page_text or "支付" in page_text or "席位已锁定" in page_text:
                log("  订单已完成，请支付")
                return True

            # 失败标志
            if "订票失败" in page_text:
                reason_match = re.search(r"原因[：:]\s*(.+?)(?:[。\n]|$)", page_text)
                if reason_match:
                    log(f"  订票失败: {reason_match.group(1)}")
                else:
                    log("  订票失败")
                return False

            if "取消次数过多" in page_text:
                log("  今日取消次数已达上限，停止后续所有订单")
                self.stop_all = True
                return False

            # 页面跳转检测：URL 变化说明处理完成
            current_url = self.driver.current_url
            if current_url != last_url and "confirmPassenger" not in current_url:
                log("  页面已跳转，正在核对结果...")
                time.sleep(2)
                try:
                    page_text = self.driver.find_element(By.TAG_NAME, "body").text
                    if re.search(r"[Ee]\d{10,}", page_text) or "订单号" in page_text:
                        log("  订单完成")
                        return True
                except Exception:
                    pass
            last_url = current_url

        log("  等待超时，无法确认结果，请手动核对订单")
        return False

    # ============ 主流程 ============
    def wait_until_sale_time(self, sale_time):
        """阻塞等待到起售时刻，提前 SALE_LEAD_TIME 秒放行给查询动作。
        今日起售时刻已过则顺延到次日；顺延后仍需等待超过
        SALE_SKIP_THRESHOLD 秒（如白天起售已过），视为已过，立即放行。
        """
        if not sale_time:
            return
        now = datetime.now()
        h, m = map(int, sale_time.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        wait_seconds = (target - now).total_seconds()
        if wait_seconds < 0:
            # 今日起售时刻已过：顺延到次日再等
            target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            if wait_seconds > SALE_SKIP_THRESHOLD:
                log(f"  起售 {sale_time} 已过（顺延次日需等 {int(wait_seconds // 3600)} 小时），直接查询")
                return
        if wait_seconds <= SALE_LEAD_TIME:
            return
        log(f"  距起售 {sale_time} 还有 {int(wait_seconds // 60)} 分 {int(wait_seconds % 60)} 秒")
        while True:
            now = datetime.now()
            remaining = (target - now).total_seconds()
            if remaining <= SALE_LEAD_TIME:
                break
            time.sleep(min(remaining - SALE_LEAD_TIME, 30))
        log(f"  起售时刻临近，开始查询 {sale_time}")

    def run(self):
        try:
            # 合并同线路
            merged = {}
            for o in self.orders:
                if not o.get("enabled", True):
                    continue
                key = (o["from_st"], o["to_st"], o["date"], o.get("depart_time_range", ""))
                if key not in merged:
                    merged[key] = {
                        "from_st": o["from_st"],
                        "to_st": o["to_st"],
                        "date": o["date"],
                        "sale_time": o.get("sale_time"),
                        "depart_time_range": o.get("depart_time_range", ""),
                        "seat_type": o.get("seat_type", ""),
                        "passengers": [],
                    }
                merged[key]["passengers"].extend(o["passengers"])
                if not merged[key]["depart_time_range"] and o.get("depart_time_range"):
                    merged[key]["depart_time_range"] = o["depart_time_range"]
                if not merged[key]["seat_type"] and o.get("seat_type"):
                    merged[key]["seat_type"] = o["seat_type"]

            merged_orders = list(merged.values())
            merged_orders.sort(key=lambda o: o.get("sale_time", "99:99"))
            log(f"合并后共 {len(merged_orders)} 笔订单")

            self.login()

            # timer 模式：登录后倒计时
            if self.mode == "timer":
                log(f"倒计时模式：{self.delay_seconds} 秒后开始抢票")
                for remaining in range(self.delay_seconds, 0, -1):
                    print(f"\r  剩余 {remaining} 秒...", end="", flush=True)
                    time.sleep(1)
                print("\r  开始抢票！" + " " * 20)

            # 逐笔处理
            for i, order in enumerate(merged_orders):
                log(f"--- 订单 {i+1}/{len(merged_orders)}: "
                    f"{order['from_st']} → {order['to_st']}  {order['date']} "
                    f"({len(order['passengers'])}人) ---")

                sale_time = order.get("sale_time")
                if sale_time:
                    self.wait_until_sale_time(sale_time)

                try:
                    # 查票失败重试，下单不重试（避免重复提交）
                    train = None
                    for attempt in range(1, QUERY_RETRY + 1):
                        train = self.query_tickets(order)
                        if train:
                            break
                        log(f"  查票无结果，第 {attempt}/{QUERY_RETRY} 次")
                        time.sleep(1)
                    if not train:
                        log("  未找到可预订车次")
                        continue
                    success = self.book_ticket(order, train)
                except Exception as e:
                    log_exc(f"  订单处理异常: {e}")
                    continue

                if success is True:
                    log("  ✓ 抢票成功")
                elif self.stop_all:
                    log("  ✗ 风控拦截，终止后续订单")
                    break
                elif not self.auto_submit:
                    log("  → 已转人工确认，订单结果请自行核对")
                else:
                    log("  ✗ 订票失败")
                time.sleep(2)

            log("=" * 40)
            log("全部处理完毕，请到 12306 App 查看并付款")
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass


if __name__ == "__main__":
    config = load_config()
    bot = TicketBot(config)
    bot.run()
