"""Opt-in live smoke test: start Flask on port 5086, then run this script.

Requires Playwright and its Chromium browser; public Binance quotes are read.
No brokerage login or trading actions. Screenshots go to the system temp folder.
"""
from pathlib import Path
from tempfile import gettempdir

from playwright.sync_api import expect, sync_playwright


def loaded(page):
    page.wait_for_function("document.querySelector('#contractCount').textContent !== '—'", timeout=60000)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:5086/stock-options")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#rows tr", timeout=60000)
    assert "股票" in page.locator("#catalogCount").inner_text()
    page.locator("#strategy").select_option("CALL")
    assert "股市值" in page.locator("#rows tr").first.inner_text()
    page.locator("#rows button").first.click()
    page.wait_for_function("document.querySelector('#contractCheck') && !document.querySelector('#contractCheck').textContent.includes('正在')", timeout=30000)
    assert "标准股票交割" in page.locator("#contractCheck").inner_text()
    assert "被行权总收益率" in page.locator("#detailBody").inner_text()
    page.locator("#closeDetail").click()
    page.locator("#fee").fill("")
    assert page.locator("#rows tr").count() == 0
    assert "有效" in page.locator("#empty").inner_text()
    page.locator("#fee").fill("0.60")
    page.locator("#search").fill("AAPL")
    page.locator('[data-stock="AAPL"]').click()
    loaded(page)
    assert page.locator("#stockTitle").inner_text() == "AAPL"
    page.locator("#favorite").click()
    assert page.locator("#favorite").get_attribute("aria-pressed") == "true"
    page.reload()
    page.wait_for_selector("#rows tr", timeout=60000)
    assert page.locator("#stockTitle").inner_text() == "AAPL"
    assert page.locator("#favorite").get_attribute("aria-pressed") == "true"
    page.locator("#assetType").select_option("ETF")
    page.locator("#search").fill("SPY")
    page.locator('[data-stock="SPY"]').click()
    loaded(page)
    assert page.locator("#stockTitle").inner_text() == "SPY"
    page.locator("#sort").select_option("apr")
    page.locator("#expiration").select_option("0")
    loaded(page)
    assert page.locator("#rows tr").count() > 0
    # Failed refresh must clear the previous quotes and permit retry.
    pattern = "**/api/stock-options/chain?*"
    page.route(pattern, lambda route: route.fulfill(status=502, json={"ok": False, "error": "行情读取失败，请稍后重试。"}))
    page.locator("#refresh").click()
    expect(page.locator("#status")).to_have_class("notice error")
    assert page.locator("#rows tr").count() == 0
    assert page.locator("#refresh").is_enabled()
    page.unroute(pattern)
    page.locator("#refresh").click()
    page.wait_for_selector("#rows tr", timeout=60000)
    for width, height, name in [(390, 844, "mobile"), (1440, 1000, "desktop")]:
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        path = Path(gettempdir()) / f"optiondata-stocks-{name}.png"
        page.screenshot(path=str(path), full_page=True)
        print(path)
    assert not errors, errors
    print("PASS: live stock/ETF navigation, CALL details, fee validation, favorites, expiry, error/retry, responsive layout; no JS errors")
    browser.close()
