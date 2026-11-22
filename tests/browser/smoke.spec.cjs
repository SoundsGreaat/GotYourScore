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

  const newReview = page.getByRole("button", { name: "New Review", exact: true });
  await newReview.click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "New Bad Feedback" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("tab", { name: "QA Score" }).click();
  await expect(page.locator("#reviews-view")).toBeVisible();
  await expect(page).toHaveURL(/#my-reviews\/qa-score@\d{4}-\d{2}$/);
  await newReview.click();
  await expect(page.locator("#review-drawer-toggle")).toBeChecked();
  await expect(page.locator("#drawer-container .ql-editor")).toBeVisible();
  expect(failures).toEqual([]);
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

  await editor.getByRole("textbox", { name: "Template name" }).fill("");
  const invalid = nextResponse(page, /\/admin\/scorecards\/\d+\/save$/, "POST");
  await save.click();
  const response = await invalid;
  expect(response.status()).toBe(400);
  await expect(page.getByRole("alert").filter({ hasText: "Invalid request." })).toBeVisible();
  await expect(editor.locator("[data-display]")).toHaveValue("CI rule");
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
