const { expect, test } = require("@playwright/test");
const { base, signIn } = require("./harness.cjs");

async function clickBackdrop(page, dialog) {
  const bounds = await dialog.boundingBox();
  const viewport = page.viewportSize();
  const x = bounds.x > 4 ? 2 : viewport.width - 2;
  const y = x < bounds.x || x > bounds.x + bounds.width
    ? Math.floor(viewport.height / 2) : 2;
  expect(x < bounds.x || x > bounds.x + bounds.width || y < bounds.y).toBe(true);
  await page.mouse.click(x, y);
  await expect(dialog).toBeVisible();
}

async function verifyDialog(page, open, dialog, close, { useEscape = false } = {}) {
  await open.press("Enter");
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveClass(/\bgys-modal\b/);
  await expect(dialog).toHaveAttribute("closedby", "closerequest");
  await clickBackdrop(page, dialog);
  const shape = await close.evaluate((button) => {
    const css = getComputedStyle(button);
    return [css.width, css.height, css.borderRadius];
  });
  expect(shape[0]).toBe(shape[1]);
  expect(parseFloat(shape[2])).toBeGreaterThan(0);
  expect(parseFloat(shape[2])).toBeLessThan(parseFloat(shape[0]) / 2);
  const resting = await close.evaluate((button) => getComputedStyle(button).backgroundColor);
  await close.hover();
  await expect(close).not.toHaveCSS("background-color", resting);
  if (useEscape) await page.keyboard.press("Escape");
  else await close.click();
  await expect(dialog).toBeHidden();
  return shape;
}

test("delegate, review drawer and both tracker dialogs use the same close control and backdrop policy", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  const openDelegate = page.getByRole("button", { name: "Delegate case", exact: true });
  const delegate = page.locator("#delegate-case-modal");
  const expectedShape = await verifyDialog(page, openDelegate, delegate,
    delegate.locator(".gys-modal-close"), { useEscape: true });
  await expect(openDelegate).toBeFocused();

  const openReview = page.getByRole("button", { name: "New Review", exact: true });
  const review = page.getByRole("dialog", { name: "New Review" });
  expect(await verifyDialog(page, openReview, review,
    review.getByRole("button", { name: "Close review" }))).toEqual(expectedShape);
  await expect(openReview).toBeFocused();

  for (const tracker of ["Bad Feedback", "Refund Check"]) {
    const tab = page.getByRole("tab", { name: tracker, exact: true });
    await tab.click();
    await expect(tab).toHaveAttribute("aria-selected", "true");
    const view = page.locator(`#${tracker.toLowerCase().replace(" ", "-")}-view`);
    await expect(view).toBeVisible();
    const openImport = view.getByRole("button", { name: "Import from Excel" });
    const importer = page.getByRole("dialog", { name: `Import ${tracker}` });
    expect(await verifyDialog(page, openImport, importer, importer.locator(".gys-modal-close"))).toEqual(expectedShape);
    await expect(openImport).toBeFocused();

    const openEditor = page.getByRole("button", { name: `New ${tracker}`, exact: true });
    const editor = page.getByRole("dialog", { name: `New ${tracker}` });
    expect(await verifyDialog(page, openEditor, editor,
      editor.getByRole("button", { name: "Close editor" }), { useEscape: true })).toEqual(expectedShape);
    await expect(openEditor).toBeFocused();
  }
});

test("the side editor keeps its menu in the dialog and preserves an unsaved draft", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  const opener = page.getByRole("button", { name: "New Review", exact: true });
  const drawer = page.getByRole("dialog", { name: "New Review" });
  const editor = drawer.locator(".ql-editor");
  const menu = drawer.locator(".js-ai-context-menu");

  await opener.press("Enter");
  await expect(drawer.getByRole("button", { name: "Close review" })).toBeFocused();
  await editor.fill("Synthetic draft for dialog checks.");
  await editor.click({ button: "right" });
  await expect(menu.getByRole("button", { name: "Copy All", exact: true })).toBeVisible();
  const panelBounds = await drawer.boundingBox();
  const menuBounds = await menu.boundingBox();
  expect(menuBounds.x).toBeGreaterThanOrEqual(panelBounds.x);
  expect(menuBounds.x + menuBounds.width).toBeLessThanOrEqual(panelBounds.x + panelBounds.width);

  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await expect(drawer).toBeVisible();
  await editor.click({ button: "right" });
  await expect(menu).toBeVisible();
  await drawer.getByRole("button", { name: "Close review" }).click();
  await expect(drawer).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.press("Enter");
  await expect(editor).toHaveText("Synthetic draft for dialog checks.");
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
});

