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

  const periodMenu = page.locator('#reviews-period-menu button[aria-checked="false"]');
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

test("cloned delegation row pickers restore their own choices when a bulk override is removed", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  await page.getByRole("button", { name: "Delegate case", exact: true }).click();
  const modal = page.locator("#delegate-case-modal");
  const caseType = modal.getByRole("button", { name: "Case type", exact: true });
  const picker = modal.locator(".gys-picker:popover-open");
  const choose = (name) => picker.getByRole("menuitemradio", { name, exact: true }).click();
  await caseType.first().click();
  await choose("Service Request");
  await expect(caseType).toHaveCount(2);

  const override = modal.getByRole("button", { name: "Override case type for all rows" });
  await override.click();
  await choose("Initial Fix");
  await expect(caseType.first()).toBeDisabled();
  await expect(caseType.first()).toHaveText("Initial Fix");
  await override.click();
  await choose("— any —");
  await expect(caseType.first()).toBeEnabled();
  await expect(caseType.first()).toHaveText("Service Request");
  await expect(caseType.nth(1)).toHaveText("Select case type…");
  await caseType.first().click();
  await expect(picker.getByRole("menuitemradio", { name: "Service Request" })).toBeChecked();
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

test("admin section tabs follow the layout's keyboard direction and restore deep links", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 780 });
  await signIn(page);
  await page.goto(`${base}/admin#users`);
  const tabs = page.getByRole("tablist", { name: "Admin sections" });
  const active = tabs.getByRole("tab", { name: "Users", exact: true });
  const recovery = tabs.getByRole("tab", { name: "Deleted Users" });
  await expect(tabs).toHaveAttribute("aria-orientation", "vertical");
  await expect(page.getByRole("heading", { name: "Users and roles" })).toBeVisible();
  await active.focus();
  await page.keyboard.press("ArrowDown");
  await expect(recovery).toBeFocused();
  await expect(active).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("tabpanel", { name: "Deleted Users" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 720 });
  await expect(tabs).toHaveAttribute("aria-orientation", "horizontal");
  await expect(recovery).toBeFocused();
  await expect(recovery).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  await expect(tabs.getByRole("tab", { name: "Deleted Reviews" })).toBeFocused();
});

test("admin animates new panels and disclosures only when motion is allowed", async ({ page }) => {
  await signIn(page);
  await page.addInitScript(() => {
    window.adminTransitions = [];
    document.addEventListener("transitionrun", (event) => {
      window.adminTransitions.push(`${event.target.id}:${event.pseudoElement}:${event.propertyName}`);
    });
  });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto(`${base}/admin#ai`);
  await expect(page.locator("#admin-ai-root")).toBeVisible();
  await page.getByRole("tab", { name: "Users", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.adminTransitions)).toContain("users-root::opacity");

  const add = page.locator("#add-user-summary");
  const form = page.getByRole("form", { name: "Add user" });
  const transition = await add.evaluate(async (summary) => {
    const details = summary.parentElement;
    const started = new Promise((resolve) => details.addEventListener("toggle", () => {
      resolve(details.getAnimations().some((item) => item.effect.getKeyframes()[0].height));
    }, { once: true }));
    summary.click();
    return started;
  });
  expect(transition).toBe(true);
  await expect(form).toBeVisible();
  await add.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await expect(form).toBeHidden();
  await expect(page.getByRole("searchbox")).toBeFocused();

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("tab", { name: "Prompts", exact: true }).click();
  const prompts = page.locator("#admin-prompts-root");
  await expect(prompts).toHaveCSS("transition-duration", "0s");
  const reduced = await prompts.locator("details.gys-admin-disclosure").first().evaluate(async (details) => {
    const toggled = new Promise((resolve) => details.addEventListener("toggle", () => resolve(details.getAnimations().length), { once: true }));
    details.querySelector("summary").click();
    return toggled;
  });
  expect(reduced).toBe(0);
});

test("admin text fields use a single focus glow and keep a high contrast outline", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#scorecards`);
  const field = page.getByRole("form", { name: "New template" }).getByRole("textbox", { name: "Template name" });
  await expect(field).toBeVisible();
  const resting = await field.evaluate((el) => getComputedStyle(el).borderColor);
  await page.getByRole("tabpanel", { name: "Scorecards" }).focus();
  await page.keyboard.press("Tab");
  await expect(field).toBeFocused();
  await expect(field).toHaveCSS("outline-style", "none");
  await expect(field).not.toHaveCSS("border-color", resting);
  await expect(field).toHaveCSS("box-shadow", /0px 0px 0px 3px/);

  await page.emulateMedia({ forcedColors: "active" });
  await expect(field).toHaveCSS("outline-style", "solid");
  await expect(field).toHaveCSS("outline-width", "2px");
});

