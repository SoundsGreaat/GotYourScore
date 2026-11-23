const { expect, test } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
const htmxVersion = require("../../package-lock.json").packages["node_modules/htmx.org"].version;

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

async function signIn(page) {
  await page.context().addCookies([{ name: "session", value: session, url: base, httpOnly: true }]);
}

function nextResponse(page, path, method = "GET") {
  return page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    const matches = typeof path === "string" ? pathname === path : path.test(pathname);
    return matches && response.request().method() === method;
  });
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
  await expect(page.getByRole("button", { name: "Import from Excel" })).toBeVisible();
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

test("review filters survive period changes and event-driven outer swaps", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/#my-reviews/qa-score`);
  await expect(page).toHaveURL(/#my-reviews\/qa-score@\d{4}-\d{2}$/);
  const search = page.getByRole("searchbox", { name: "Search by case number" });
  const filter = nextResponse(page, "/partials/my-reviews");
  await search.fill("CI-FILTER");
  expect(new URL((await filter).url()).searchParams.get("case_number")).toBe("CI-FILTER");
  await expect(page.getByRole("link", { name: "Reset filters" })).toBeVisible();

  const periodMenu = page.locator('#reviews-period-menu button:not(.active)');
  await page.getByRole("button", { name: "Reporting period" }).click();
  const period = await periodMenu.first().getAttribute("data-opt-value");
  const change = nextResponse(page, "/partials/my-reviews");
  await periodMenu.first().click();
  const params = new URL((await change).url()).searchParams;
  expect(params.get("period")).toBe(period);
  expect(params.get("case_number")).toBe("CI-FILTER");
  await expect(page).toHaveURL(`${base}/#my-reviews/qa-score@${period}`);

  const refresh = nextResponse(page, "/partials/my-reviews");
  await page.evaluate(() => document.body.dispatchEvent(new CustomEvent("reviews-changed")));
  const refreshed = new URL((await refresh).url()).searchParams;
  expect(refreshed.get("case_number")).toBe("CI-FILTER");
  expect(refreshed.get("period")).toBe(period);
  await expect(search).toHaveValue("CI-FILTER");
  expect(failures).toEqual([]);
});

test("a background QA matrix refresh does not discard an open delegation draft", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  await page.getByRole("button", { name: "Delegate case", exact: true }).click();
  const modal = page.locator("#delegate-case-modal");
  const caseNumber = modal.getByRole("textbox", { name: "Case number (optional)" }).first();
  await caseNumber.fill("CI-DRAFT");
  const canceled = await page.evaluate(() => {
    const section = document.querySelector("#delegate-case-modal").closest("section");
    const guard = new Promise((resolve) => {
      document.addEventListener(window.htmx.version.startsWith("4.") ?
        "htmx:before:request" : "htmx:beforeRequest", (event) => {
        if (event.target === section) resolve(event.defaultPrevented);
      });
    });
    document.body.dispatchEvent(new CustomEvent("reviews-changed"));
    return guard;
  });
  expect(canceled).toBe(true);
  await expect(modal).toBeVisible();
  await expect(caseNumber).toHaveValue("CI-DRAFT");
  expect(failures).toEqual([]);
});

test("admin scorecard saves swap scripts and validation errors leave the editor intact", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/admin`);
  await expect(page.locator("#admin-prompts-root")).toBeVisible();
  await page.getByRole("tab", { name: "Scorecards" }).click();
  const template = `CI template ${randomUUID()}`;
  await page.locator("#scorecards-root").getByRole("textbox", { name: "Template name" }).fill(template);
  const created = nextResponse(page, "/admin/scorecards/create", "POST");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  expect((await created).status()).toBe(200);

  const editor = page.locator("#scorecard-editor");
  await expect(editor.getByRole("textbox", { name: "Template name" })).toHaveValue(template);
  await editor.getByRole("button", { name: "+ Add rule (new category)" }).click();
  await editor.locator("[data-display]").fill("CI rule");
  const save = editor.getByRole("button", { name: "Save rules" });
  await save.click();
  await expect(page.getByRole("alert").filter({ hasText: "Scorecard saved." })).toBeVisible();
  await expect(editor.locator("[data-row]")).toHaveAttribute("data-item-id", /\d+/);

  const rule = editor.locator("[data-display]");
  const savePath = /\/admin\/scorecards\/\d+\/save$/;
  let release;
  const holdSave = new Promise((resolve) => { release = resolve; });
  await page.route(savePath, async (route) => {
    await holdSave;
    await route.continue();
  }, { times: 1 });
  let lastSave;
  try {
    const first = page.waitForRequest((request) => savePath.test(new URL(request.url()).pathname));
    await rule.fill("First edit");
    await save.click();
    await first;
    await rule.fill("Intermediate edit");
    await save.click();
    await rule.fill("Last edit");
    await save.click();
    lastSave = page.waitForResponse((response) => {
      const request = response.request();
      return savePath.test(new URL(response.url()).pathname)
        && new URLSearchParams(request.postData()).get("display_name_0") === "Last edit";
    });
  } finally {
    release();
  }
  expect((await lastSave).status()).toBe(200);
  await expect(rule).toHaveValue("Last edit");

  await editor.getByRole("textbox", { name: "Template name" }).fill("");
  const invalid = nextResponse(page, savePath, "POST");
  await save.click();
  const response = await invalid;
  expect(response.status()).toBe(400);
  await expect(page.getByRole("alert").filter({ hasText: "Invalid request." })).toBeVisible();
  await expect(rule).toHaveValue("Last edit");
  expect(failures).toEqual([`400 ${response.url()}`]);
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

test("mobile dashboard menu is keyboard operable and does not leave the page inert", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  await expect(page.getByRole("tab", { name: "QA Score" })).toHaveAttribute("aria-selected", "true");
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

test("admin tabs scroll on mobile and select only after a successful swap", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await signIn(page);
  await page.goto(`${base}/admin`);
  await expect(page.locator("#admin-prompts-root")).toBeVisible();
  const prompts = page.getByRole("tab", { name: "Prompts" });
  const users = page.getByRole("tab", { name: "Deleted Bad Feedback" });
  await prompts.focus();
  await page.keyboard.press("End");
  await expect(users).toBeFocused();
  await expect(prompts).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: "Prompts" })).toBeVisible();
  await expect.poll(() => page.getByRole("tablist").evaluate((track) => track.scrollLeft)).toBeGreaterThan(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);

  await page.route("**/admin/partials/deleted-bad-feedback", (route) => route.fulfill({
    status: 503,
    json: { detail: "Temporarily unavailable." },
  }), { times: 1 });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "Temporarily unavailable." })).toBeVisible();
  await expect(page.getByRole("tabpanel", { name: "Prompts" })).toBeVisible();
  await expect(page).toHaveURL(`${base}/admin`);
  await page.keyboard.press("Space");
  await expect(page.getByRole("tabpanel", { name: "Deleted Bad Feedback" })).toBeVisible();
  await expect(page).toHaveURL(`${base}/admin#deleted-bad-feedback`);
});

test("Team Quotas does not overwrite the selected review period", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/#my-reviews/qa-score`);
  await page.getByRole("button", { name: "Reporting period" }).click();
  const priorPeriod = page.locator("#reviews-period-menu button:not(.active)").first();
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
