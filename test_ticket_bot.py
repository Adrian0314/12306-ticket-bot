import tempfile
import unittest
from pathlib import Path

import ticket_bot

try:  # lxml 是可选测试依赖：pip install lxml
    from lxml import html as lxml_html
except ImportError:
    lxml_html = None


class CachedChromeDriverTests(unittest.TestCase):
    def test_prefers_driver_with_matching_chrome_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir)
            older_driver = (
                cache_root
                / "chromedriver"
                / "win64"
                / "151.0.7921.99"
                / "chromedriver.exe"
            )
            matching_driver = (
                cache_root
                / "chromedriver"
                / "win64"
                / "151.0.7922.138"
                / "chromedriver.exe"
            )
            for driver in (older_driver, matching_driver):
                driver.parent.mkdir(parents=True)
                driver.touch()

            result = ticket_bot.find_cached_chromedriver(
                "151.0.7922.170", cache_root
            )

        self.assertEqual(result, str(matching_driver))


# ============ 选座回归（离线，不需要浏览器） ============
# 历史 bug：SEAT_CHOICE_XPATH 写死成 #erdeng1，而确认框里的座位图是「一行对应一位乘车人」
# （#erdeng1 第 1 人、#erdeng2 第 2 人……），导致两人订单只能点到第 1 排、
# 「已选座」永远停在 1/2，第二位乘车人选不上座。
# 下面用 lxml 当 XPath 引擎 + 一个 Selenium 形状的假 driver，驱动真实的下单逻辑，
# 不打网络也不起浏览器，可稳定回归。

SEAT_GROUPS = {
    "yideng": (["A", "C"], ["D", "F"]),
    "erdeng": (["A", "B", "C"], ["D", "F"]),
    "tedeng": (["A", "C"], ["F"]),
    "shangwu": (["A"], ["F"]),
}
SEAT_ROW_ORDER = ["yideng", "erdeng", "tedeng", "shangwu"]


def build_seat_dialog(active="erdeng", rows=2, need=2):
    """复刻确认框座位图：各席别容器并存，非当前席别 display:none，a 的 id 跨容器重复。"""
    items = []
    for r in range(1, rows + 1):
        for group in SEAT_ROW_ORDER:
            left, right = SEAT_GROUPS[group]
            style = "block" if group == active else "none"
            lis_l = "".join(
                '<li><a href="javascript:" id="%d%s">%s</a></li>' % (r, letter, letter)
                for letter in left
            )
            lis_r = "".join(
                '<li><a href="javascript:" id="%d%s">%s</a></li>' % (r, letter, letter)
                for letter in right
            )
            items.append(
                '<div class="sel-item" id="%s%d" style="display: %s;">'
                '<ul class="seat-list">%s</ul><div class="txt">过道</div>'
                '<ul class="seat-list">%s</ul></div>' % (group, r, style, lis_l, lis_r)
            )
    return (
        '<!DOCTYPE html><html><body><div class="seat-sel" id="id-seat-sel">'
        '<div class="seat-sel-hd">已选座<span id="selectNo">0/%d</span></div>'
        '<div class="seat-sel-bd">%s</div></div></body></html>'
    ) % (need, "".join(items))


class _FakeElement:
    def __init__(self, node, driver):
        self.node = node
        self.driver = driver

    @property
    def text(self):
        return "".join(self.node.itertext()).strip()

    def _row(self):
        node = self.node
        while node is not None:
            if "sel-item" in (node.get("class") or ""):
                return node
            node = node.getparent()
        return None

    def is_displayed(self):
        node = self.node
        while node is not None:
            if "display:none" in (node.get("style") or "").replace(" ", "").lower():
                return False
            node = node.getparent()
        return True

    def click(self):
        self.driver.clicks.append((self._row().get("id"), self.text))
        self.driver.mark_selected(self._row().get("id"), self.text)

    def find_element(self, by, xpath):
        found = self.node.xpath(xpath)
        if not found:
            raise ticket_bot.WebDriverException("no such element: " + xpath)
        return _FakeElement(found[0], self.driver)


class _FakeDriver:
    """只实现选座逻辑用到的那几个 Selenium API。"""

    def __init__(self, page_html):
        self.tree = lxml_html.fromstring(page_html)
        self.clicks = []
        self.selected = {}

    def find_elements(self, by, xpath):
        return [_FakeElement(node, self) for node in self.tree.xpath(xpath)]

    def find_element(self, by, xpath):
        found = self.find_elements(by, xpath)
        if not found:
            raise ticket_bot.WebDriverException("no such element: " + xpath)
        return found[0]

    def execute_script(self, script, element):
        element.click()

    def mark_selected(self, row_id, letter):
        """模拟 12306：一行一个座位，点完刷新「已选座 x/N」"""
        self.selected[row_id] = letter
        need = int(self.tree.xpath("//span[@id='selectNo']")[0].text.split("/")[-1])
        for span in self.tree.xpath("//span[@id='selectNo']"):
            span.text = "%d/%d" % (len(self.selected), need)

    def counter(self):
        return self.tree.xpath("//span[@id='selectNo']")[0].text


