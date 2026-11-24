const { expect, test } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
const { base, nextResponse, signIn, watchPage } = require("./harness.cjs");

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
  const selected = page.locator("[data-template-card]").filter({ hasText: template });
  await expect(selected.locator("[data-template-open]")).toHaveAttribute("aria-current", "true");
  await expect(selected).toHaveCSS("box-shadow", "none");
  await editor.getByRole("button", { name: "+ Add rule (new category)" }).click();
  await editor.locator("[data-display]").fill("CI rule");
  await editor.getByRole("button", { name: "+ Add rule", exact: true }).click();
  const draft = editor.locator("[data-row]").last();
  await expect(draft.getByRole("spinbutton", { name: "Penalty points" })).toHaveValue("1");
  await expect(draft.getByRole("checkbox", { name: "Rule active" })).toBeChecked();
  await draft.getByRole("button", { name: "Remove rule" }).click();
  await expect(editor.locator("[data-row]")).toHaveCount(1);
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

  await editor.getByRole("textbox", { name: "Template name" }).fill(template);
  await editor.getByRole("button", { name: "Remove rule" }).click();
  await expect(rule).toHaveCount(0);
  const removed = nextResponse(page, savePath, "POST");
  await save.click();
  expect((await removed).status()).toBe(200);
  await expect(editor.locator("[data-empty-rules]")).toBeVisible();
});

test("admin scorecard toggles retain the editor, dirty fields and keyboard focus", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/admin#scorecards`);
  const editor = page.locator("#scorecard-editor");
  const create = async (name, open = false) => {
    const form = page.getByRole("form", { name: "New template" });
    await form.getByRole("textbox", { name: "Template name" }).fill(name);
    await form.getByRole("button", { name: "Create", exact: true }).click();
    if (open) await page.getByRole("button", { name: new RegExp(name) }).click();
    await expect(editor.getByRole("textbox", { name: "Template name" })).toHaveValue(name);
  };
  const name = `CI toggle ${randomUUID()}`;
  const sibling = `${name} sibling`;
  await create(name);
  await create(sibling, true);
  const editorField = editor.getByRole("textbox", { name: "Template name" });
  await editorField.fill(`${sibling} unsaved`);
  const original = await editorField.elementHandle();

  const first = page.getByRole("checkbox", { name: `Active template: ${name}`, exact: true });
  const second = page.getByRole("checkbox", { name: `Active template: ${sibling}`, exact: true });
  const togglePath = /\/admin\/scorecards\/\d+\/toggle$/;
  await expect(first).not.toBeChecked();
  await expect(second).toBeChecked();
  await first.focus();
  const activation = nextResponse(page, togglePath, "POST");
  await page.keyboard.press("Space");
  expect((await activation).status()).toBe(204);
  await expect(first).toBeFocused();
  await expect(first).toBeChecked();
  await expect(second).not.toBeChecked();
  await expect(editor.locator("[data-template-status]")).toHaveText("Inactive");
  await expect(editorField).toHaveValue(`${sibling} unsaved`);
  expect(await original.evaluate((node) => node.isConnected)).toBe(true);

  const deactivation = nextResponse(page, togglePath, "POST");
  await page.keyboard.press("Space");
  expect((await deactivation).status()).toBe(204);
  await expect(first).not.toBeChecked();
  await expect(second).not.toBeChecked();
  await expect(first).toBeFocused();
  await expect(editorField).toHaveValue(`${sibling} unsaved`);

  const reset = nextResponse(page, togglePath, "POST");
  await second.click();
  expect((await reset).status()).toBe(204);
  await expect(editor.locator("[data-template-status]")).toHaveText("Active");
  let send;
  const delayed = new Promise((resolve) => { send = resolve; });
  await page.route(togglePath, async (route) => {
    await delayed;
    await route.continue();
  }, { times: 1 });
  const queued = page.waitForResponse((response) =>
    togglePath.test(new URL(response.url()).pathname) && response.request().postData() === "active=false"
  );
  try {
    await first.click();
    await second.click();
  } finally {
    send();
  }
  expect((await queued).status()).toBe(204);
  await expect(first).toBeChecked();
  await expect(second).not.toBeChecked();
  await expect(editor.locator("[data-template-status]")).toHaveText("Inactive");
  expect(failures).toEqual([]);
});

test("creating a template keeps the open scorecard's unsaved editor and selection", async ({ page }) => {
  const failures = watchPage(page);
  await signIn(page);
  await page.goto(`${base}/admin#scorecards`);
  const editor = page.locator("#scorecard-editor");
  const editorName = editor.getByRole("textbox", { name: "Template name" });
  const form = page.getByRole("form", { name: "New template" });
  const templateName = form.getByRole("textbox", { name: "Template name" });
  const original = `CI preserved ${randomUUID()}`;
  const created = `${original} sibling`;
  await templateName.fill(original);
  await templateName.press("Enter");
  await expect(editorName).toHaveValue(original);
  await expect(templateName).toHaveValue("");
  await editorName.fill(`${original} unsaved`);
  await editor.getByRole("button", { name: "+ Add rule (new category)" }).click();
  const rule = editor.getByRole("textbox", { name: "Rule display name" });
  await rule.fill("Unsaved rule");
  const ruleNode = await rule.elementHandle();

  const createPath = "/admin/scorecards/create";
  let release;
  const holdCreate = new Promise((resolve) => { release = resolve; });
  await page.route(`**${createPath}`, async (route) => {
    await holdCreate;
    await route.continue();
  }, { times: 1 });
  const response = nextResponse(page, createPath, "POST");
  try {
    const submitted = page.waitForRequest((request) => new URL(request.url()).pathname === createPath);
    await templateName.fill(created);
    await templateName.press("Enter");
    await submitted;
    await templateName.fill("Another template draft");
    await rule.focus();
  } finally {
    release();
  }
  expect((await response).status()).toBe(200);

  const currentCard = page.getByRole("checkbox", { name: `Active template: ${original}`, exact: true });
  const createdCard = page.getByRole("checkbox", { name: `Active template: ${created}`, exact: true });
  await expect(createdCard).toBeChecked();
  await expect(currentCard).not.toBeChecked();
  await expect(editor.locator("[data-template-status]")).toHaveText("Inactive");
  const selected = page.locator("[data-template-open][aria-current=true]");
  await expect(selected).toHaveCount(1);
  await expect(selected.locator("span").first()).toHaveText(original);
  await expect(editorName).toHaveValue(`${original} unsaved`);
  await expect(rule).toHaveValue("Unsaved rule");
  await expect(rule).toBeFocused();
  expect(await ruleNode.evaluate((node) => node.isConnected)).toBe(true);
  await expect(templateName).toHaveValue("Another template draft");
  expect(failures).toEqual([]);
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
