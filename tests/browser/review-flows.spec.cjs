const { expect, test } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
const { base, nextResponse, signIn, watchPage } = require("./harness.cjs");

test("Refund Check is the third tracker and drafts stay in its own queue", async ({ page }) => {
  const failures = watchPage(page);
  const marker = `Refund browser ${randomUUID()}`;
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  const refundTab = page.getByRole("tab", { name: "Refund Check", exact: true });
  await expect(refundTab.locator("..").getByRole("tab")).toHaveText([
    "QA Score", "Bad Feedback", "Refund Check",
  ]);

  const refundTracker = nextResponse(page, "/partials/refund-check");
  await refundTab.click();
  expect((await refundTracker).status()).toBe(200);
  await expect(refundTab).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/#qa-tracker\/refund-check(?:@\d{4}-\d{2})?$/);
  await page.getByRole("button", { name: "New Refund Check", exact: true }).click();
  const dialog = page.getByRole("dialog").filter({
    has: page.getByRole("heading", { name: "New Refund Check", exact: true }),
  });
  await dialog.getByLabel("Source", { exact: true }).fill(marker);
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/refund-check", async (route) => {
    if (route.request().method() === "POST") await held;
    await route.continue();
  });
  const creation = nextResponse(page, "/api/refund-check", "POST");
  const saveLater = dialog.getByRole("button", { name: "Save For Later" });
  await saveLater.click();
  try {
    await expect(saveLater).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
  } finally {
    release();
  }
  const response = await creation;
  expect(response.status()).toBe(201);
  const { id } = await response.json();
  try {
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole("row").filter({ hasText: marker })).toBeVisible();

    const queue = nextResponse(page, "/partials/to-review");
    await page.getByRole("link", { name: /To review/ }).click();
    expect(new URL((await queue).url()).searchParams.get("cat")).toBe("refund-check");
    await expect(page.getByRole("row").filter({ hasText: marker })).toBeVisible();
    await page.getByRole("tab", { name: "Bad Feedback", exact: true }).click();
    await expect(page).toHaveURL(/#to-review\/bad-feedback$/);
    await expect(page.getByRole("row").filter({ hasText: marker })).toHaveCount(0);
    expect(failures).toEqual([]);
  } finally {
    await page.request.delete(`${base}/api/refund-check/${id}`);
  }
});