class SeatSelectionTests(unittest.TestCase):
    """多人订单的选座回归。需要 lxml：pip install lxml"""

    def setUp(self):
        self._real_log = ticket_bot.log
        ticket_bot.log = lambda msg: None  # 测试不写运行日志
        self.bot = ticket_bot.TicketBot({})

    def tearDown(self):
        ticket_bot.log = self._real_log

    def _select(self, page_html, letters, seat_type, passengers):
        driver = _FakeDriver(page_html)
        self.bot.driver = driver
        self.bot._try_select_seat(letters, seat_type, passengers)
        return driver

    def test_seat_map_has_duplicate_ids_across_groups(self):
        """先确认夹具复刻了真实页面的坑：同一个 #1A 出现在 4 个第 1 排容器里"""
        tree = lxml_html.fromstring(build_seat_dialog())
        self.assertEqual(len(tree.xpath("//a[@id='1A']")), 4)

    def test_old_selector_could_only_fill_one_seat(self):
        """旧写法写死 #erdeng1，只能点到第 1 排 → 计数器停在 1/2（bug 复现）"""
        driver = _FakeDriver(build_seat_dialog())
        driver.find_element(ticket_bot.By.XPATH, "//div[@id='erdeng1']//a[text()='A']").click()
        self.assertEqual(driver.counter(), "1/2")

    def test_two_passengers_click_each_row(self):
        driver = self._select(build_seat_dialog(need=2), "A", "二等座", 2)
        self.assertEqual(driver.clicks, [("erdeng1", "A"), ("erdeng2", "A")])
        self.assertEqual(driver.counter(), "2/2")

    def test_multiple_letters_are_assigned_per_row(self):
        """preferred_seat 写多个字母时按行依次分配（两人想坐一起）"""
        driver = self._select(build_seat_dialog(need=2), "DF", "二等座", 2)
        self.assertEqual(driver.clicks, [("erdeng1", "D"), ("erdeng2", "F")])
        self.assertEqual(driver.counter(), "2/2")

    def test_seat_type_selects_matching_group_only(self):
        """一等座只能点可见的 yideng 行，不能碰隐藏的 erdeng"""
        driver = self._select(build_seat_dialog(active="yideng", need=2), "C", "一等座", 2)
        self.assertEqual(driver.clicks, [("yideng1", "C"), ("yideng2", "C")])

    def test_invalid_letter_is_ignored(self):
        """二等座没有 E 座，非法字母应被过滤而不是乱点"""
        driver = self._select(build_seat_dialog(need=2), "E", "二等座", 2)
        self.assertEqual(driver.clicks, [])

    def test_lowercase_letter_is_accepted(self):
        driver = self._select(build_seat_dialog(need=2), "d", "二等座", 2)
        self.assertEqual(driver.clicks, [("erdeng1", "D"), ("erdeng2", "D")])

    def test_fewer_rows_than_passengers_does_not_crash(self):
        driver = self._select(build_seat_dialog(rows=2, need=3), "A", "二等座", 3)
        self.assertEqual(driver.clicks, [("erdeng1", "A"), ("erdeng2", "A")])

    def test_no_seat_map_is_skipped(self):
        driver = self._select("<html><body>本车次不支持选座</body></html>", "A", "二等座", 2)
        self.assertEqual(driver.clicks, [])

    def test_read_seat_counter_returns_selected_and_needed(self):
        self.bot.driver = _FakeDriver(build_seat_dialog(rows=2, need=2))
        self.assertEqual(self.bot._read_seat_counter(), (0, 2))
        self.assertEqual(self.bot._read_seat_need(), 2)
        self.bot.driver = _FakeDriver("<html><body>无座位图</body></html>")
        self.assertEqual(self.bot._read_seat_counter(), (0, 0))
        self.assertEqual(self.bot._read_seat_need(default=3), 3)

    def test_per_passenger_list_assigns_each_row(self):
        """合并订单后的逐人偏好 ["D","F"] → 第 1 人 D、第 2 人 F"""
        driver = self._select(build_seat_dialog(need=2), ["D", "F"], "二等座", 2)
        self.assertEqual(driver.clicks, [("erdeng1", "D"), ("erdeng2", "F")])
        self.assertEqual(driver.counter(), "2/2")

    def test_per_passenger_list_skips_empty_entries(self):
        """列表里的空字符串表示该位乘客不选座，只点有配置的行"""
        driver = self._select(build_seat_dialog(need=2), ["D", ""], "二等座", 2)
        self.assertEqual(driver.clicks, [("erdeng1", "D")])
        self.assertEqual(driver.counter(), "1/2")

    def test_per_passenger_all_empty_is_noop(self):
        driver = self._select(build_seat_dialog(need=2), ["", ""], "二等座", 2)
        self.assertEqual(driver.clicks, [])

    def test_per_passenger_list_shorter_than_rows(self):
        driver = self._select(build_seat_dialog(need=3, rows=3), ["D", "F"], "二等座", 3)
        self.assertEqual(driver.clicks, [("erdeng1", "D"), ("erdeng2", "F")])

    def test_per_passenger_invalid_entries_are_ignored(self):
        driver = self._select(build_seat_dialog(need=2), ["E", "F"], "二等座", 2)
        self.assertEqual(driver.clicks, [("erdeng2", "F")])