test("admin user directory keeps the search and focus after editing, deleting and restoring", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#users`);
  const add = page.locator("#add-user-summary");
  const nickname = `Directory-${randomUUID().slice(0, 8)}`;
  const search = page.getByRole("searchbox", { name: "Search by name or email" });
  await expect(search).toBeVisible();
  await expect(page.getByLabel(/Nickname/)).toBeHidden();
  await add.focus();
  await page.keyboard.press("Enter");
  await page.getByLabel(/Nickname/).fill(nickname);
  await page.getByRole("button", { name: "Add user", exact: true }).click();
  await expect(add).toBeFocused();
  await search.fill(nickname);
  const row = page.locator("#users-list li").filter({ hasText: nickname });
  const summary = row.locator("summary");
  await expect(page.locator("#users-list li:visible")).toHaveCount(1);
  await expect(page.locator("#users-count")).toHaveText(/1 of \d+ accounts/);
  await expect(row.getByRole("button", { name: "Save roles" })).toBeHidden();
  await summary.focus();
  await page.keyboard.press("Space");
  await row.getByRole("checkbox", { name: "QA", exact: true }).check();
  await row.getByRole("button", { name: "Save roles" }).click();
  await expect(row.locator("summary").getByText("QA", { exact: true })).toBeVisible();
  await expect(search).toHaveValue(nickname);
  await expect(summary).toBeFocused();
  await page.keyboard.press("Enter");
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Delete user" }).click();
  await expect(page.getByText("No accounts match your search.")).toBeVisible();
  await expect(search).toBeFocused();

  await page.setViewportSize({ width: 390, height: 720 });
  await page.getByRole("tab", { name: "Deleted Users" }).click();
  const deleted = page.getByRole("region", { name: "Deleted Users table" });
  await expect(deleted.getByRole("rowheader", { name: nickname })).toBeVisible();
  await deleted.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => deleted.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
  await deleted.getByRole("button", { name: `Restore ${nickname}` }).click();
  await expect(deleted.getByRole("rowheader", { name: nickname })).toHaveCount(0);
  await page.getByRole("tab", { name: "Users", exact: true }).click();
  await expect(summary).toBeVisible();
});

test("scorecard rules can be reordered without dragging and saved in the new order", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await signIn(page);
  await page.goto(`${base}/admin#scorecards`);
  const list = page.locator("#scorecards-root");
  await list.getByRole("textbox", { name: "Template name" }).fill(`Ordering ${randomUUID()}`);
  await list.getByRole("button", { name: "Create", exact: true }).click();
  const editor = page.locator("#scorecard-editor");
  await editor.getByRole("button", { name: "+ Add rule (new category)" }).click();
  await editor.getByRole("button", { name: "+ Add rule", exact: true }).click();
  const rules = editor.getByRole("textbox", { name: "Rule display name" });
  await rules.nth(0).fill("First");
  await rules.nth(1).fill("Second");
  const move = editor.locator("[data-row]").last().getByRole("button", { name: "Move rule up" });
  await move.focus();
  await page.keyboard.press("Enter");
  await expect(rules.nth(0)).toHaveValue("Second");
  await expect(editor.locator("#scorecard-move-status")).toHaveText("Second moved up to position 1.");
  await expect(editor.locator("[data-row]").first().getByRole("button", { name: "Move rule up" })).toBeFocused();
  await editor.getByRole("button", { name: "Save rules" }).click();
  await expect(editor.locator("[data-row]").first()).toHaveAttribute("data-item-id", /\d+/);
  await expect(rules.nth(0)).toHaveValue("Second");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("AI settings keeps optional guidance folded and can reset saved empty routing", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#ai`);
  const guidance = page.getByText("Supported JSON fields", { exact: true });
  const provider = page.getByRole("form", { name: "OpenRouter provider routing" });
  const reset = provider.getByRole("button", { name: "Reset to default" });
  await expect(guidance).toBeVisible();
  await expect(page.getByText(/order \/ only \/ ignore/)).toBeHidden();
  await guidance.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/order \/ only \/ ignore/)).toBeVisible();
  await provider.getByRole("textbox", { name: "Provider routing JSON" }).fill("{}");
  await provider.getByRole("button", { name: "Save routing" }).click();
  await expect(reset).toBeVisible();
  await expect(provider.getByRole("textbox", { name: "Provider routing JSON" })).toHaveValue("{}");
  page.once("dialog", (dialog) => dialog.accept());
  await reset.click();
  await expect(reset).toHaveCount(0);
});

test("prompt history is on demand and a stored version can be read in full", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#prompts`);
  const scoring = page.getByRole("region", { name: "AI Scoring" });
  const content = "Prompt example. ".repeat(15) + randomUUID();
  await scoring.getByLabel("Prompt content").fill(content);
  await scoring.getByRole("button", { name: "Save as active version" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Saved as the active version." })).toBeVisible();
  const history = scoring.locator("summary").filter({ hasText: "Version history" });
  await expect(scoring.locator("pre").first()).toBeHidden();
  await history.focus();
  await page.keyboard.press("Enter");
  const version = scoring.locator("summary").filter({ hasText: "Read full prompt" }).first();
  await version.focus();
  await page.keyboard.press("Enter");
  await expect(scoring.locator("pre").first()).toBeVisible();
  await expect(scoring.locator("pre").first()).toHaveText(content);
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
