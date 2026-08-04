/**
 * Скриншоты для README — снимаются скриптом, а не руками: иначе они молча устаревают
 * при первом же изменении интерфейса или стратегии (ровно это и случилось с прошлой
 * версией backtest.png, где остался результат до правки движка).
 *
 * Требуется запущенный терминал на :8100 и Playwright:
 *   npx playwright install chromium
 *   node scripts/screenshots.mjs
 */
import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";

const BASE = process.env.GRIDLAB_URL || "http://localhost:8100";
const OUT = "docs/screenshots";
const VIEWPORT = { width: 1680, height: 1000 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function clickByText(page, re) {
  const btn = page.locator("button").filter({ hasText: re }).first();
  if (await btn.count()) {
    await btn.click();
    return true;
  }
  return false;
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 });

  page.on("console", (m) => m.type() === "error" && console.log("  console:", m.text()));

  await page.goto(BASE, { waitUntil: "networkidle" });
  // localStorage хранит параметры прошлой сессии и перебивает дефолты сервера —
  // для воспроизводимого снимка его надо очистить и перезагрузить страницу.
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "networkidle" });
  await sleep(4000);

  await page.screenshot({ path: `${OUT}/terminal.png`, fullPage: false });
  console.log("  ✓ terminal.png");

  // Бэктест по всей корзине → вкладка «Аналитика» с итоговыми метриками
  await clickByText(page, /^Бэктест/);
  await sleep(12000);
  await clickByText(page, /Аналитика/);
  await sleep(2500);
  await page.screenshot({ path: `${OUT}/backtest.png`, fullPage: false });
  console.log("  ✓ backtest.png");

  // Панель параметров: видно режим «Грид» и поля лестницы
  await clickByText(page, /Параметры/);
  await sleep(2000);
  await page.screenshot({ path: `${OUT}/params.png`, fullPage: false });
  console.log("  ✓ params.png");

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
