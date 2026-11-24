const { expect, test } = require("@playwright/test");
const { base, nextResponse, signIn, watchPage } = require("./harness.cjs");
const htmxVersion = require("../../package-lock.json").packages["node_modules/htmx.org"].version;

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
  expect(await page.evaluate(() => window.htmx?.version)).toBe(htmxVersion);
  expect(failures).toEqual([]);
});

test("an authenticated dashboard loads the requested DB-backed HTMX partial", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  const tracker = nextResponse(page, "/partials/bad-feedback");
  expect((await page.goto(`${base}/#qa-tracker/bad-feedback`)).status()).toBe(200);
  expect((await tracker).status()).toBe(200);
  await expect(page.locator("#view-container #bad-feedback-view")).toBeVisible();
  const create = page.getByRole("button", { name: "New Bad Feedback", exact: true });
  const importExcel = page.getByRole("button", { name: "Import from Excel" });
  await expect(create).toHaveCSS("height", "36px");
  await expect(importExcel).toBeVisible();
  await expect(importExcel).not.toHaveCSS(
    "background-color", await create.evaluate((el) => getComputedStyle(el).backgroundColor)
  );
  await expect(page.locator(".gys-role-badge")).toHaveText(/^(QA · Admin|Admin · QA)$/);
  expect(await page.evaluate(() => [
    typeof window.Quill, typeof window.DOMPurify?.sanitize, typeof window.Sortable,
  ])).toEqual(["function", "function", "function"]);
  expect(failures).toEqual([]);
});

test("sidebar and category swaps preserve the hash and open the matching editors", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();

  const reviews = nextResponse(page, "/partials/my-reviews");
  await page.getByRole("link", { name: "My Reviews" }).click();
  expect(new URL((await reviews).url()).searchParams.get("cat")).toBe("bad-feedback");
  await expect(page).toHaveURL(`${base}/#my-reviews/bad-feedback`);

  await page.getByRole("button", { name: "New Bad Feedback", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "New Bad Feedback" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("tab", { name: "QA Score" }).click();
  await expect(page.locator("#reviews-view")).toBeVisible();
  await expect(page).toHaveURL(/#my-reviews\/qa-score@\d{4}-\d{2}$/);
  const newReview = page.getByRole("button", { name: "New Review", exact: true });
  await newReview.click();
  await expect(page.locator("#review-drawer-toggle")).toBeChecked();
  await expect(page.locator("#drawer-container .ql-editor")).toBeVisible();
  await page.locator("#case-type-button").click();
  await page.locator("#case-type-menu").getByText("Service Request", { exact: true }).click();
  await expect.poll(() => page.evaluate(() => JSON.parse(
    localStorage.getItem("gotyourscore:review-draft:new")
  )?.case_type)).toBe("Service Request");
  await page.locator("#drawer-container").getByText("Close", { exact: true }).click();
  await expect(page.locator("#review-drawer-toggle")).not.toBeChecked();
  await newReview.click();
  await expect(page.locator("#review-drawer-toggle")).toBeChecked();
  await expect(page.locator("#case-type-button")).toHaveText("Service Request");
  await expect(page.locator("#case-type")).toHaveValue("Service Request");
  await expect(page.locator("#case-type-button")).toHaveAccessibleDescription("Service Request");
  await page.locator("#case-type-button").click();
  await expect(page.locator("#case-type-menu").getByRole("menuitemradio", { name: "Service Request" })).toBeChecked();
  expect(failures).toEqual([]);
});

test("rapid category switches keep the newest view and errors keep the last loaded tab", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  await expect(page).toHaveURL(/#qa-tracker\/qa-score@\d{4}-\d{2}$/);

  let release;
  const holdFirst = new Promise((resolve) => { release = resolve; });
  await page.route(/\/partials\/bad-feedback\?/, async (route) => {
    await holdFirst;
    await route.abort("aborted");
  }, { times: 1 });
  const badFeedback = page.getByRole("tab", { name: "Bad Feedback" });
  const qaScore = page.getByRole("tab", { name: "QA Score" });
  try {
    const first = page.waitForRequest((request) =>
      new URL(request.url()).pathname === "/partials/bad-feedback"
    );
    await badFeedback.click();
    await first;
    await qaScore.click();
    await badFeedback.click();
    await expect(page.locator("#bad-feedback-view")).toBeVisible();
    await expect(page).toHaveURL(/#qa-tracker\/bad-feedback@\d{4}-\d{2}$/);
    await expect(badFeedback).toHaveAttribute("aria-selected", "true");
  } finally {
    release();
  }

  await page.route(/\/partials\/qa-matrix\?/, (route) => route.fulfill({
    status: 503,
    json: { detail: "Temporarily unavailable." },
  }), { times: 1 });
  const failure = nextResponse(page, "/partials/qa-matrix");
  await qaScore.click();
  await expect(page.getByRole("alert").filter({ hasText: "Temporarily unavailable." })).toBeVisible();
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  await expect(page).toHaveURL(/#qa-tracker\/bad-feedback@\d{4}-\d{2}$/);
  await expect(badFeedback).toHaveAttribute("aria-selected", "true");
  expect(failures).toEqual([`503 ${(await failure).url()}`]);
});

test("an expired dashboard session redirects the whole page to login", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  await page.context().clearCookies();
  const partial = nextResponse(page, "/partials/my-reviews");
  await page.getByRole("link", { name: "My Reviews" }).click();
  expect((await partial).headers()["hx-redirect"]).toBe("/login");
  await expect(page).toHaveURL(`${base}/login`);
  await expect(page.getByRole("link", { name: /Login with Google/ })).toBeVisible();
  expect(failures).toEqual([]);
});

test("browser back from Administration restores the dashboard view", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  const dashboard = `${base}/#qa-tracker/bad-feedback`;
  await page.goto(dashboard);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  await page.getByRole("link", { name: "Administration" }).click();
  await expect(page.locator("#admin-prompts-root")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(dashboard);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  expect(failures).toEqual([]);
});

test("Team Quotas does not overwrite the selected review period", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/#my-reviews/qa-score`);
  await page.getByRole("button", { name: "Reporting period" }).click();
  const priorPeriod = page.locator('#reviews-period-menu button[aria-checked="false"]').first();
  const period = await priorPeriod.getAttribute("data-opt-value");
  await priorPeriod.click();
  await expect(page).toHaveURL(`${base}/#my-reviews/qa-score@${period}`);
  await page.getByRole("link", { name: "Team Quotas" }).click();
  await expect(page).toHaveURL(`${base}/#team-quotas/qa-score@${period}`);
  const nextReviews = nextResponse(page, "/partials/my-reviews");
  await page.getByRole("link", { name: "My Reviews" }).click();
  expect(new URL((await nextReviews).url()).searchParams.get("period")).toBe(period);
  await expect(page).toHaveURL(`${base}/#my-reviews/qa-score@${period}`);
});

test("mobile dashboard menu is keyboard operable and does not leave the page inert", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  const quotas = page.getByRole("region", { name: "Agent review quotas" });
  await expect(quotas.getByRole("columnheader", { name: "Completed cases" })).toBeVisible();
  await quotas.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => quotas.evaluate((region) => region.scrollLeft)).toBeGreaterThan(0);
  const opener = page.getByRole("button", { name: "Open menu" });
  await opener.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Close menu" })).toBeFocused();
  await expect(page.locator("#dashboard-content")).toHaveAttribute("inert", "");
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
  await expect(opener).toHaveAttribute("aria-expanded", "false");

  await opener.press("Enter");
  await page.getByRole("link", { name: "My Reviews" }).press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "My Reviews" })).toBeFocused();
  await expect(opener).toHaveAttribute("aria-expanded", "false");
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
});