test("a native modal enters and exits with shared motion and skips it when reduced motion is requested", async ({ page }) => {
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/qa-score`);
  const opener = page.getByRole("button", { name: "Delegate case", exact: true });
  const dialog = page.locator("#delegate-case-modal");
  const entry = await opener.evaluate((trigger) => {
    trigger.click();
    const dialog = document.getElementById("delegate-case-modal");
    getComputedStyle(dialog).opacity;
    return dialog.getAnimations().map((animation) => animation.transitionProperty);
  });
  expect(entry).toContain("opacity");
  await expect(dialog).toBeVisible();
  await dialog.evaluate(async (node) => {
    await Promise.allSettled(node.getAnimations().map((animation) => animation.finished));
    node.addEventListener("transitionrun", (event) => {
      if (event.target === node && event.propertyName === "opacity" && node.hasAttribute("data-closing")) {
        node.dataset.exitTransitionObserved = "true";
      }
    });
  });
  await dialog.locator(".gys-modal-close").click();
  await expect(dialog).toHaveAttribute("data-exit-transition-observed", "true");
  await expect(dialog).toBeHidden();

  await page.emulateMedia({ reducedMotion: "reduce" });
  await opener.press("Enter");
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((node) => ({
    durations: getComputedStyle(node).transitionDuration,
    translate: getComputedStyle(node).translate,
    animations: node.getAnimations().length,
  }))).toEqual({ durations: "0s", translate: "none", animations: 0 });
  // Native close restores focus before its queued close event. Do not drop
  // an opener activation delivered in that gap.
  await opener.evaluate((trigger) => {
    trigger.addEventListener("focus", () => trigger.click(), { once: true });
  });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".gys-modal-close")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("notes and destructive confirmation share the modal policy and cancellation never deletes", async ({ page }) => {
  await signIn(page);
  const { id: user } = await (await page.request.get(`${base}/auth/me`)).json();
  const response = await page.request.post(`${base}/api/refund-check`, {
    data: {
      related_case: "CI-modal-check",
      agents: [{ user_id: user, kind: "Support", qa_comment: "CI sample note" }],
    },
  });
  expect(response.status()).toBe(201);
  const { id, agents } = await response.json();
  let deleted = false;
  try {
    await page.goto(`${base}/#qa-tracker/refund-check`);
    const badge = page.locator(`[data-case-notes="refund-check-${id}-${agents[0].id}"]`);
    const notes = page.locator(".js-notes-modal");
    const shape = await verifyDialog(page, badge, notes, notes.locator(".gys-modal-close"));
    await expect(badge).toBeFocused();

    const remove = page.locator(`[data-tracker-delete="${id}"]`);
    const confirm = page.getByRole("dialog").filter({ has: page.getByRole("button", { name: "Cancel", exact: true }) });
    expect(await verifyDialog(page, remove, confirm, confirm.locator(".gys-modal-close"),
      { useEscape: true })).toEqual(shape);
    await expect(remove).toBeFocused();
    expect((await page.request.get(`${base}/api/refund-check/${id}`)).status()).toBe(200);

    await remove.click();
    await expect(confirm.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
    const deletion = page.waitForResponse((result) => result.request().method() === "DELETE"
      && new URL(result.url()).pathname === `/api/refund-check/${id}`);
    await confirm.getByRole("button", { name: "Delete", exact: true }).click();
    deleted = (await deletion).ok();
    expect(deleted).toBe(true);
    await expect(confirm).toBeHidden();
  } finally {
    if (!deleted) await page.request.delete(`${base}/api/refund-check/${id}`);
  }
});