test("drawer keeps its rules footprint and action buttons stable while switching case types", async ({ page }) => {
  await page.setViewportSize({ width: 430, height: 820 });
  await signIn(page);
  await page.addInitScript(() => {
    const animate = Element.prototype.animate;
    window.ruleResizes = [];
    Element.prototype.animate = function (frames, options) {
      if (this.querySelector(":scope > #manual-score-area")) {
        window.ruleResizes.push(frames.map(({ height }) => parseFloat(height)));
      }
      return animate.call(this, frames, options);
    };
  });
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  let failNext = false;
  await page.route("**/partials/case-rules?*", async (route) => {
    if (failNext) {
      failNext = false;
      await route.fulfill({ status: 503 });
      return;
    }
    const type = new URL(route.request().url()).searchParams.get("case_type");
    if (type === "Service Request") await held;
    const count = type === "Initial Fix" ? 24 : 3;
    const rows = Array.from({ length: count }, (_, i) =>
      `<div data-rule-row class="p-2"><label><input type="checkbox" data-error-name="test-${i}">Rule ${i}</label></div>`
    );
    await route.fulfill({ contentType: "text/html", body: rows.join("") });
  });
  await page.goto(`${base}/#qa-tracker/qa-score`);
  await page.getByRole("button", { name: "New Review", exact: true }).click();

  const rules = page.locator("#manual-score-area");
  const rulesPanel = rules.locator("..");
  const footer = page.locator(".js-save-review");
  await expect(rules.locator("[data-rule-row]")).toHaveCount(24);
  await rulesPanel.evaluate((el) => Promise.all(el.getAnimations().map((a) => a.finished)));
  const oldHeight = (await rulesPanel.boundingBox()).height;
  const actionY = (await footer.boundingBox()).y;
  const select = async (type) => {
    await page.locator("#case-type-button").click();
    await page.locator("#case-type-menu").getByRole("menuitemradio", { name: type, exact: true }).click();
  };

  try {
    await select("Service Request");
    await expect(rules).toHaveAttribute("aria-busy", "true");
    await expect(rules).toHaveAttribute("inert", "");
    await expect(rulesPanel.getByRole("status")).toHaveText("Loading scorecard…");
    expect((await rulesPanel.boundingBox()).height).toBeCloseTo(oldHeight, 0);
    expect((await footer.boundingBox()).y).toBeCloseTo(actionY, 0);
  } finally {
    release();
  }
  await expect(rules.locator("[data-rule-row]")).toHaveCount(3);
  await expect(rules).not.toHaveAttribute("inert", "");
  const [start, end] = await page.evaluate(() => window.ruleResizes.at(-1));
  expect(start).toBeCloseTo(oldHeight, 0);
  expect(end).toBeLessThan(start);
  expect((await footer.boundingBox()).y).toBeCloseTo(actionY, 0);

  await page.emulateMedia({ reducedMotion: "reduce" });
  const before = await page.evaluate(() => window.ruleResizes.length);
  await select("No Cases");
  await expect(rules).toContainText("The agent had no cases");
  await expect(page.getByRole("textbox", { name: "Case number", exact: true })).toBeHidden();
  expect(await page.evaluate(() => window.ruleResizes.length)).toBe(before);

  await page.locator("#review-agent-button").click();
  await page.locator("#review-agent-menu").getByRole("menuitemradio").first().click();
  await expect(footer).toBeEnabled();
  failNext = true;
  await select("Initial Fix");
  const retry = page.getByRole("button", { name: "Retry scorecard" });
  await expect(retry).toBeVisible();
  await expect(footer).toBeDisabled();
  await retry.click();
  await expect(rules.locator("[data-rule-row]")).toHaveCount(24);
  await expect(footer).toBeEnabled();
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

test("To Review queues only refetch for updates in their own category", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/#to-review/qa-score`);
  const queue = page.locator("#to-review-view");
  for (const [tab, category, relevant, unrelated] of [
    ["QA Score", "qa-score", "reviews-changed", "bad-feedback-changed"],
    ["Bad Feedback", "bad-feedback", "bad-feedback-changed", "reviews-changed"],
  ]) {
    await page.getByRole("tab", { name: tab, exact: true }).click();
    await expect(queue).toHaveAttribute("hx-trigger", `${relevant} from:body`);
    const activity = await queue.evaluate(async (section, type) => {
      let requests = 0;
      const count = () => { requests += 1; };
      section.addEventListener("htmx:before:request", count);
      document.body.dispatchEvent(new CustomEvent(type));
      await new Promise(requestAnimationFrame);
      section.removeEventListener("htmx:before:request", count);
      return requests;
    }, unrelated);
    expect(activity).toBe(0);
    const refreshed = nextResponse(page, "/partials/to-review");
    await page.evaluate((type) => document.body.dispatchEvent(new CustomEvent(type)), relevant);
    expect(new URL((await refreshed).url()).searchParams.get("cat")).toBe(category);
  }
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

test("the review drawer restores the delegated QA and can clear the assignment", async ({ page }) => {
  await signIn(page);
  const me = await (await page.request.get(`${base}/auth/me`)).json();
  const nickname = me.email[0].toUpperCase() + me.email.split("@")[0].slice(1);
  const caseNumber = `Delegated ${randomUUID()}`;

  await page.goto(`${base}/#qa-tracker/qa-score`);
  await page.getByRole("button", { name: "Delegate case", exact: true }).click();
  const modal = page.locator("#delegate-case-modal");
  const picker = modal.locator(".gys-picker:popover-open");
  await modal.getByRole("button", { name: "QA agent (optional)", exact: true }).first().click();
  await picker.getByRole("menuitemradio", { name: nickname, exact: true }).click();
  await modal.getByRole("button", { name: "Support agent", exact: true }).first().click();
  await picker.getByRole("menuitemradio").first().click();
  await modal.getByRole("button", { name: "Case type", exact: true }).first().click();
  await picker.getByRole("menuitemradio", { name: "Initial Fix", exact: true }).click();
  await modal.getByRole("textbox", { name: "Case number (optional)" }).first().fill(caseNumber);
  const delegation = nextResponse(page, "/api/reviews/pending/bulk", "POST");
  await modal.getByRole("button", { name: /^Delegate 1 case/ }).click();
  const { created: [{ id }] } = await (await delegation).json();

  try {
    await modal.getByRole("button", { name: "Close delegation" }).click();
    await page.getByRole("link", { name: /To review/ }).click();
    const row = page.locator("#to-review-view").getByRole("row").filter({ hasText: caseNumber });
    const open = row.getByRole("button", { name: `Complete delegated case ${caseNumber}`, exact: true });
    await open.click();
    await expect(page.getByRole("heading", { name: "Complete case", exact: true })).toBeVisible();
    const assignee = page.getByRole("button", { name: "Assigned QA", exact: true });
    await expect(assignee).toHaveText(nickname);
    await assignee.press("ArrowDown");
    await expect(page.getByRole("menuitemradio", { name: nickname, exact: true })).toBeChecked();
    await page.getByRole("menuitemradio", { name: "Unassigned — shared queue" }).press("Enter");
    const save = nextResponse(page, `/api/reviews/${id}`, "PATCH");
    await page.getByRole("button", { name: "Save Changes", exact: true }).click();
    const response = await save;
    expect(response.status()).toBe(200);
    expect((await response.json()).assigned_qa_id).toBeNull();

    await expect(assignee).toBeHidden();
    await open.click();
    await expect(assignee).toBeVisible();
    await expect(assignee).toHaveText("Unassigned — shared queue");
  } finally {
    await page.request.delete(`${base}/api/reviews/${id}`);
  }
});

test("Save For Later restores the author as assignee even without the QA role", async ({ page }) => {
  await signIn(page);
  const me = await (await page.request.get(`${base}/auth/me`)).json();
  const nickname = me.email[0].toUpperCase() + me.email.split("@")[0].slice(1);
  const rolesPath = `${base}/admin/users/${me.id}/roles`;
  const headers = { "HX-Request": "true" };
  const caseNumber = `Draft ${randomUUID()}`;
  let id;

  try {
    expect((await page.request.post(rolesPath, { headers, form: { roles: "Admin" } })).ok()).toBe(true);
    await page.goto(`${base}/#qa-tracker/qa-score`);
    await page.getByRole("button", { name: "New Review", exact: true }).click();
    await page.locator("#review-agent-button").click();
    await page.locator("#review-agent-picker").getByRole("menuitemradio").first().click();
    await page.getByRole("textbox", { name: "Case number", exact: true }).fill(caseNumber);
    const creation = nextResponse(page, "/api/reviews", "POST");
    await page.getByRole("button", { name: "Save For Later", exact: true }).click();
    const draft = await (await creation).json();
    id = draft.id;
    expect(draft.assigned_qa_id).toBe(me.id);

    await page.getByRole("link", { name: /To review/ }).click();
    const open = page.locator("#to-review-view").getByRole("button", {
      name: `Complete delegated case ${caseNumber}`, exact: true,
    });
    await open.click();
    await expect(page.getByRole("button", { name: "Assigned QA", exact: true })).toHaveText(nickname);
    const save = nextResponse(page, `/api/reviews/${id}`, "PATCH");
    await page.getByRole("button", { name: "Save Changes", exact: true }).click();
    const response = await save;
    expect(response.status()).toBe(200);
    expect((await response.json()).assigned_qa_id).toBe(me.id);

    await expect(page.locator(".js-review-drawer")).toBeHidden();
    await open.click();
    const completion = nextResponse(page, `/api/reviews/${id}`, "PATCH");
    await page.getByRole("button", { name: "Complete review", exact: true }).click();
    const completed = await completion;
    expect(completed.status()).toBe(200);
    expect(await completed.json()).toMatchObject({ assigned_qa_id: me.id, status: "completed" });
  } finally {
    if (id) await page.request.delete(`${base}/api/reviews/${id}`);
    expect((await page.request.post(rolesPath, { headers, form: { roles: "QA" } })).ok()).toBe(true);
  }
});
