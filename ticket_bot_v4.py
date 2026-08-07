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
import re
import time
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

# ============ 常量 ============
LOG_FILE = "ticket_log_v4.txt"
CONFIG_FILE = "config.json"
LOGIN_TIMEOUT = 300          # 扫码登录等待上限（秒）
QUERY_TIMEOUT = 15           # 查票接口等待上限（秒）
QUERY_RETRY = 3              # 单笔订单查票重试次数
SALE_LEAD_TIME = 5           # 起售时刻提前量（秒），覆盖导航+填表耗时
BOOK_BTN_TEXT = "预订"        # 12306 预订按钮文字

# 席别 → 查票接口字段索引（leftTicket 返回格式）
SEAT_FIELD_MAP = {
    "硬座": 29,
    "二等座": 30,
    "一等座": 31,
    "特等座": 32,
    "商务座": 32,
    "无座": 26,
}


def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


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
        self.driver = None
        self.stop_all = False

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
                    if "queryG" in url or "queryZ" in url:
                        rid = msg["params"]["requestId"]
                        if rid not in pending_ids:
                            pending_ids.append(rid)
                except Exception:
                    continue
            # 逐个取响应体，失败的下一轮重试
            for rid in pending_ids:
                if rid in tried:
                    continue
                tried.add(rid)
                body = self._get_response_body(rid)
                if body:
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

        # Chrome 启动偶发闪退（更新中/进程冲突），重试 3 次
        for attempt in range(1, 4):
            try:
                self.driver = webdriver.Chrome(opts)
                break
            except Exception as e:
                log(f"  Chrome 启动失败（第{attempt}/3次）: {str(e)[:120]}")
                if attempt == 3:
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

            train_code = fields[2]      # 车次号，如 G2917
            start_time = fields[8]
            arrive_time = fields[9]
            duration = fields[10]
            seat_count = fields[seat_idx] if seat_idx < len(fields) else "无"

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

        # 席别下拉框（如果页面上有的话）
        if seat_type:
            try:
                st = self.driver.find_element(By.XPATH, '//select[contains(@id,"seat")]')
                Select(st).select_by_visible_text(seat_type)
                log(f"  已选席别: {seat_type}")
            except Exception:
                pass

        # 提交订单
        try:
            submit_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "submitOrder_id"))
            )
        except Exception as e:
            log(f"  找不到提交按钮: {e}")
            return False
        try:
            submit_btn.click()
        except Exception:
            log("  提交按钮被遮挡，用 JS 点击绕过")
            self.driver.execute_script("arguments[0].click();", submit_btn)
        log("  已提交")
        time.sleep(2)

        # 确认
        try:
            self.driver.find_element(By.ID, "qr_submit_id").click()
            log("  已确认，等待系统处理...")
            return self._wait_order_result()
        except Exception:
            log("  确认失败")
            return None

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
        """阻塞等待到起售时刻，提前 SALE_LEAD_TIME 秒放行给查询动作"""
        if not sale_time:
            return
        now = datetime.now()
        h, m = map(int, sale_time.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        wait_seconds = (target - now).total_seconds()
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
                    log(f"  订单处理异常: {e}")
                    continue

                if success is True:
                    log("  ✓ 抢票成功")
                elif self.stop_all:
                    log("  ✗ 风控拦截，终止后续订单")
                    break
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
