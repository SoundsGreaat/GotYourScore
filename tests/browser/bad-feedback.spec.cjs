const { expect, test } = require("@playwright/test");
const { base, signIn } = require("./harness.cjs");

async function openImport(page) {
  await page.goto(`${base}/#qa-tracker/bad-feedback`);
  await expect(page.locator("#bad-feedback-view")).toBeVisible();
  await page.getByRole("button", { name: "Import from Excel" }).click();
  return page.getByRole("dialog").filter({ has: page.getByRole("heading", { name: "Import Bad Feedback" }) });
}

function inspection(headers = ["Customer"], config = {}) {
  return {
    headers,
    suggestions: {},
    total_rows: 1,
    preview: [{ row_number: 2, values: ["Example"] }],
    field_labels: {
      customer_info: "Customer info",
      support_agents: "RA Support agents assigned",
      sales_agents: "Sales agents assigned",
    },
    required_fields: [],
    ...config,
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

test("required import mappings block submission until corrected", async ({ page }) => {
  await signIn(page);
  await page.route("**/api/bad-feedback/import/inspect", (route) => route.fulfill({
    json: inspection(["Customer", "Reference"], {
      field_labels: {
        customer_info: "Customer details",
        custom_reference: "Case reference",
        support_agents: "RA Support agents assigned",
        sales_agents: "Sales agents assigned",
      },
      required_fields: ["customer_info", "custom_reference"],
    }),
  }));
  let commits = 0;
  await page.route("**/api/bad-feedback/import", (route) => {
    commits++;
    return route.fulfill({ json: { created_count: 1, skipped_count: 0, created_users: [], skipped: [] } });
  });

  const dialog = await openImport(page);
  await dialog.locator(".js-bf-file").setInputFiles({
    name: "required.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("fixture"),
  });
  await dialog.getByRole("button", { name: "Analyze" }).click();
  const customer = dialog.getByRole("button", { name: "Column for Customer details (required)", exact: true });
  const reference = dialog.getByRole("button", { name: "Column for Case reference (required)", exact: true });
  const importButton = dialog.getByRole("button", { name: "Import", exact: true });
  const error = dialog.getByRole("alert");
  await expect(dialog.locator('label[for="bf-map-customer_info-button"]')).toHaveText("Customer details (required)");
  await expect(customer).not.toHaveAttribute("aria-invalid", "true");
  await importButton.press("Enter");
  await expect(error).toHaveText("Choose spreadsheet columns for: Customer details, Case reference.");
  await expect(customer).toBeFocused();
  await expect(customer).toHaveAttribute("aria-invalid", "true");
  await expect(reference).toHaveAttribute("aria-invalid", "true");
  expect(commits).toBe(0);

  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByRole("menuitemradio", { name: "Not imported" })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(dialog.locator("#bf-map-customer_info")).toHaveValue("Customer");
  await expect(customer).not.toHaveAttribute("aria-invalid", "true");
  await expect(error).toHaveText("Choose spreadsheet columns for: Case reference.");

  await reference.click();
  await dialog.getByRole("menuitemradio", { name: "Reference", exact: true }).click();
  await expect(reference).not.toHaveAttribute("aria-invalid", "true");
  await expect(error).toBeEmpty();
  await importButton.press("Enter");
  await expect(dialog.locator(".js-bf-step-report")).toContainText("1 record(s) imported.");
  expect(commits).toBe(1);
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

test("tracker notes open the specific agent card and respect each view's edit mode", async ({ page }) => {
  await signIn(page);
  const me = await page.request.get(`${base}/auth/me`);
  expect(me.ok()).toBe(true);
  const { id: userId } = await me.json();
  const response = await page.request.post(`${base}/api/refund-check`, {
    data: {
      related_case: "CI-notes",
      agents: [
        { user_id: userId, kind: "Support", fault: "Full Fault", qa_comment: "<strong>Support note</strong>" },
        { user_id: userId, kind: "Sales", fault: "Warning", qa_comment: "Sales note" },
      ],
    },
  });
  expect(response.status()).toBe(201);
  const { id, agents } = await response.json();
  const support = agents.find((agent) => agent.kind === "Support");
  const sales = agents.find((agent) => agent.kind === "Sales");
  const badge = (agent) => page.locator(`[data-case-notes="refund-check-${id}-${agent.id}"]`);
  const notes = page.locator(".js-notes-modal");
  const edit = notes.getByRole("button", { name: "Edit record" });
  const close = notes.getByRole("button", { name: "Close", exact: true });
  try {
    await page.goto(`${base}/#qa-tracker/refund-check`);
    await expect(badge(support)).toHaveClass(/badge-warning/);
    await badge(support).click();
    await expect(notes.locator("strong")).toHaveText("Support note");
    await close.click();
    await expect(notes).toBeHidden();
    await expect(badge(sales)).toHaveClass(/badge-info/);
    await badge(sales).click();
    await expect(notes.locator(".js-notes-body")).toHaveText("Sales note");
    await edit.click();
    const editor = page.getByRole("dialog", { name: "Edit Refund Check" });
    await expect(editor).toBeVisible();
    await expect(page.locator(".js-bf-editor-root")).toHaveAttribute("data-bf-open", String(id));
    await editor.getByRole("button", { name: "Close editor" }).click();
    await expect(editor).toBeHidden();

    await page.getByRole("link", { name: /To review/ }).click();
    await expect(badge(sales)).toHaveAttribute("title", "Sales — view the QA note");
    await badge(sales).click();
    await expect(notes.locator(".js-notes-body")).toHaveText("Sales note");
    await expect(edit).toBeVisible();
    await close.click();

    await page.getByRole("link", { name: "My Reviews" }).click();
    await expect(badge(sales)).toHaveText("Sales · Warning");
    await badge(sales).click();
    await expect(notes.locator(".js-notes-body")).toHaveText("Sales note");
    await expect(edit).toBeHidden();
  } finally {
    await page.request.delete(`${base}/api/refund-check/${id}`);
  }
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
