/* Optional browser regression test: node tests/test_scene_map_browser.cjs
 * Requires Playwright and an installed Chromium browser. Uses synthetic tiles;
 * no Earth Engine/Supabase credentials or Python services are needed.
 */
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

const componentDir = path.join(__dirname, "../components/scene_map");
const names = {true_color: "True color", cloud: "Cloud / shadow mask",
  ndvi: "NDVI", lst: "LST (°C)", eci: "ECI"};
const defaults = {visible: ["true_color", "cloud"], preferred: "true_color",
  cloud_opacity: 0.55, boundary: true};
const tilePng = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAGUlEQVR4nGP0n1nIQApgIkn1qIZRDUNKAwBSCAF5S1lG2AAAAABJRU5ErkJggg==", "base64");
const boundary = {type: "Polygon", coordinates: [[[15.8, 45.7], [16.2, 45.7],
  [16.2, 45.9], [15.8, 45.9], [15.8, 45.7]]]};

const server = http.createServer((req, res) => {
  if (req.url === "/") {
    res.setHeader("Content-Type", "text/html");
    res.end(`<iframe src="/component/index.html" style="width:100%;height:640px;border:0"></iframe>
      <script>window.events=[]; addEventListener('message',e=>{
      if(e.data.type==='streamlit:setComponentValue') window.events.push(e.data.value);
      });</script>`);
  } else if (req.url.startsWith("/tiles/")) {
    if (req.url.startsWith("/tiles/broken/")) {
      res.writeHead(503).end(); return;
    }
    res.setHeader("Content-Type", "image/png");
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Cache-Control", "max-age=3600");
    res.end(tilePng);
  } else {
    const name = req.url.replace("/component/", "");
    if (!["index.html", "map.js", "style.css"].includes(name)) {
      res.writeHead(404).end(); return;
    }
    res.setHeader("Content-Type", name.endsWith("js") ? "application/javascript"
      : name.endsWith("css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(path.join(componentDir, name)));
  }
});

(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 760}, ignoreHTTPSErrors: true});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    // Only scene tiles matter for these tests; avoid a live basemap dependency.
    await page.route("**/*.basemaps.cartocdn.com/**", route => route.fulfill({
      contentType: "image/png", headers: {"Access-Control-Allow-Origin": "*"},
      body: tilePng,
    }));
    await page.goto(origin);
    const frame = page.frames().find(item => item.url().includes("/component/"));
    await frame.waitForFunction(() => typeof L !== "undefined");
    const payload = (scene, ready = [], failures = {}) => ({
      scene_id: scene, image_id: scene, city: "zagreb", center: [45.8, 16], boundary,
      preferences: defaults,
      layers: Object.entries(names).map(([key, name]) => ({key, name,
        url: ready.includes(key) ? `${origin}/tiles/${scene}/${key}/{z}/{x}/{y}.png` : null,
        error: failures[key] || null,
      })),
    });
    const render = data => page.evaluate(data => {
      document.querySelector("iframe").contentWindow.postMessage({type: "streamlit:render", args: data}, "*");
    }, data);
    const check = name => frame.getByRole("checkbox", {name, exact: true});
    const waitTiles = () => frame.waitForFunction(() =>
      !document.getElementById("status").textContent.includes("image tiles…"));

    await render(payload("a", ["true_color"]));
    await frame.locator(".leaflet-true_color-pane img").first().waitFor();
    await check("Cloud / shadow mask").uncheck();
    await frame.getByRole("button", {name: "Zoom in"}).click();
    await waitTiles();
    await frame.evaluate(() => {
      window.testTile = document.querySelector(".leaflet-true_color-pane img");
      window.testView = document.querySelector(".leaflet-map-pane").style.transform;
    });
    await render(payload("a", ["true_color", "ndvi"]));
    await frame.waitForFunction(() => [...document.querySelectorAll("label")].some(label =>
      label.textContent.includes("NDVI") && label.textContent.includes("Ready")));
    assert(await frame.evaluate(() => window.testTile.isConnected), "New layers must leave displayed tiles in place");
    assert(await frame.evaluate(() => window.testView === document.querySelector(".leaflet-map-pane").style.transform),
      "New layers must preserve the map view");
    assert.equal(await check("NDVI").isChecked(), false, "Ready layers must stay hidden until selected");

    await check("NDVI").check();
    await check("True color").uncheck();
    await waitTiles();
    const opacity = frame.getByRole("slider", {name: "Cloud mask opacity"});
    await opacity.fill("0.35");
    await opacity.dispatchEvent("change");
    await render(payload("b"));
    await frame.waitForFunction(() => document.getElementById("status").textContent.includes("Preparing NDVI"));
    assert.equal(await check("NDVI").isChecked(), true);
    assert.equal(await check("True color").isChecked(), false);
    assert.equal(await opacity.inputValue(), "0.35");
    assert.equal(await frame.locator(".leaflet-ndvi-pane img").count(), 0, "Old dates must disappear while the new date prepares");
    await render(payload("b", ["ndvi"]));
    await frame.locator(".leaflet-ndvi-pane img").first().waitFor();
    await waitTiles();
    assert.equal(await frame.locator(".leaflet-true_color-pane img").count(), 0);

    await render(payload("b", [], {ndvi: "Test failure"}));
    const retry = frame.getByRole("button", {name: "Retry NDVI", exact: true});
    await retry.waitFor();
    await retry.click();
    await page.waitForFunction(() => window.events.some(event => event.retry === "ndvi"));
    const event = await page.evaluate(() => window.events.findLast(event => event.retry === "ndvi"));
    assert.equal(event.scene_id, "b");
    assert.deepEqual(event.visible, ["ndvi"]);
    assert.equal(event.preferred, "ndvi");
    assert.equal(event.cloud_opacity, 0.35);
    await render(payload("a", ["ndvi"]));
    await frame.locator(".leaflet-ndvi-pane img").first().waitFor();
    await waitTiles();

    const download = page.waitForEvent("download").catch(async error => {
      console.error("Export status:", await frame.locator("#status").textContent());
      console.error("Browser errors:", errors);
      throw error;
    });
    await frame.getByRole("button", {name: "Download the current map view as PNG"}).click();
    const image = await download;
    assert.equal(image.suggestedFilename(), "utomika_a.png");
    const downloaded = await image.path();
    assert(fs.statSync(downloaded).size > 1000, "PNG export must contain image data");

    await render(payload("broken", ["ndvi"]));
    await frame.getByRole("button", {name: "Retry NDVI", exact: true}).waitFor();
    assert.match(await frame.locator("#status").textContent(), /could not be loaded/);
    await frame.getByRole("button", {name: "Retry NDVI", exact: true}).click();
    await page.waitForFunction(() => window.events.some(event => event.retry === "ndvi" && event.scene_id === "broken"));
    await render(payload("a", ["ndvi"]));
    await frame.locator(".leaflet-ndvi-pane img").first().waitFor();
    await waitTiles();
    assert.deepEqual(errors, [], "Component must not raise browser errors");
    console.log("PASS: persistent tiles/view, selected layer across dates, opacity, scene-safe retry, tile failures, and PNG export");
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
