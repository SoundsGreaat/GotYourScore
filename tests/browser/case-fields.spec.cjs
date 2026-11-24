const { expect, test } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
const { base, nextResponse, signIn, watchPage } = require("./harness.cjs");

test("Case Fields reuses admin pickers and keeps the tab and other edits on save and archive", async ({ page }) => {
  const failures = watchPage(page);
  const label = `Refund reason ${randomUUID()}`;
  const renamed = `${label} updated`;
  await signIn(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto(`${base}/admin#case-fields`);
  const refund = page.getByRole("region", { name: "Refund Check", exact: true });
  const feedback = page.getByRole("region", { name: "Bad Feedback", exact: true });
  await feedback.locator("[data-case-default-focus]").click();
  const otherDraft = feedback.getByRole("form", { name: "Add Bad Feedback field" }).getByLabel("Label");
  await otherDraft.fill("Other tracker draft");
  await refund.locator("[data-case-default-focus]").click();
  const add = refund.getByRole("form", { name: "Add Refund Check field" });
  const addLabel = add.getByLabel("Label");
  const type = add.getByRole("button", { name: "Type" });
  const [labelBox, typeBox] = await Promise.all([addLabel.boundingBox(), type.boundingBox()]);
  expect(typeBox.height).toBe(labelBox.height);
  expect(typeBox.y).toBe(labelBox.y);
  const dateRow = refund.getByRole("listitem").filter({
    has: page.getByRole("group", { name: "Reorder Date", exact: true }),
  });
  const editAction = dateRow.locator("summary").getByText("Edit", { exact: true }).last();
  await expect(editAction).toBeVisible();
  const [gripBox, nameBox, editBox, reorderBox] = await Promise.all([
    dateRow.locator(".scorecard-grip").boundingBox(),
    dateRow.locator("[data-case-field-name]").boundingBox(),
    editAction.boundingBox(),
    dateRow.getByRole("group", { name: "Reorder Date", exact: true }).boundingBox(),
  ]);
  expect(gripBox.x + gripBox.width).toBeLessThan(nameBox.x);
  expect(editBox.x + editBox.width).toBeLessThan(reorderBox.x);
  expect(gripBox.y + gripBox.height / 2).toBeCloseTo(reorderBox.y + reorderBox.height / 2);
  await add.getByLabel("Label").fill(label);
  await add.getByRole("button", { name: "Type" }).click();
  await add.getByRole("menuitemradio", { name: "Long text", exact: true }).click();
  await expect(add.getByRole("button", { name: "Type" })).toHaveText("Long text");
  await page.evaluate(() => { window.testCaseFieldsRoot = document.getElementById("admin-case-fields-root"); });
  const created = nextResponse(page, "/admin/case-fields/refund-check/create", "POST");
  await add.getByRole("button", { name: "Add field" }).click();
  expect((await created).status()).toBe(200);
  await expect(add.getByLabel("Label")).toHaveValue("");
  await expect(add.getByRole("button", { name: "Type" })).toHaveText("Text");
  await expect(feedback.getByText(label, { exact: true })).toHaveCount(0);
  await refund.locator("summary").getByText("Source", { exact: true }).click();
  const sourceDraft = refund.getByRole("form", { name: "Edit Source", exact: true });
  await sourceDraft.getByLabel("Label").fill("Unsaved source");
  await sourceDraft.getByRole("button", { name: "Type" }).click();
  await sourceDraft.getByRole("menuitemradio", { name: "Long text", exact: true }).click();
  await refund.locator("summary").filter({ hasText: label }).click();
  const edit = refund.getByRole("form", { name: `Edit ${label}` });
  await expect(edit.getByRole("button", { name: "Type" })).toHaveText("Long text");
  const [editLabel, editType] = await Promise.all([
    edit.getByLabel("Label").boundingBox(), edit.getByRole("button", { name: "Type" }).boundingBox(),
  ]);
  expect(editType.height).toBe(editLabel.height);
  expect(editType.y).toBe(editLabel.y);
  await edit.getByLabel("Label").fill(renamed);
  const save = nextResponse(page, /\/admin\/case-fields\/refund-check\/\d+\/update$/, "POST");
  await edit.getByRole("button", { name: "Save changes" }).click();
  expect((await save).status()).toBe(200);
  const renamedForm = refund.getByRole("form", { name: `Edit ${renamed}` });
  await expect(renamedForm).toBeVisible();
  await expect(renamedForm.getByRole("button", { name: "Save changes" })).toBeFocused();
  await expect(otherDraft).toHaveValue("Other tracker draft");
  await expect(sourceDraft.getByLabel("Label")).toHaveValue("Unsaved source");
  await expect(sourceDraft.getByRole("button", { name: "Type" })).toHaveText("Long text");
  const archiveDetails = refund.locator('details[id$="-archive"]');
  await archiveDetails.locator("summary").click();
  await expect(archiveDetails).toHaveJSProperty("open", true);
  const archived = nextResponse(page, /\/admin\/case-fields\/refund-check\/\d+\/archive$/, "POST");
  await renamedForm.getByRole("button", { name: `Archive ${renamed}` }).click();
  expect((await archived).status()).toBe(200);
  await expect(renamedForm).toHaveCount(0);
  await expect(archiveDetails.getByRole("button", { name: `Restore ${renamed}`, exact: true })).toBeVisible();
  await expect(otherDraft).toHaveValue("Other tracker draft");
  await expect(sourceDraft.getByLabel("Label")).toHaveValue("Unsaved source");
  expect(await page.evaluate(() => window.testCaseFieldsRoot === document.getElementById("admin-case-fields-root"))).toBe(true);

  const restored = nextResponse(page, /\/admin\/case-fields\/refund-check\/\d+\/restore$/, "POST");
  await archiveDetails.getByRole("button", { name: `Restore ${renamed}`, exact: true }).click();
  expect((await restored).status()).toBe(200);
  await expect(archiveDetails).toHaveJSProperty("open", true);
  await expect(refund.locator("summary").filter({ hasText: renamed })).toBeVisible();
  // Leave the new field archived for the other tests.
  await refund.locator("summary").filter({ hasText: renamed }).click();
  await renamedForm.getByRole("button", { name: `Archive ${renamed}` }).click();
  await expect(renamedForm).toHaveCount(0);

  let releaseCreate;
  const heldCreate = new Promise((resolve) => { releaseCreate = resolve; });
  const createPath = "/admin/case-fields/refund-check/create";
  await page.route(`**${createPath}`, async (route) => {
    await heldCreate;
    await route.continue();
  }, { times: 1 });
  await add.getByLabel("Label").fill(`${label} first draft`);
  const createRequest = page.waitForRequest(`**${createPath}`);
  await add.getByRole("button", { name: "Add field" }).click();
  try {
    await createRequest;
    await add.getByLabel("Label").fill("Next draft kept");
  } finally {
    releaseCreate();
  }
  const firstDraft = refund.locator("summary").getByText(`${label} first draft`, { exact: true });
  await expect(firstDraft).toBeVisible();
  await expect(add.getByLabel("Label")).toHaveValue("Next draft kept");
  await firstDraft.click();
  await refund.getByRole("button", { name: `Archive ${label} first draft`, exact: true }).click();
  await expect(firstDraft).toHaveCount(0);
  await page.setViewportSize({ width: 320, height: 720 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  expect(failures).toEqual([]);
});

test("Case Fields persists drag and arrow moves and respects reduced motion", async ({ page }) => {
  await signIn(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto(`${base}/admin#case-fields`);
  const list = page.getByRole("list", { name: "Refund Check active fields", exact: true });
  const rows = list.getByRole("listitem");
  const names = list.locator("[data-case-field-name]");
  await expect(names.first()).toBeVisible();
  const initial = await names.allTextContents();
  expect(initial.length).toBeGreaterThan(1);
  const first = initial[0];
  const path = "/admin/case-fields/refund-check/reorder";
  const grips = list.locator(".scorecard-grip");
  const drag = nextResponse(page, path, "POST");
  await grips.first().dragTo(rows.nth(1), { targetPosition: { x: 35, y: 48 } });
  expect((await drag).status()).toBe(204);
  await expect(names.first()).toHaveText(initial[1]);
  await page.reload();
  await expect(names.first()).toHaveText(initial[1]);

  await page.emulateMedia({ reducedMotion: "reduce" });
  const move = list.getByRole("button", { name: `Move ${first} up`, exact: true });
  const restore = nextResponse(page, path, "POST");
  await move.press("Enter");
  expect((await restore).status()).toBe(204);
  await expect(names).toHaveText(initial);
  await expect(move).toBeFocused();
  expect(await list.evaluate((node) => node.getAnimations({ subtree: true })
    .filter((animation) => animation.effect.getKeyframes().some((frame) => frame.translate)).length)).toBe(0);
  await expect(list.locator("..").getByRole("status")).toHaveText(`${first} moved up to position 1.`);

  await page.route(`**${path}`, (route) => route.fulfill({
    status: 400, json: { detail: "The field order is out of date." },
  }), { times: 1 });
  const down = list.getByRole("button", { name: `Move ${first} down`, exact: true });
  await down.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "The field order is out of date." })).toBeVisible();
  await expect(names).toHaveText(initial);
  await expect(down).toBeFocused();
});

test("returning to Case Fields waits for a pending reorder before loading the list", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/admin#case-fields`);
  const list = page.getByRole("list", { name: "Refund Check active fields", exact: true });
  const names = list.locator("[data-case-field-name]");
  const first = await names.first().textContent();
  const second = await names.nth(1).textContent();
  await list.evaluate((node) => { window.previousList = node; });
  const path = "/admin/case-fields/refund-check/reorder";
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route(`**${path}`, async (route) => {
    await held;
    await route.continue();
  }, { times: 1 });
  await page.evaluate(() => document.addEventListener("htmx:before:request", ({ detail }) => {
    if (detail.ctx.request.action === "/admin/partials/case-fields") window.caseFieldsRequested = true;
  }));
  let fetches = 0;
  await page.route("**/admin/partials/case-fields", (route) => {
    fetches++;
    return route.continue();
  });
  const order = page.waitForRequest(`**${path}`);
  await list.getByRole("button", { name: `Move ${first} down`, exact: true }).click();
  try {
    await order;
    await page.getByRole("tab", { name: "Users", exact: true }).click();
    await expect(page.getByRole("tabpanel", { name: "Users", exact: true })).toBeVisible();
    expect(await page.evaluate(() => window.Sortable.get(window.previousList) == null)).toBe(true);
    await page.getByRole("tab", { name: "Case Fields", exact: true }).click();
    await expect.poll(() => page.evaluate(() => window.caseFieldsRequested)).toBe(true);
    expect(fetches).toBe(0);
  } finally {
    release();
  }
  await expect(names.first()).toHaveText(second);
  const restored = nextResponse(page, path, "POST");
  await list.getByRole("button", { name: `Move ${first} up`, exact: true }).click();
  expect((await restored).status()).toBe(204);
  await expect(names.first()).toHaveText(first);
});