test("a delayed dashboard response does not dismiss a newly reopened mobile menu", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/partials/my-reviews?*", async (route) => {
    await held;
    await route.continue();
  });
  const opener = page.getByRole("button", { name: "Open menu" });
  const close = page.getByRole("button", { name: "Close menu" });
  await opener.click();
  const request = page.waitForRequest("**/partials/my-reviews?*");
  await page.getByRole("link", { name: "My Reviews" }).click();
  await request;
  try {
    await page.keyboard.press("Escape");
    await expect(opener).toBeFocused();
    await opener.press("Enter");
    await expect(close).toBeFocused();
  } finally {
    release();
  }
  await expect(page).toHaveURL(`${base}/#my-reviews/bad-feedback`);
  await expect(close).toBeFocused();
});

test("theme follows the system, preserves an override and syncs open tabs", async ({ page, context }) => {
  await signIn(page);
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.goto(base);
  const toggle = page.getByRole("checkbox", { name: "Dark mode" });
  const scheme = page.locator('meta[name="color-scheme"]');
  await expect(toggle).not.toBeChecked();
  await expect(scheme).toHaveAttribute("content", "light dark");
  const other = await context.newPage();
  await other.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await other.goto(base);

  await page.locator("label.toggle").click();
  await expect(scheme).toHaveAttribute("content", "dark");
  await expect(page.locator("html")).toHaveCSS("color-scheme", "dark");
  await expect(other.getByRole("checkbox", { name: "Dark mode" })).toBeChecked();
  await page.reload();
  await expect(toggle).toBeChecked();
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(scheme).toHaveAttribute("content", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(toggle).toBeChecked();

  await toggle.press("Space");
  await expect(scheme).toHaveAttribute("content", "light dark");
  await expect(other.getByRole("checkbox", { name: "Dark mode" })).not.toBeChecked();
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(toggle).toBeChecked();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await page.evaluate(() => localStorage.getItem("theme"))).toBeNull();
});
