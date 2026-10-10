import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const chromePath = process.argv[2];
const url = process.argv[3] || "http://127.0.0.1:8768/latest_dashboard.html";
const outputDir = process.argv[4] || "reports";

if (!chromePath) {
  throw new Error("Usage: node tests/check_dashboard_layout.mjs <chrome-path> [url] [output-dir]");
}

const profileDir = await mkdtemp(join(tmpdir(), "fund-monitor-layout-"));
const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--remote-debugging-port=0",
    `--user-data-dir=${profileDir}`,
    "about:blank",
  ],
  { stdio: "ignore", windowsHide: true },
);

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function readDevToolsPort() {
  const path = join(profileDir, "DevToolsActivePort");
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const [port] = (await readFile(path, "utf8")).trim().split(/\r?\n/);
      if (port) return port;
    } catch {
      // Chrome writes the port file after its browser process is ready.
    }
    await delay(100);
  }
  throw new Error("Chrome DevTools port did not become available");
}

async function connectPage(port) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
      const page = pages.find((item) => item.type === "page");
      if (page?.webSocketDebuggerUrl) return new WebSocket(page.webSocketDebuggerUrl);
    } catch {
      // The HTTP endpoint can appear shortly before the page target.
    }
    await delay(100);
  }
  throw new Error("Chrome page target did not become available");
}

function createCdp(socket) {
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
  });
  return (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

const selectors = [
  ".portfolio-card",
  ".fund-card",
  ".ai-cycle-panel",
  ".holding-stock-trends",
  ".recommendation-qualification",
  ".ai-fund-badge",
];

try {
  const port = await readDevToolsPort();
  const socket = await connectPage(port);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  const cdp = createCdp(socket);
  await cdp("Page.enable");
  await cdp("Runtime.enable");

  const results = [];
  for (const viewport of [
    { width: 1280, height: 720, name: "desktop" },
    { width: 390, height: 844, name: "mobile" },
  ]) {
    await cdp("Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: viewport.width < 600,
    });
    await cdp("Page.navigate", { url });
    await delay(1200);

    const expression = `(() => {
      const selectors = ${JSON.stringify(selectors)};
      const viewportWidth = document.documentElement.clientWidth;
      const failures = [];
      for (const selector of selectors) {
        document.querySelectorAll(selector).forEach((element, index) => {
          const rect = element.getBoundingClientRect();
          if (element.scrollWidth > element.clientWidth + 1 || rect.right > viewportWidth + 1 || rect.left < -1) {
            failures.push({
              selector,
              index,
              clientWidth: element.clientWidth,
              scrollWidth: element.scrollWidth,
              left: Math.round(rect.left),
              right: Math.round(rect.right),
            });
          }
        });
      }
      const details = document.querySelector('.ai-details');
      const panel = document.querySelector('.ai-cycle-panel');
      panel?.scrollIntoView({ block: 'start' });
      return {
        documentClientWidth: viewportWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        failures,
        aiPanelPresent: Boolean(panel),
        evidenceCollapsed: details ? !details.open : null,
        aiBadges: document.querySelectorAll('.ai-fund-badge').length,
        targetFundCodesPresent: ['016702','378006','270023','161815','378546','018229','005534']
          .every((code) => document.body.innerText.includes(code)),
      };
    })()`;
    const evaluation = await cdp("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    await delay(200);
    const screenshot = await cdp("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    });
    const screenshotPath = join(outputDir, `dashboard_ai_cycle_${viewport.name}.png`);
    await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));
    results.push({ viewport, screenshotPath, ...evaluation.result.value });
  }

  console.log(JSON.stringify(results, null, 2));
  socket.close();
  if (results.some((result) => result.failures.length > 0 || result.documentScrollWidth > result.documentClientWidth + 1)) {
    process.exitCode = 1;
  }
} finally {
  const chromeExited = chrome.exitCode !== null
    ? Promise.resolve()
    : new Promise((resolve) => chrome.once("exit", resolve));
  chrome.kill();
  await Promise.race([chromeExited, delay(3000)]);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      await rm(profileDir, { recursive: true, force: true });
      break;
    } catch (error) {
      if (!['EBUSY', 'EPERM'].includes(error.code) || attempt === 4) break;
      await delay(300);
    }
  }
}
