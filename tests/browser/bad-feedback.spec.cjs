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
  await staleResponse;
  await expect(dialog.locator(".js-bf-step-map")).toBeHidden();
  await expect(dialog.getByRole("button", { name: "Analyze" })).toBeDisabled();
});

test("successful report remains visible until dismissed, then changed event swaps tracker", async ({ page }) => {
  await signIn(page);
  await page.route("**/api/bad-feedback/import/inspect", (route) =>
    route.fulfill({ json: inspection() }));
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
