const { expect, test } = require("@playwright/test");
const { base, signIn } = require("./harness.cjs");

async function setup(page, reducedMotion) {
  await page.emulateMedia({ reducedMotion });
  await page.clock.install();
  await signIn(page);
  await page.goto(`${base}/#qa-tracker/refund-check`);
  await page.getByRole("button", { name: "New Refund Check" }).click();
  const dialog = page.getByRole("dialog", { name: "New Refund Check" });
  const cancel = dialog.getByRole("button", { name: "Cancel" });
  await cancel.focus();
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
  const stack = dialog.locator(":scope > .gys-float-stack");
  await page.evaluate(() => {
    // Oldest toast sits below the survivor.
    window.gysFloatNote("First note", "alert-success", 1000);
    window.gysFloatNote("Second note", "alert-success", 1050);
    window.gysFloatNote("Surviving note", "alert-success", 6000);
  });
  const row = (text) => stack.locator(".gys-float-row").filter({ hasText: text });
  return { cancel, stack, row };
}

test("the toast stack closes gaps smoothly when multiple notes expire", async ({ page }) => {
  await page.addInitScript(() => {
    const animate = Element.prototype.animate;
    Element.prototype.animate = function (...args) {
      const animation = animate.apply(this, args);
      if (this.matches(".gys-float-row")) {
        animation.pause();
        animation.currentTime = 0;
      }
      return animation;
    };
  });
  const { cancel, row } = await setup(page, "no-preference");
  const survivor = row("Surviving note");
  const top = () => survivor.evaluate((node) => node.getBoundingClientRect().top);
  const start = await top();
  await page.clock.runFor(1050);
  expect(await top()).toBeCloseTo(start, 0);
  for (const text of ["First note", "Second note"]) {
    await row(text).evaluate((node) => {
      const [collapse] = node.getAnimations();
      collapse.currentTime = collapse.effect.getComputedTiming().duration / 2;
    });
  }
  const half = await top();
  expect(half).toBeGreaterThan(start);
  for (const text of ["First note", "Second note"]) {
    await row(text).evaluate((node) => node.getAnimations()[0].finish());
    await expect(row(text)).toHaveCount(0);
  }
  expect(await top()).toBeGreaterThan(half);
  await expect(cancel).toBeFocused();
});

test("reduced motion closes gaps immediately", async ({ page }) => {
  const { cancel, stack, row } = await setup(page, "reduce");
  const survivor = row("Surviving note");
  const top = () => survivor.evaluate((node) => node.getBoundingClientRect().top);
  const start = await top();
  await page.clock.runFor(1050);
  await expect(stack.getByRole("alert")).toHaveCount(1);
  expect(await top()).toBeGreaterThan(start);
  expect(await stack.evaluate((node) => node.getAnimations({ subtree: true }))).toEqual([]);
  await expect(cancel).toBeFocused();
});