class OrderMergeTests(unittest.TestCase):
    """同线路订单合并：座位偏好必须按乘车人顺序逐人保留（回归）。

    历史 bug：合并时 preferred_seat 只保留第一笔订单的值（后面被 if not ... 挡掉），
    订单 2 选 D、订单 3 选 F 会被并成一笔两人订单，选座时两人都被点成 D。
    """

    def setUp(self):
        self._real_log = ticket_bot.log
        ticket_bot.log = lambda msg: None  # 测试不写运行日志
        self.bot = ticket_bot.TicketBot({"preferred_seat": "", "orders": []})

    def tearDown(self):
        ticket_bot.log = self._real_log

    @staticmethod
    def _order(name, seat="__omit__", pax=1, enabled=True, sale_time="08:00"):
        """seat="__omit__" 表示配置里不写这个字段（跟随全局）"""
        order = {
            "enabled": enabled, "from_st": "佛山西", "to_st": "平南南",
            "date": "2026-09-26", "sale_time": sale_time, "depart_time_range": "",
            "seat_type": "二等座",
            "passengers": [{"name": name} for _ in range(pax)],
        }
        if seat != "__omit__":
            order["preferred_seat"] = seat
        return order

    def test_each_order_keeps_its_own_seat_preference(self):
        self.bot.orders = [self._order("乘客A", seat="D"),
                           self._order("乘客B", seat="F", sale_time="08:01")]
        merged = self.bot._merge_orders()
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["passengers"]), 2)
        self.assertEqual(merged[0]["preferred_seats"], ["D", "F"])

    def test_omitted_seat_falls_back_to_global(self):
        self.bot = ticket_bot.TicketBot({"preferred_seat": "D", "orders": []})
        self.bot.orders = [self._order("乘客A"),
                           self._order("乘客B", seat="F", sale_time="08:01")]
        merged = self.bot._merge_orders()
        self.assertEqual(merged[0]["preferred_seats"], ["D", "F"])

    def test_explicit_empty_seat_is_preserved(self):
        '''配置里写 ""（不选座）不会被其他订单的字母覆盖'''
        self.bot.orders = [self._order("乘客A", seat=""),
                           self._order("乘客B", seat="F", sale_time="08:01")]
        merged = self.bot._merge_orders()
        self.assertEqual(merged[0]["preferred_seats"], ["", "F"])

    def test_multi_passenger_order_expands_its_preference(self):
        self.bot.orders = [self._order("乘客A", seat="D", pax=2),
                           self._order("乘客B", seat="F", sale_time="08:01")]
        merged = self.bot._merge_orders()
        self.assertEqual(merged[0]["preferred_seats"], ["D", "D", "F"])
        self.assertEqual(len(merged[0]["passengers"]), 3)

    def test_disabled_orders_are_not_merged(self):
        self.bot.orders = [self._order("乘客A", seat="D", enabled=False),
                           self._order("乘客B", seat="F")]
        merged = self.bot._merge_orders()
        self.assertEqual([p["name"] for p in merged[0]["passengers"]], ["乘客B"])
        self.assertEqual(merged[0]["preferred_seats"], ["F"])

    def test_different_routes_are_not_merged(self):
        self.bot.orders = [self._order("乘客A", seat="D"),
                           self._order("乘客B", seat="F", sale_time="08:01")]
        self.bot.orders[1]["to_st"] = "广州南"
        merged = self.bot._merge_orders()
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["preferred_seats"], ["D"])
        self.assertEqual(merged[1]["preferred_seats"], ["F"])

    def test_merged_order_keeps_other_fields(self):
        self.bot.orders = [self._order("乘客A", seat="D", sale_time="07:00"),
                           self._order("乘客B", seat="F", sale_time="08:01")]
        merged = self.bot._merge_orders()
        self.assertEqual(merged[0]["from_st"], "佛山西")
        self.assertEqual(merged[0]["to_st"], "平南南")
        self.assertEqual(merged[0]["date"], "2026-09-26")
        self.assertEqual(merged[0]["sale_time"], "07:00")
        self.assertEqual(merged[0]["seat_type"], "二等座")


if lxml_html is None:
    SeatSelectionTests = unittest.skip(
        "选座回归测试需要 lxml：pip install lxml"
    )(SeatSelectionTests)


if __name__ == "__main__":
    unittest.main()

