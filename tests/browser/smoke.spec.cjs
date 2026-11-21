const { expect, test } = require("@playwright/test");

const base = process.env.APP_BASE_URL;
const session = process.env.CI_SESSION_COOKIE;
if (!base || !session || new URL(base).hostname !== "127.0.0.1") {
  throw new Error("Start the isolated Compose stack and set APP_BASE_URL and CI_SESSION_COOKIE");
}

function watchPage(page) {
  const failures = [];
  page.on("pageerror", (error) => failures.push(error.message));
  page.on("requestfailed", (request) => {
    if (request.failure().errorText !== "net::ERR_ABORTED") {
      failures.push(`${request.url()}: ${request.failure().errorText}`);
    }
  });
  page.on("response", (response) => {
    if (response.status() >= 400) failures.push(`${response.status()} ${response.url()}`);
  });
  return failures;
}

test("the production server renders login and serves the generated assets", async ({ page }) => {
  const failures = watchPage(page);
  const response = await page.goto(base);
  expect(response.status()).toBe(200);
  await expect(page).toHaveURL(`${base}/login`);
  await expect(page.getByRole("heading", { name: "GotYourScore" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Login with Google/ })).toHaveAttribute(
    "href", "/auth/login"
  );
  await expect(page.locator("h1")).toHaveCSS("font-weight", "800");
  expect(await page.evaluate(() => typeof window.htmx)).toBe("object");
  expect(failures).toEqual([]);
});

test("an authenticated dashboard loads the requested DB-backed HTMX partial", async ({ page }) => {
  const failures = watchPage(page);
  await page.context().addCookies([{ name: "session", value: session, url: base, httpOnly: true }]);
  const tracker = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/partials/bad-feedback"
  );
  expect((await page.goto(`${base}/#qa-tracker/bad-feedback`)).status()).toBe(200);
  expect((await tracker).status()).toBe(200);
  await expect(page.locator("#view-container #bad-feedback-view")).toBeVisible();
  await expect(page.getByRole("button", { name: "Import from Excel" })).toBeVisible();
  expect(await page.evaluate(() => [
    typeof window.Quill, typeof window.DOMPurify?.sanitize, typeof window.Sortable,
  ])).toEqual(["function", "function", "function"]);
  expect(failures).toEqual([]);
});
