const { expect, test } = require("@playwright/test");

const base = process.env.APP_BASE_URL;
const session = process.env.CI_SESSION_COOKIE;
if (!base || !session || new URL(base).hostname !== "127.0.0.1") {
  throw new Error("Start the isolated Compose stack and set APP_BASE_URL and CI_SESSION_COOKIE");
}

async function signIn(page) {
  await page.context().addCookies([{ name: "session", value: session, url: base, httpOnly: true }]);
}

async function openImport(page) {
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  await page.getByRole("button", { name: "Import from Excel" }).click();
  return page.getByRole("dialog").filter({ has: page.getByRole("heading", { name: "Import Bad Feedback" }) });
}

function inspection(headers = ["Customer"]) {
  return {
    headers,
    suggestions: {},
    total_rows: 1,
    preview: [{ row_number: 2, values: ["Example"] }],
  };
}

test("delayed inspection is disabled in flight and cannot update a closed/reopened importer", async ({ page }) => {
  await signIn(page);
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/bad-feedback/import/inspect", async (route) => {
    await held;
    await route.fulfill({ json: inspection(["Stale header"]) });
  });

  const dialog = await openImport(page);
  await dialog.locator(".js-bf-file").setInputFiles({
    name: "first.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("first"),
  });
  const request = page.waitForRequest("**/api/bad-feedback/import/inspect");
  await dialog.getByRole("button", { name: "Analyze" }).click();
  await request;
  await expect(dialog.getByRole("button", { name: "Analyzing…" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "Import from Excel" }).click();
  await expect(dialog).toBeVisible();
  const staleResponse = page.waitForResponse("**/api/bad-feedback/import/inspect");
  release();
  await (await staleResponse).finished();
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  await expect(dialog.locator(".js-bf-step-map")).toBeHidden();
  await expect(dialog.getByRole("button", { name: "Analyze" })).toBeDisabled();
});

test("successful report remains visible until dismissed, then changed event swaps tracker", async ({ page }) => {
  await signIn(page);
  const headers = Array.from({ length: 10 }, (_, index) => `Column ${index + 1}`).concat("Customer email");
  await page.route("**/api/bad-feedback/import/inspect", (route) =>
    route.fulfill({ json: inspection(headers) }));
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/bad-feedback/import", async (route) => {
    await held;
    await route.fulfill({ json: { created_count: 1, skipped_count: 0, created_users: [], skipped: [] } });
  });

  const dialog = await openImport(page);
  await dialog.locator(".js-bf-file").setInputFiles({
    name: "report.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("fixture"),
  });
  await dialog.getByRole("button", { name: "Analyze" }).click();
  await expect(dialog.locator(".js-bf-step-map")).toBeVisible();
  const mapping = dialog.locator("#bf-map-customer_info-button");
  const picker = dialog.locator("#bf-map-customer_info-picker");
  await mapping.press("Enter");
  const search = picker.getByRole("searchbox");
  await search.fill("email");
  await search.press("Enter");
  await expect(dialog.locator("#bf-map-customer_info")).toHaveValue("Customer email");
  await mapping.click();
  await expect(picker.getByRole("menuitemradio", { name: "Customer email" })).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(dialog.locator(".js-bf-step-map")).toBeVisible();
  await dialog.getByRole("button", { name: "Import", exact: true }).click();
  try {
    await expect(dialog.getByRole("button", { name: "Importing…" })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    const refreshCanceled = await page.evaluate(() => {
      const section = document.querySelector("#bad-feedback-view");
      const cancellation = new Promise((resolve) => {
        document.addEventListener("htmx:before:request", (event) => {
          if (event.target === section) resolve(event.defaultPrevented);
        }, { once: true });
      });
      document.body.dispatchEvent(new CustomEvent("bad-feedback-changed"));
      return cancellation;
    });
    expect(refreshCanceled).toBe(true);
  } finally {
    release();
  }
  await expect(dialog.locator(".js-bf-step-report")).toBeVisible();
  await expect(dialog.locator(".js-bf-report-body")).toContainText("1 record(s) imported.");
  await expect(dialog).toBeVisible();

  const refresh = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/partials/bad-feedback"
      && response.request().method() === "GET");
  await dialog.getByRole("button", { name: "Done" }).click();
  expect((await refresh).status()).toBe(200);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  await expect(page.getByRole("button", { name: "Import from Excel" })).toBeVisible();
});

test("Bad Feedback editor cards share the new picker behavior", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await page.getByRole("button", { name: "New Bad Feedback" }).click();
  const editor = page.locator(".js-bf-edit-modal");
  await editor.getByRole("button", { name: "+ Add agent", exact: true }).click();
  const agent = editor.getByRole("button", { name: "Agent", exact: true }).first();
  expect((await agent.boundingBox()).width).toBeGreaterThan(100);
  const type = editor.getByRole("button", { name: "Agent type" }).first();
  await type.click();
  const picker = editor.locator(".gys-picker:popover-open");
  await expect(picker.getByRole("menuitemradio", { name: "Support" })).toBeChecked();
  await expect(picker.getByRole("searchbox")).toHaveCount(0);
  await picker.getByRole("menuitemradio", { name: "Sales" }).click();
  await expect(type).toHaveText("Sales");
  await type.click();
  await expect(picker.getByRole("menuitemradio", { name: "Sales" })).toBeChecked();
});

test("Bad Feedback agent cards resize the open dialog and keep focus on removal", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await page.getByRole("button", { name: "New Bad Feedback" }).click();
  const dialog = page.getByRole("dialog");
  const add = dialog.getByRole("button", { name: "+ Add agent", exact: true });
  const container = dialog.locator(".js-bf-agents");
  await expect(add).toBeVisible();
  const heights = await container.evaluate(async (el) => {
    el.nextElementSibling.click();
    const motion = el.getAnimations()[0];
    const frames = motion?.effect.getKeyframes().map(({ height }) => parseFloat(height));
    await motion?.finished;
    return frames;
  });
  expect(heights[0]).toBe(0);
  expect(heights[1]).toBeGreaterThan(0);

  await page.emulateMedia({ reducedMotion: "reduce" });
  const remove = dialog.getByRole("button", { name: "Remove agent" });
  await remove.focus();
  await page.keyboard.press("Space");
  await expect(remove).toHaveCount(0);
  await expect(add).toBeFocused();
  expect(await container.evaluate((el) => el.getAnimations().length)).toBe(0);
});
